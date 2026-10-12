"""Stage 3 loop: server health -> rescue (route-around) -> barcode decoder -> name (no I/O).

A *server incident* opens when any server turns slow (debounced, see health.py):

    detected   which servers are slow and by how much
    rescued    RouteWeightsCmd moving victims off the slow servers (capped by rescue_capacity_guard)
    named      the decoder's verdict, once it has been stable for open_after_ticks ticks:
               culprit tenant(s) with confidence, or host suspect(s) with no tenant blamed
    proving    a relocation test (culprit -> spare) or a drain test (host suspect), run by the runtime
    verdict    one of five (verdicts.py) + the action it takes, with the reasons
    resolved   every server healthy again: route weights cleared, our drains undone
    closed

Routing changes, tests and actions happen only in `tenantguard` mode; health and decoding always run
(they feed ServerView and the status sentence). The tests themselves are async (prover.py): this module
only asks for them (`jobs`) and judges their results (`on_proof`, `on_host`).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from tenantguard_common.contracts import (DrainCmd, IncidentEvent, RelocateCmd, RouteWeightsCmd, Suspect,
                                          TelemetryFrame)

from .decoder import Decoding, decode
from .health import HealthTracker
from .incidents import Incident
from .prover import HostCheck, HostResult, NullProver, ProofRequest, ProofResult, Prover
from .rescue import RescuePlan, plan_rescue
from .verdicts import (COSTLY, HONEST, HOST, NO_BLAME, OVER, ActionBudget, Evidence, OffenderMemory,
                       VerdictParams, classify, host_verdict)

if TYPE_CHECKING:
    from .core import Brain

LEDGER_WINDOW_S = 3.0
GROWTH_FLOOR_MS = 20.0            # ms/s: growth is measured against max(baseline, this)
CLEAR_TTL_S = 5.0                 # equal weights (= default routing) for a few seconds, then nothing


@dataclass
class Ledger:
    bg_growth: Dict[str, float] = field(default_factory=dict)
    evict_share: Dict[str, float] = field(default_factory=dict)
    blackbox_all: Dict[str, float] = field(default_factory=dict)
    over_share: Dict[str, float] = field(default_factory=dict)

    def hint(self, t: str) -> float:
        return max(self.bg_growth.get(t, 0.0), self.evict_share.get(t, 0.0),
                   self.blackbox_all.get(t, 0.0), self.over_share.get(t, 0.0))

    def why(self, t: str) -> List[str]:
        out = []
        if self.over_share.get(t, 0) > 0.2:
            out.append("its paid CPU is well over its fair share")
        if self.bg_growth.get(t, 0) > 0.2:
            out.append("its background-job CPU grew")
        if self.evict_share.get(t, 0) > 0.2:
            out.append(f"it caused {self.evict_share[t]:.0%} of cache evictions")
        if self.blackbox_all.get(t, 0) > 0.2:
            out.append("unattributed (black-box) CPU grew on all of its servers")
        return out


class Ewma:
    def __init__(self, alpha: float = 0.1):
        self.alpha, self.v = alpha, {}

    def update(self, k: str, x: float) -> None:
        self.v[k] = x if k not in self.v else self.v[k] + self.alpha * (x - self.v[k])

    def growth(self, k: str, x: float) -> float:
        base = self.v.get(k, 0.0)
        return max(0.0, min(1.0, (x - base) / max(base, GROWTH_FLOOR_MS)))


class Stage3:
    def __init__(self, brain: "Brain", prover: Optional[Prover] = None):
        self.b = brain
        cfg = brain.cfg
        self.cfg = cfg
        servers = sorted(set(cfg.worker_urls) | set(cfg.fleet_servers))
        self.barcodes = {t: list(c.servers) for t, c in brain.tenants.items()}
        self.health = HealthTracker(servers, cfg.health, self.barcodes)
        self.prover: Prover = prover or NullProver()
        self.inc: Optional[Incident] = None
        self.decoding: Optional[Decoding] = None
        self.ledger = Ledger()
        self.plan: Optional[RescuePlan] = None
        self.route_sent: Dict[str, Dict[str, float]] = {}       # tenant -> weights in force (from us)
        self.route_sent_at = 0.0
        self.route_ok: Optional[bool] = None
        self.reported_slow: Tuple[str, ...] = ()
        self.reported_rescue: Tuple[str, ...] = ()
        self.named_key: Optional[tuple] = None
        self.pending_key: Optional[tuple] = None
        self.pending_n = 0
        self.bg_base = Ewma(0.01)                     # ~30 s time constant at 4 Hz
        self.bb_base = Ewma(0.01)
        self.vp: VerdictParams = cfg.verdicts
        self.budget: ActionBudget = cfg.action_budget
        self.memory = OffenderMemory(self.vp.repeat_count, self.vp.repeat_window_s)
        self.confidence: Dict[str, float] = {}       # culprit -> confidence when named (this incident)
        self.judged: Dict[str, str] = {}              # subject ("K" or "host:w4") -> verdict (this incident)
        self.proving: Dict[str, float] = {}           # subject -> since
        self.noted: Dict[str, str] = {}               # subject -> last "deferred" note (said once)
        self.jobs: List[object] = []                  # ProofRequest | HostCheck | ("restore_spare", cores)
        self.control: List[Tuple[str, str, object]] = []   # (sender key, path, command)
        self.drains: Dict[str, float] = {}            # server -> last DrainCmd sent (ours, host_fault)
        self.pre_evidence: Dict[str, dict] = {}       # culprit -> verdict inputs captured before its test
        self.rescued_at: Optional[float] = None       # last time the rescue grew (ladder grace, see core)

    # ------------------------------------------------------------- inputs
    def on_frame(self, frame: TelemetryFrame, now: float) -> None:
        # tenant-relative health (health.py); failed requests (5xx) say nothing about a server's speed
        self.health.observe(((c.tenant, c.server, c.p95_ms, c.count - min(c.errors, c.count))
                             for c in frame.cells), now)

    def on_canary(self, server: str, rtt_ms: float, now: float) -> None:
        self.health.observe_canary(server, rtt_ms, now)

    # ------------------------------------------------------------- ledger hints
    def _update_ledger(self, now: float, slow: List[str]) -> None:
        bg = defaultdict(float)
        ev = defaultdict(int)
        bb: Dict[str, Tuple[float, float]] = {}
        for s in self.b.stat_samples:
            if s.t <= now - LEDGER_WINDOW_S:
                continue
            for t, ms in s.tenant_bg_ms.items():
                bg[t] += ms
            for t, n in s.tenant_evictions.items():
                ev[t] += n
            ms, dt = bb.get(s.server, (0.0, 0.0))
            bb[s.server] = (ms + s.blackbox_ms, dt + s.dt)
        span = LEDGER_WINDOW_S
        bg_rate = {t: v / span for t, v in bg.items()}
        bb_rate = {s: (ms / dt if dt > 0 else 0.0) for s, (ms, dt) in bb.items()}
        led = Ledger()
        led.bg_growth = {t: self.bg_base.growth(t, r) for t, r in bg_rate.items()}
        tot_ev = sum(ev.values())
        led.evict_share = {t: n / tot_ev for t, n in ev.items()} if tot_ev >= 5 else {}
        g = {s: self.bb_base.growth(s, r) for s, r in bb_rate.items()}
        led.blackbox_all = {t: min(g.get(s, 0.0) for s in servers) for t, servers in self.barcodes.items()}
        led.over_share = {t: max(0.0, min(1.0, sd.usage_ratio - 1.0)) for t, sd in self.b.scores.items()}
        self.ledger = led
        # baselines learn only in a real calm: nothing open, nothing even momentarily slow, and the ladder calm
        # (with persistence, an attack runs for several seconds before any server opens)
        quiet = not slow and self.b.calm and not any(h.slow_now for h in self.health.servers.values())
        if quiet:
            for t, r in bg_rate.items():
                self.bg_base.update(t, r)
            for s, r in bb_rate.items():
                self.bb_base.update(s, r)

    # ------------------------------------------------------------- tick
    def held_by_culprit(self) -> List[str]:
        """Servers a named culprit still over-uses. A rescue moves the victims away, so those servers can look
        fine again while the culprit is still hot; lifting the rescue then would just bring the victims back
        into harm. Such servers stay 'slow' until the culprit is back within its fair share."""
        if not self.named_key or self.inc is None:
            return []
        out = set()
        for t in self.named_key[0]:
            sd = self.b.scores.get(t)
            if sd is not None and sd.usage_ratio > 1.0:
                out |= set(self.barcodes[t]) & set(self.reported_slow)
        return sorted(out)

    def tick(self, now: float, acting: bool) -> Tuple[List[IncidentEvent], List[RouteWeightsCmd]]:
        slow = sorted(set(self.health.slow_servers()) | set(self.held_by_culprit()))
        self._update_ledger(now, slow)
        stuck = {t: all(s in slow for s in v) for t, v in self.barcodes.items()} if slow else {}
        fleet_ratios = {k: v for k, v in self.health.ratios().items() if self.health._peer(k)}
        self.decoding = decode(self.barcodes, fleet_ratios, stuck=stuck,
                               ledger={t: self.ledger.hint(t) for t in self.barcodes},
                               params=self.cfg.decoder) if slow else None
        events: List[IncidentEvent] = []
        cmds: List[RouteWeightsCmd] = []
        if not acting:
            if self.inc is not None:
                events += self._close(now, f"mode is '{self.b.mode}': routing changes are off outside "
                                           f"'tenantguard' mode", cmds)
            return events, cmds

        if slow and self.inc is None:
            self.inc = self.b.book.open_server(now)
        if self.inc is None:
            return events, cmds

        if not slow and self.proving:
            # a running test moved the suspect (or drained the host): its servers look fine *because* of the
            # test. Keep the incident (and the rescue) until the verdict is in.
            slow = list(self.reported_slow)
        if not slow:
            events += self._close(now, "every server is back within its peers' latency", cmds)
            return events, cmds

        if not set(slow) <= set(self.reported_slow):          # report growth; shrinking shows in the state
            self.reported_slow = tuple(sorted(set(slow) | set(self.reported_slow)))
            events.append(self._emit(now, "detected", detail=self._slow_text(slow)))

        # rescue
        # a named culprit stays where it is (moving it would just spread it); the prover relocates it
        named = set(self.named_key[0]) if self.named_key else set()
        victims = {t: v for t, v in self.barcodes.items() if t not in named}
        up = [s for s, h in self.health.servers.items() if h.source != "none" and not h.slow]
        self.plan = plan_rescue(barcodes=victims, slow=slow, load=self.b.cpu_by_server(now),
                                capacity=self.b.server_capacity(now), guard=self.cfg.rescue_guard, targets=up)
        want = dict(self.plan.weights)
        for t in self.route_sent:                       # tenants no longer affected go back to default
            if t not in want:
                want[t] = self._equal(t)
        changed = any(t not in self.route_sent or _diff(self.route_sent[t], w) > 0.02 for t, w in want.items())
        renew = self.route_ok and now - self.route_sent_at >= self.cfg.rescue_ttl_s / 4
        retry = self.route_ok is False and now - self.route_sent_at >= 1.0
        if want and (changed or renew or retry):
            cmds.append(RouteWeightsCmd(weights=want, ttl_s=self.cfg.rescue_ttl_s,
                                        incident_id=self.inc.incident_id))
            self.route_sent = {t: w for t, w in want.items() if t in self.plan.weights}
            self.route_sent_at, self.route_ok = now, None
        rescued = tuple(sorted(self.plan.weights))
        if not set(rescued) <= set(self.reported_rescue):
            self.reported_rescue = tuple(sorted(set(rescued) | set(self.reported_rescue)))
            self.rescued_at = now
            events.append(self._emit(now, "rescued", action=f"RouteWeightsCmd {len(rescued)} tenants "
                                     f"ttl={self.cfg.rescue_ttl_s:g}s", detail=self._rescue_text(slow)))

        # naming, once the decoding is stable
        d = self.decoding
        key = (tuple(c.tenant for c in d.culprits), tuple(d.host_suspects)) if d else None
        if key == self.pending_key:
            self.pending_n += 1
        else:
            self.pending_key, self.pending_n = key, 1
        if (key and (key[0] or key[1]) and key != self.named_key
                and self.pending_n >= self.cfg.health.open_after_ticks):
            self.named_key = key
            events.append(self._named(now, d))
            for c in d.culprits:
                if c.tenant not in self.confidence:
                    self.memory.note_named(c.tenant, now)
                self.confidence[c.tenant] = c.confidence
        if self.named_key:
            events += self._judge(now)
        events += self._renew_drains(now)
        return events, cmds

    # ------------------------------------------------------------- judge: prove, then verdict
    def _judge(self, now: float) -> List[IncidentEvent]:
        out: List[IncidentEvent] = []
        culprits, hosts = self.named_key
        for t in culprits:
            if t in self.judged or t in self.proving:
                continue
            conf = self.confidence.get(t, 0.0)
            repeat, last = self.memory.is_repeat(t, now), self.memory.last_verdict.get(t)
            if repeat and last:
                ev = Evidence(tenant=t, confidence=conf, confirmed=False, repeat=True, last_verdict=last,
                              **self.b.verdict_inputs(t, now))
                out += self._verdict(now, t, last, [f"repeat offender: named {self.vp.repeat_count}+ times in "
                                                    f"{self.vp.repeat_window_s / 60:.0f} min, last verdict {last}; "
                                                    f"acting at once without re-proving"], ev, relocated=False)
                continue
            if conf < self.vp.confidence_to_prove:
                ev = Evidence(tenant=t, confidence=conf, confirmed=False, **self.b.verdict_inputs(t, now))
                v, why = classify(ev, self.vp)
                out += self._verdict(now, t, v, why, ev, relocated=False)
                continue
            if not self.prover.available:
                continue                                    # named only: "not yet proven"
            if not self.budget.can_relocate(now):
                out += self._note(now, t, "proof deferred: the relocation budget "
                                          f"({self.budget.relocations_per_min}/min) is used up")
                continue
            self.budget.note_relocation(now)
            self.proving[t] = now
            fp = self.b.footprint(t, now)
            # judge on what it did on its own servers, before the test moved it (and its black-box work) away
            self.pre_evidence[t] = self.b.verdict_inputs(t, now)
            self.jobs.append(ProofRequest(incident_id=self.inc.incident_id, tenant=t, servers=self.barcodes[t],
                                          confidence=conf, footprint_cpu_ms=fp["footprint"],
                                          paid_cpu_ms=fp["paid"]))
            ev = self._emit(now, "proving", action="RelocateCmd target=spare",
                            detail=f"Proving {self.b.tenants[t].name} ({t}): moving it to the spare (sized to "
                                   f"{self.cfg.prover.spare_sizing:g}x its {fp['footprint']:.0f} ms/s footprint), "
                                   f"waiting {self.cfg.prover.settle_s:g} s, then measuring for "
                                   f"{self.cfg.prover.window_s:g} s whether its servers recover and the harm follows.")
            ev.tenant = t
            ev.suspects = [Suspect(tenant=t, confidence=conf)]
            out.append(ev)
        for srv in hosts:
            subj = f"host:{srv}"
            if subj in self.judged or subj in self.proving:
                continue
            util = self.b.server_utilisation(now)
            ok, why = self.budget.can_drain(srv, [u for k, u in util.items() if k != srv and k in self.cfg.fleet_servers])
            if not ok:
                out += self._note(now, subj, f"drain test for {srv} deferred: {why}")
                continue
            self.budget.drained[srv] = now
            self.proving[subj] = now
            self.jobs.append(HostCheck(incident_id=self.inc.incident_id, server=srv))
            out.append(self._emit(now, "proving", action=f"DrainCmd {srv}",
                                  detail=f"Testing host suspect {srv}: draining it and measuring it idle for "
                                         f"{self.cfg.prover.window_s:g} s against its peers."))
        return out

    def _note(self, now: float, subj: str, text: str) -> List[IncidentEvent]:
        if self.noted.get(subj) == text:
            return []
        self.noted[subj] = text
        return [self._emit(now, "proving", detail=text.capitalize() + ".")]

    def on_proof(self, res: ProofResult, now: float) -> List[IncidentEvent]:
        t = res.tenant
        self.proving.pop(t, None)
        if self.inc is None or res.incident_id != self.inc.incident_id:
            # the incident ended while we were testing: put everything back
            if res.relocated:
                self._send_relocate(RelocateCmd(tenant=t, target=None, ttl_s=self.cfg.prover.relocate_ttl_s))
            self.jobs.append(("restore_spare", res.spare_cores_before))
            return []
        pre = self.pre_evidence.pop(t, None) or self.b.verdict_inputs(t, now)
        ev = Evidence(tenant=t, confidence=self.confidence.get(t, 0.0), confirmed=res.confirmed, R=res.recovery_R,
                      harm_followed=res.harm_followed, repeat=self.memory.is_repeat(t, now),
                      last_verdict=self.memory.last_verdict.get(t), **pre)
        v, why = classify(ev, self.vp)
        return self._verdict(now, t, v, why + ([res.detail] if res.detail else []), ev,
                             relocated=res.relocated, spare_before=res.spare_cores_before, readings=res.readings)

    def _verdict(self, now: float, t: str, v: str, why: List[str], ev: Evidence, *, relocated: bool,
                 spare_before: Optional[float] = None, readings: Optional[dict] = None) -> List[IncidentEvent]:
        self.judged[t] = v
        self.memory.note_verdict(t, v)
        name = f"{self.b.tenants[t].name} ({t})"
        ttl_r, ttl_q = self.cfg.prover.relocate_ttl_s, self.cfg.ladder.ttl_quarantine
        iid = self.inc.incident_id
        action = "none"
        home = RelocateCmd(tenant=t, target=None, ttl_s=ttl_r, incident_id=iid)
        if v == HONEST:
            if relocated:
                action = f"stays on the spare as a burst lane for {ttl_r:g} s (no penalty)"
            else:
                self._send_relocate(RelocateCmd(tenant=t, target="spare", ttl_s=ttl_r, incident_id=iid))
                action = f"moved to the spare as a burst lane for {ttl_r:g} s (no penalty)"
        elif v == COSTLY:
            cls = self.b.top_class(t, now)
            if self.budget.can_quarantine(now):
                self.budget.note_quarantine(now)
                self._send_relocate(RelocateCmd(tenant=t, cls=cls, target="quarantine", ttl_s=ttl_q, incident_id=iid))
                if cls:
                    self._send_relocate(home)
                    action = (f"request class {cls} (the one that grew) -> quarantine heavy lane for {ttl_q:g} s; "
                              f"the rest of its traffic goes home")
                else:
                    action = f"whole tenant -> quarantine heavy lane for {ttl_q:g} s (no per-class data)"
            else:
                if not relocated:
                    self._send_relocate(RelocateCmd(tenant=t, target="spare", ttl_s=ttl_r, incident_id=iid))
                action = (f"kept apart on the spare for {ttl_r:g} s (quarantine budget: one every "
                          f"{self.budget.quarantine_every_s:g} s)")
        elif v == OVER:
            if relocated:
                self._send_relocate(home)
            extra = self.b.force_throttle(t, now, f"verdict over_plan for {name}")
            action = (f"back on its own servers, limits tightened to {self.cfg.ladder.throttle_factor:.0%} of plan "
                      f"(the ladder relaxes it when calm)")
        else:  # NO_BLAME
            if relocated:
                self._send_relocate(home)
            action = "no action on the tenant; the rescue keeps its neighbours safe"
        left_spare = relocated and (v in (OVER, NO_BLAME) or (v == COSTLY and "quarantine" in action
                                                                and not action.startswith("kept")))
        if left_spare:
            self.jobs.append(("restore_spare", spare_before))
        e = self._emit(now, "verdict", action=action,
                       detail=f"Verdict {v} for {name}: " + "; ".join(why) + f". Action: {action}.",
                       report={"verdict": v, "evidence": {k: (round(x, 3) if isinstance(x, float) else x)
                                                          for k, x in vars(ev).items()},
                               "readings": readings or {}})
        e.tenant, e.verdict = t, v
        e.suspects = [Suspect(tenant=t, confidence=round(ev.confidence, 3))]
        return [e] + (extra if v == OVER else [])

    def on_host(self, res: HostResult, now: float) -> List[IncidentEvent]:
        subj = f"host:{res.server}"
        self.proving.pop(subj, None)
        if self.inc is None or res.incident_id != self.inc.incident_id:
            if res.drained:
                self._send_drain(res.server, False, None)
            self.budget.drained.pop(res.server, None)
            return []
        if not res.drained:
            self.budget.drained.pop(res.server, None)
            return self._note(now, subj, f"drain test for {res.server} failed: {res.detail}")
        v, why = host_verdict(res.idle_ratio, self.vp)
        self.judged[subj] = v
        if v == HOST:
            self.drains[res.server] = now
            action = f"{res.server} stays drained (TTL {self.cfg.prover.relocate_ttl_s:g} s, renewed while it is slow)"
        else:
            self._send_drain(res.server, False, self.inc.incident_id)
            self.budget.drained.pop(res.server, None)
            action = f"{res.server} undrained"
        e = self._emit(now, "verdict", action=action,
                       detail=f"Verdict {v} for {res.server}: {res.detail}; {why}. No tenant blamed. Action: {action}.",
                       report={"verdict": v, "server": res.server, "idle_ratio": res.idle_ratio})
        e.verdict = v
        return [e]

    def _renew_drains(self, now: float) -> List[IncidentEvent]:
        ttl = self.cfg.prover.relocate_ttl_s
        for srv, at in list(self.drains.items()):
            if now - at >= ttl / 4:
                self._send_drain(srv, True, self.inc.incident_id if self.inc else None)
                self.drains[srv] = now
        return []

    def _send_relocate(self, cmd: RelocateCmd) -> None:
        key = f"relocate:{cmd.tenant}:{cmd.cls or '*'}"
        self.control.append((key, "/control/relocate", cmd))

    def _send_drain(self, server: str, drained: bool, iid: Optional[str]) -> None:
        self.control.append((f"drain:{server}", "/control/drain",
                             DrainCmd(server=server, drained=drained, ttl_s=self.cfg.prover.relocate_ttl_s,
                                      incident_id=iid)))

    def status(self) -> str:
        """One sentence for the status line while a server incident is open ('' otherwise)."""
        if self.inc is None:
            return ""
        slow = self.health.slow_servers()
        if not slow:
            return ""
        out = f"{', '.join(slow)} slow ({', '.join(f'{self.health.servers[s].peer_ratio:.1f}x' for s in slow)} peers)"
        if self.plan and self.plan.weights:
            out += f": {len(self.plan.weights)} tenants routed around"
        if self.named_key:
            culprits, hosts = self.named_key
            if culprits:
                out += f"; named {', '.join(culprits)}"
            if hosts:
                out += f"; host suspect {', '.join(hosts)}"
        return out + "."

    def mark_routes(self, ok: bool, now: float) -> None:
        self.route_ok = ok
        self.route_sent_at = now

    # ------------------------------------------------------------- helpers
    def _equal(self, t: str) -> Dict[str, float]:
        """Default routing again, over the servers the gateway knows (the ones we routed with)."""
        servers = list(self.route_sent.get(t) or self.barcodes[t])
        return {s: round(1.0 / len(servers), 4) for s in servers}

    def _emit(self, now: float, stage: str, **kw) -> IncidentEvent:
        return self.b.book.emit(self.inc, now, stage, **kw)

    def _close(self, now: float, why: str, cmds: List[RouteWeightsCmd]) -> List[IncidentEvent]:
        out = []
        if self.route_sent:
            cmds.append(RouteWeightsCmd(weights={t: self._equal(t) for t in self.route_sent}, ttl_s=CLEAR_TTL_S,
                                        incident_id=self.inc.incident_id))
            out.append(self._emit(now, "resolved", action=f"RouteWeightsCmd cleared for "
                                  f"{', '.join(sorted(self.route_sent))}",
                                  detail=f"Route weights back to default: {why}."))
        for srv in list(self.drains):                   # host back to normal: undo our drains
            self._send_drain(srv, False, self.inc.incident_id)
            self.budget.drained.pop(srv, None)
            out.append(self._emit(now, "resolved", action=f"DrainCmd {srv} drained=false",
                                  detail=f"{srv} undrained: it is back within its peers' latency."))
        self.drains = {}
        dur = now - self.inc.opened_at
        named = self.named_key
        what = ""
        if named:
            what = (f" Named: {', '.join(named[0])}." if named[0] else "") + \
                   (f" Host suspects: {', '.join(named[1])}." if named[1] else "")
        out.append(self._emit(now, "closed", detail=f"Server incident closed after {dur:.0f} s: {why}.{what}"))
        self.b.book.close(self.inc, now)
        self.inc = None
        self.route_sent = {}
        self.route_ok = None
        self.plan = None
        self.reported_slow = self.reported_rescue = ()
        self.rescued_at = None
        self.named_key = self.pending_key = None
        self.pending_n = 0
        self.confidence, self.judged, self.noted = {}, {}, {}
        return out

    def _srv(self, s: str) -> str:
        h = self.health.servers[s]
        lvl = f"{h.level_ms:.0f} ms" if h.level_ms is not None else "?"
        peer = f"{h.peer_ms:.0f} ms" if h.peer_ms is not None else "?"
        healing = "" if h.slow_now or not h.slow else ", healing"
        return f"{s} {h.peer_ratio:.1f}x its peers ({lvl} vs {peer}, by {h.source}{healing})"

    def _slow_text(self, slow: List[str]) -> str:
        return "Slow servers: " + "; ".join(self._srv(s) for s in slow) + "."

    def _rescue_text(self, slow: List[str]) -> str:
        p = self.plan
        if not p or not p.weights:
            return (f"Nobody can be routed around {', '.join(slow)}"
                    + (f" (stuck: {', '.join(p.stuck)})" if p and p.stuck else "") + ".")
        parts = []
        for t, w in sorted(p.weights.items()):
            dest = ", ".join(f"{s} {v:.0%}" for s, v in w.items() if v > 0)
            parts.append(f"{t} -> {dest}" + (" (partial: capacity guard)" if t in p.capped else ""))
        txt = (f"Moved {len(p.weights)} tenants off {', '.join(slow)} onto their healthy servers, keeping every "
               f"server under {self.cfg.rescue_guard:.0%} CPU: " + "; ".join(parts) + ".")
        if p.stuck:
            txt += f" Stuck (every server slow): {', '.join(p.stuck)}."
        return txt

    def _named(self, now: float, d: Decoding) -> IncidentEvent:
        texts = []
        for c in d.culprits:
            servers = self.barcodes[c.tenant]
            ratios = ", ".join(f"{s} {self.health.servers[s].peer_ratio:.1f}x" for s in servers)
            why = [f"{len(c.covers)} of its {len(servers)} servers are slower than their peers ({ratios})"]
            if all(s in self.health.slow_servers() for s in servers):
                why.append("it cannot be routed around them")
            why += self.ledger.why(c.tenant)
            name = self.b.tenants[c.tenant].name
            texts.append(f"Named {name} ({c.tenant}) with {c.confidence:.0%} confidence: " + "; ".join(why) + ".")
        for s in d.host_suspects:
            texts.append(f"Host suspect {self._srv(s)}: no tenant's barcode explains it "
                         f"(host fault {d.top_confidence.get('host_fault', 0):.0%}). No tenant blamed.")
        if d.culprits and self.prover.available:
            texts.append("Proof by relocation is next (if confidence >= "
                         f"{self.vp.confidence_to_prove:.0%}).")
        elif d.culprits:
            texts.append("Not yet proven: relocation needs the Docker host (prover not available).")
        ev = self.b.book.emit(self.inc, now, "named", detail=" ".join(texts),
                              report={"p_slow": d.p, "confidence": d.top_confidence,
                                      "host_suspects": d.host_suspects,
                                      "culprits": [{"tenant": c.tenant, "confidence": c.confidence,
                                                    "covers": c.covers} for c in d.culprits]})
        ev.suspects = [Suspect(tenant=c.tenant, confidence=c.confidence) for c in d.culprits]
        if d.culprits:
            ev.tenant = d.culprits[0].tenant
        return ev


def _diff(a: Dict[str, float], b: Dict[str, float]) -> float:
    return max(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))
