"""Stage 3 loop: server health -> rescue (route-around) -> barcode decoder -> name (no I/O).

A *server incident* opens when any server turns slow (debounced, see health.py):

    detected   which servers are slow and by how much
    rescued    RouteWeightsCmd moving victims off the slow servers (capped by rescue_capacity_guard)
    named      the decoder's verdict, once it has been stable for open_after_ticks ticks:
               culprit tenant(s) with confidence, or host suspect(s) with no tenant blamed
    resolved   every server healthy again: route weights cleared
    closed

Routing changes and incidents happen only in `tenantguard` mode; health and decoding always run
(they feed ServerView and the status sentence). Proving a named culprit (relocation to the spare)
is the prover's job: see prover.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from tenantguard_common.contracts import IncidentEvent, RouteWeightsCmd, TelemetryFrame

from .decoder import Decoding, decode
from .health import HealthTracker
from .incidents import Incident
from .prover import NullProver, Prover
from .rescue import RescuePlan, plan_rescue

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
        self.health = HealthTracker(servers, cfg.health)
        self.barcodes = {t: list(c.servers) for t, c in brain.tenants.items()}
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
        self.bg_base = Ewma()
        self.bb_base = Ewma()

    # ------------------------------------------------------------- inputs
    def on_frame(self, frame: TelemetryFrame, now: float) -> None:
        # per server: the MEDIAN of its tenants' cell p95, so a server is slow only when most of its
        # tenants are slow there; one tenant's own heavy requests (a whale, a batch export) don't count
        acc: Dict[str, Tuple[List[float], int]] = {}
        for c in frame.cells:
            if c.count > 0 and c.p95_ms > 0:
                vals, n = acc.get(c.server, ([], 0))
                vals.append(c.p95_ms)
                acc[c.server] = (vals, n + c.count)
        self.health.observe({s: (median(v), n) for s, (v, n) in acc.items()}, now)

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
        if not slow:                        # baselines learn only while every server is healthy
            for t, r in bg_rate.items():
                self.bg_base.update(t, r)
            for s, r in bb_rate.items():
                self.bb_base.update(s, r)

    # ------------------------------------------------------------- tick
    def tick(self, now: float, acting: bool) -> Tuple[List[IncidentEvent], List[RouteWeightsCmd]]:
        slow = self.health.slow_servers()
        self._update_ledger(now, slow)
        stuck = {t: all(s in slow for s in v) for t, v in self.barcodes.items()} if slow else {}
        self.decoding = decode(self.barcodes, self.health.ratios(), stuck=stuck,
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
        return events, cmds

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
        self.named_key = self.pending_key = None
        self.pending_n = 0
        return out

    def _srv(self, s: str) -> str:
        h = self.health.servers[s]
        lvl = f"{h.level_ms:.0f} ms" if h.level_ms is not None else "?"
        peer = f"{h.peer_ms:.0f} ms" if h.peer_ms is not None else "?"
        return f"{s} {h.peer_ratio:.1f}x its peers ({lvl} vs {peer}, by {h.source})"

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
        from tenantguard_common.contracts import Suspect
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
            texts.append("Proof by relocation is next.")
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
