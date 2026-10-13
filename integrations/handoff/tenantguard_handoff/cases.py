"""The case book (pure: no I/O, `now` passed in).

- brain IncidentEvent  -> create / update a CaseView (one per incident_id; case_id INC-NNNN assigned here)
- Web owner / decision -> updated CaseView + the control commands to send (executed by bridge.py)
- deadlines            -> expire pending actions (TenantGuard's safe default: TTL auto-undo)
- CaseView             -> the partner's wire Case (HH:MM:SS strings appear only there)
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel
from tenantguard_common.contracts import (BlockCmd, CaseAction, CaseContext, CaseHistoryItem, CaseView,
                                          IncidentEvent, OperatorCmd, TenantConfig)

from . import wire
from .settings import HandoffSettings
from .timefmt import from_ist, ist

SEVERITY = {"normal": "low", "warn": "low", "throttle": "medium", "deprioritize": "medium",
            "quarantine": "high", "blocked": "high"}
SEV_RANK = {"low": 0, "medium": 1, "high": 2}
VERDICTS = set(wire.Verdict.__args__)  # type: ignore[attr-defined]


def _finding(ev: IncidentEvent) -> str | None:
    """'host' | 'split' for the brain's findings (incident id 'srv-...'), None for a tenant's ladder case."""
    if not ev.incident_id.startswith("srv-"):
        return None
    r = ev.report or {}
    return "host" if r.get("verdict") == "host_fault" or isinstance(r.get("projects"), list) else "split"
HOLD_LEVELS = ("throttle", "deprioritize", "quarantine", "blocked")
HOLD_RANK = {lvl: i for i, lvl in enumerate(HOLD_LEVELS)}
STAGE_TEXT = {"detected": "Detected", "level_change": "Level", "rescued": "Victims rescued", "named": "Named",
              "proving": "Proving", "verdict": "Verdict", "resolved": "Resolved", "explained": "Explained",
              "closed": "Closed"}


@dataclass
class Command:
    """A control call the bridge must make: target 'brain' (POST /api/operator) or 'gateway' (POST /control/...)."""
    target: str
    path: str
    body: BaseModel


@dataclass
class _Case:
    view: CaseView
    version: int = 0
    kinds: Dict[str, str] = field(default_factory=dict)      # action_id -> "hold" | "block"
    level: str = "normal"
    block_applied: bool = False
    seen: set = field(default_factory=set)                   # (stage, ts) of events already applied
    closed_at: Optional[float] = None                        # first resolved/closed time (eviction clock)
    dropped: int = 0                                          # history items dropped from memory (history_keep)


class DecisionError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


class CaseBook:
    def __init__(self, settings: HandoffSettings, tenants: Iterable[TenantConfig], next_seq: int = 1,
                 id_map: Optional[Dict[str, str]] = None):
        self.s = settings
        self.tenants = {t.id: t for t in tenants}
        self.next_seq = next_seq
        self.by_incident: "OrderedDict[str, str]" = OrderedDict(id_map or {})
        self.cases: Dict[str, _Case] = {}
        self.retired: "OrderedDict[str, None]" = OrderedDict()  # evicted incidents: replays are ignored

    # ----- helpers -----
    def _name(self, tenant: Optional[str]) -> str:
        t = self.tenants.get(tenant or "")
        return f"{t.name} ({t.id})" if t else (tenant or "unknown tenant")

    def neighbours(self, tenant: Optional[str]) -> List[str]:
        """The other tenants of `tenant`'s project: they share its replicas, so they are the potential victims."""
        me = self.tenants.get(tenant or "")
        if not me:
            return []
        return sorted(t.id for t in self.tenants.values() if t.id != me.id and t.project == me.project)

    def _history(self, c: _Case, now: float, text: str) -> None:
        h = c.view.history
        h.append(CaseHistoryItem(at=now, time_ist=ist(now), event=text))
        extra = len(h) - self.s.history_keep
        if extra > 0:
            del h[:extra]
            c.dropped += extra

    def _touch(self, c: _Case, now: float) -> None:
        v = c.view
        if v.status not in ("resolved", "closed"):
            v.status = "waiting_for_decision" if any(a.state == "pending" for a in v.actions) else "in_progress"
        v.updated_at = now
        c.version += 1

    def _new_action(self, c: _Case, kind: str, text: str, deadline: float) -> CaseAction:
        a = CaseAction(action_id=f"A{len(c.view.actions) + 1}", text=text, deadline_at=deadline,
                       deadline_ist=ist(deadline))
        c.view.actions.append(a)
        c.kinds[a.action_id] = kind
        return a

    def _pending(self, c: _Case, kind: str) -> Optional[CaseAction]:
        return next((a for a in c.view.actions if a.state == "pending" and c.kinds.get(a.action_id) == kind), None)

    def _active_hold(self, c: _Case) -> Optional[CaseAction]:
        """A hold the on-call still has to decide (pending) or already put in force (extended)."""
        return next((a for a in c.view.actions
                     if c.kinds.get(a.action_id) == "hold" and a.state in ("pending", "extended")), None)

    @staticmethod
    def _escalated(prev: str, level: str) -> bool:
        """Entering a hold level, or going up between hold levels (renewals and recovery steps are not)."""
        return prev not in HOLD_RANK or HOLD_RANK[level] > HOLD_RANK[prev]

    def _cap_id_map(self) -> None:
        live = set(self.cases)
        for iid in list(self.by_incident):
            if len(self.by_incident) <= self.s.id_map_max:
                break
            if self.by_incident[iid] not in live:
                del self.by_incident[iid]

    def get(self, case_id: str) -> Optional[CaseView]:
        c = self.cases.get(case_id)
        return c.view if c else None

    def version(self, case_id: str) -> int:
        return self.cases[case_id].version

    # ----- brain -> case -----
    def on_event(self, ev: IncidentEvent, now: float) -> Optional[str]:
        """Apply one incident event; returns the case_id if the case changed."""
        if ev.incident_id in self.retired:
            return None                                   # evicted case: a WS backfill must not resurrect it
        case_id = self.by_incident.get(ev.incident_id)
        c = self.cases.get(case_id) if case_id else None
        if c is None:
            if case_id is None:
                case_id = f"INC-{self.next_seq:04d}"
                self.next_seq += 1
                self.by_incident[ev.incident_id] = case_id
                self._cap_id_map()
            name = self._name(ev.tenant)
            finding = _finding(ev)
            if finding == "host":
                title = "All active projects slowed down together: the cause is outside them, nobody is blamed"
            elif finding == "split":
                title = f"Hidden CPU in project {(ev.report or {}).get('project', '?')}: hash split test"
            else:
                title = f"{name} is using more than its fair share and slowing its neighbours"
            c = _Case(view=CaseView(
                case_id=case_id, incident_id=ev.incident_id, tenant=ev.tenant or None,
                title=title, severity="medium" if finding else "low", status="in_progress",
                context=CaseContext(verdict="over_plan", affected=self.neighbours(ev.tenant),
                                    evidence_link=f"{self.s.ui_url.rstrip('/')}/case/{ev.incident_id}",
                                    summary=ev.detail or f"Incident opened for {name}."),
                created_at=now, updated_at=now))
            self.cases[case_id] = c
        key = (ev.stage, ev.ts, ev.level)
        if key in c.seen:
            return None                                   # replayed event (e.g. after a WS reconnect)
        c.seen.add(key)
        v, name = c.view, self._name(ev.tenant)

        label = STAGE_TEXT.get(ev.stage, ev.stage)
        if ev.stage == "level_change" and ev.level:
            label = f"Level {ev.prev_level or c.level} -> {ev.level}"
        self._history(c, now, f"{label}: {ev.detail}" if ev.detail else label)
        if ev.detail:
            v.context.summary = ev.detail
        verdict = ev.verdict or (ev.report or {}).get("verdict")     # findings carry it in their report
        if verdict in VERDICTS:
            v.context.verdict = verdict
            self._history(c, now, f"Verdict: {verdict.replace('_', ' ')}")

        if ev.stage == "verdict" and not v.tenant and ev.suspects:
            self._on_culprit(c, ev)

        if ev.level:
            prev, c.level = c.level, ev.level
            sev = SEVERITY.get(ev.level, "low")
            if SEV_RANK[sev] > SEV_RANK[v.severity]:
                v.severity = sev
            self._on_level(c, ev.level, prev, name, now)

        if ev.stage == "resolved":
            self._expire_all(c, now, "incident resolved by TenantGuard")
            v.status = "resolved"
            c.closed_at = c.closed_at or now
        elif ev.stage == "closed":
            self._expire_all(c, now, "incident closed")
            v.status = "closed"
            c.closed_at = c.closed_at or now
        self._touch(c, now)
        return case_id

    def _on_level(self, c: _Case, level: str, prev: str, name: str, now: float) -> None:
        """New decision requests only when the tenant escalates. The brain re-emits the current level on every
        ladder step, recovery step and operator echo; those only refresh a pending hold, never add one."""
        hold = self._pending(c, "hold")
        if level in HOLD_LEVELS:
            ttl = self.s.ttl_quarantine_s if level in ("quarantine", "blocked") else self.s.ttl_throttle_s
            deadline = now + ttl
            text = (f"{name} is at level '{level}'. TenantGuard auto-reverts it at {ist(deadline)} IST unless "
                    f"renewed. Extend (hold it {self.s.extend_s:.0f} s longer) or release (back to its full plan)?")
            if hold:
                hold.text, hold.deadline_at, hold.deadline_ist = text, deadline, ist(deadline)
            elif self._active_hold(c) is None and self._escalated(prev, level):
                self._new_action(c, "hold", text, deadline)
            if (level == "quarantine" and prev != "quarantine" and not self._pending(c, "block")
                    and not c.block_applied):
                d = now + self.s.decision_window_s
                self._new_action(c, "block", f"Approve blocking {name} entirely for {self.s.block_s:.0f} s "
                                             f"(TTL-bound, auto-undo). Reject keeps the quarantine limits only.", d)
        elif hold:                                         # back to warn / normal: nothing left to hold
            hold.state = "expired"
            self._history(c, now, f"{hold.action_id} no longer needed: {name} is back to '{level}'")

    def _on_culprit(self, c: _Case, ev: IncidentEvent) -> None:
        """A hash split found its culprit: adopt it. No decision is asked here: the split case closes at once,
        and the hidden CPU is now charged to the culprit, so the ladder's own case for it asks if needed."""
        top = max(ev.suspects, key=lambda x: x.confidence)
        if top.confidence < self.s.confidence_to_name:
            return
        v = c.view
        v.tenant = top.tenant
        project = (ev.report or {}).get("project", "?")
        v.title = f"{self._name(top.tenant)} found behind project {project}'s hidden CPU ({top.confidence:.0%} confidence)"
        v.context.affected = self.neighbours(top.tenant)

    def _expire_all(self, c: _Case, now: float, why: str) -> None:
        for a in c.view.actions:
            if a.state == "pending":
                a.state = "expired"
                self._history(c, now, f"{a.action_id} closed without a decision: {why}")

    # ----- retention -----
    def evict(self, now: float) -> List[str]:
        """Drop cases closed/resolved more than retention_s ago; their incidents are retired (replays ignored)."""
        gone = [cid for cid, c in self.cases.items()
                if c.closed_at is not None and c.view.status in ("resolved", "closed")
                and now - c.closed_at >= self.s.retention_s]
        for cid in gone:
            c = self.cases.pop(cid)
            self.retired[c.view.incident_id] = None
            while len(self.retired) > self.s.id_map_max:
                self.retired.popitem(last=False)
        if gone:
            self._cap_id_map()
        return gone

    # ----- deadlines -----
    def expire(self, now: float) -> List[str]:
        """Pending actions past their deadline get TenantGuard's safe default (timer auto-undo)."""
        changed = []
        for cid, c in self.cases.items():
            hit = False
            for a in c.view.actions:
                if a.state == "pending" and a.deadline_at <= now:
                    a.state = "expired"
                    self._history(c, now, f"No decision on {a.action_id} by {a.deadline_ist}: "
                                          f"safe default applied (timers auto-undo)")
                    hit = True
            if hit:
                self._touch(c, now)
                changed.append(cid)
        return changed

    # ----- Web -> TenantGuard -----
    def on_owner(self, upd: wire.CaseUpdateOwner, now: float) -> str:
        c = self.cases.get(upd.case_id)
        if c is None:
            raise DecisionError(404, f"unknown case {upd.case_id}")
        at = from_ist(upd.time, now)
        prev = c.view.owner
        c.view.owner, c.view.owner_since_at, c.view.owner_since_ist = upd.owner, at, ist(at)
        self._history(c, now, f"Owner: {upd.owner} acknowledged" + (f" (handed over from {prev})" if prev else ""))
        self._touch(c, now)
        return upd.case_id

    def on_decision(self, upd: wire.CaseUpdateDecision, now: float) -> Tuple[str, List[Command]]:
        c = self.cases.get(upd.case_id)
        if c is None:
            raise DecisionError(404, f"unknown case {upd.case_id}")
        v, t = c.view, c.view.tenant or ""
        if upd.decision == "resolve":
            return upd.case_id, self._resolve(c, upd, now)
        a = next((x for x in v.actions if x.action_id == upd.action_id), None)
        if a is None:
            raise DecisionError(404, f"unknown action {upd.action_id} in {upd.case_id}")
        kind = c.kinds.get(a.action_id)
        allowed = {"hold": {"pending": {"approve", "reject", "extend", "release"}, "extended": {"extend", "release"}},
                   "block": {"pending": {"approve", "reject"}, "approved": {"extend", "release"}}}
        if upd.decision not in allowed.get(kind, {}).get(a.state, set()):
            raise DecisionError(409, f"{upd.action_id} is {a.state}; '{upd.decision}' is not possible now")
        cmds: List[Command] = []
        hold_s = min(self.s.extend_s, self.s.ttl_max_s)
        name = self._name(t)
        if kind == "hold":
            if upd.decision in ("approve", "extend"):
                cmds.append(Command("brain", "/api/operator", OperatorCmd(
                    tenant=t, decision="extend", until_s=hold_s, by=upd.by, case_id=v.case_id)))
                a.state = "extended"
                a.deadline_at = max(a.deadline_at, now) + hold_s
                a.deadline_ist = ist(a.deadline_at)
                self._history(c, now, f"{upd.by}: {upd.decision} {a.action_id}: hold {name} at its current level "
                                      f"{hold_s:.0f} s longer")
            elif upd.decision == "release":
                cmds.append(Command("brain", "/api/operator", OperatorCmd(
                    tenant=t, decision="release", until_s=hold_s, by=upd.by, case_id=v.case_id)))
                a.state = "released"
                self._history(c, now, f"{upd.by}: release {a.action_id}: {name} back to its full plan "
                                      f"(no re-escalation for {hold_s:.0f} s)")
            else:
                a.state = "rejected"
                self._history(c, now, f"{upd.by}: reject {a.action_id}: no change; timers auto-undo as planned")
        else:   # block
            block_s = min(self.s.block_s, self.s.ttl_max_s)
            if upd.decision in ("approve", "extend"):
                cmds.append(Command("gateway", "/control/block", BlockCmd(
                    tenant=t, blocked=True, ttl_s=block_s, incident_id=v.incident_id)))
                c.block_applied = True
                a.state = "approved"
                a.deadline_at = now + block_s
                a.deadline_ist = ist(a.deadline_at)
                self._history(c, now, f"{upd.by}: {upd.decision} {a.action_id}: {name} blocked until "
                                      f"{a.deadline_ist} (auto-undo)")
            elif upd.decision == "release":
                cmds.append(Command("gateway", "/control/block", BlockCmd(
                    tenant=t, blocked=False, incident_id=v.incident_id)))
                c.block_applied = False
                a.state = "released"
                self._history(c, now, f"{upd.by}: release {a.action_id}: block on {name} lifted")
            else:
                a.state = "rejected"
                self._history(c, now, f"{upd.by}: reject {a.action_id}: no block; quarantine limits stay")
        a.decided_by, a.decided_at = upd.by, now
        self._touch(c, now)
        return upd.case_id, cmds

    def _resolve(self, c: _Case, upd: wire.CaseUpdateDecision, now: float) -> List[Command]:
        v, t = c.view, c.view.tenant or ""
        cmds: List[Command] = []
        if c.block_applied:
            cmds.append(Command("gateway", "/control/block", BlockCmd(tenant=t, blocked=False,
                                                                      incident_id=v.incident_id)))
            c.block_applied = False
        if any(c.kinds.get(a.action_id) == "hold" and a.state == "extended" for a in v.actions):
            cmds.append(Command("brain", "/api/operator", OperatorCmd(
                tenant=t, decision="resume", until_s=1.0, by=upd.by, case_id=v.case_id)))
        for a in v.actions:
            if a.state in ("pending", "extended", "approved"):
                a.state = "resolved"
                a.decided_by, a.decided_at = upd.by, now
        v.status = "resolved"
        self._history(c, now, f"{upd.by}: resolved the case; bridge-applied controls cleared")
        self._touch(c, now)
        return cmds

    def note(self, case_id: str, now: float, text: str) -> None:
        """Record the outcome of a control call in the case history."""
        c = self.cases.get(case_id)
        if c:
            self._history(c, now, text)
            self._touch(c, now)

    def mark_delivered(self, case_id: str, version: int, at: float) -> None:
        c = self.cases.get(case_id)
        if c and version == c.version:
            c.view.delivered_at = at

    # ----- internal -> wire -----
    def to_wire(self, case_id: str) -> wire.Case:
        c = self.cases[case_id]
        v = c.view
        keep = max(1, self.s.history_wire_max)
        hidden = c.dropped + max(0, len(v.history) - keep)
        history = [wire.HistoryItem(time=h.time_ist, event=h.event) for h in v.history[-keep:]]
        if hidden:
            history.insert(0, wire.HistoryItem(time=ist(v.created_at), event=f"{hidden} earlier events"))
        return wire.Case(
            case_id=v.case_id, title=v.title, severity=v.severity, status=v.status,
            context=wire.CaseContext(
                verdict=v.context.verdict,
                affected=[t for t in v.context.affected if t in wire.TENANT_IDS],
                evidence_link=v.context.evidence_link, summary=v.context.summary),
            pending_actions=[wire.PendingAction(action_id=a.action_id, text=a.text, deadline=a.deadline_ist)
                             for a in v.actions if a.state == "pending"],
            history=history)

    # ----- persistence of ids (cases themselves are rebuilt from the brain stream) -----
    def id_state(self) -> dict:
        return {"next_seq": self.next_seq, "by_incident": dict(self.by_incident)}
