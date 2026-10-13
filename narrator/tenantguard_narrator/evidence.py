"""Evidence packs (pure): everything a report may say, collected from the brain's event stream.

The pack is the single source of truth for the incident report; an anonymised subset of it (no tenant,
no server, no copied brain text) is the only input of the customer message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from tenantguard_common.contracts import IncidentEvent, StateSnapshot, TenantConfig

IST = timezone(timedelta(hours=5, minutes=30), "IST")
TRIGGERS = ("named", "verdict", "closed")
SERVER_RE = re.compile(r"\b(w\d+|spare|quarantine)\b")
TTL_RE = re.compile(r"ttl=(\d+(?:\.\d+)?)s")


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")


@dataclass
class _Incident:
    incident_id: str
    events: List[IncidentEvent] = field(default_factory=list)
    seen: set = field(default_factory=set)
    slow: Dict[str, float] = field(default_factory=dict)        # server -> max peer ratio seen while open
    closed: bool = False


class EvidenceBook:
    """Keeps the events of every incident and the slow servers seen while it was open."""

    def __init__(self, tenants: Iterable[TenantConfig], keep: int = 200):
        self.tenants = {t.id: t for t in tenants}
        self.incidents: Dict[str, _Incident] = {}
        self.keep = keep

    def add_event(self, ev: IncidentEvent) -> bool:
        """Store an event; True if it is new (replays from a WS reconnect or the backfill are ignored)."""
        inc = self.incidents.get(ev.incident_id)
        if inc is None:
            inc = self.incidents[ev.incident_id] = _Incident(ev.incident_id)
            while len(self.incidents) > self.keep:
                self.incidents.pop(next(iter(self.incidents)))
        key = (ev.stage, ev.ts, ev.level, ev.detail[:40])
        if key in inc.seen:
            return False
        inc.seen.add(key)
        inc.events.append(ev)
        inc.events.sort(key=lambda e: e.ts)
        if ev.stage == "closed":
            inc.closed = True
        return True

    def add_snapshot(self, snap: StateSnapshot) -> None:
        slow = {s.server: s.peer_ratio for s in snap.servers if s.status == "slow"}
        if not slow:
            return
        for inc in self.incidents.values():
            if not inc.closed:
                for s, r in slow.items():
                    inc.slow[s] = max(inc.slow.get(s, 0.0), round(r, 2))

    # ----- the pack -----
    def _name(self, t: Optional[str]) -> Optional[str]:
        cfg = self.tenants.get(t or "")
        return cfg.name if cfg else None

    def neighbours(self, t: Optional[str]) -> List[str]:
        me = self.tenants.get(t or "")
        if not me:
            return []
        return sorted(x.id for x in self.tenants.values() if x.id != me.id and x.project == me.project)

    def pack(self, incident_id: str) -> Optional[dict]:
        inc = self.incidents.get(incident_id)
        if inc is None or not inc.events:
            return None
        evs = inc.events
        culprit = next((e.tenant for e in reversed(evs) if e.tenant), None)
        suspects: Dict[str, float] = {}
        for e in evs:
            for s in e.suspects:
                suspects[s.tenant] = max(suspects.get(s.tenant, 0.0), s.confidence)
        if suspects and not culprit:
            culprit = max(suspects, key=suspects.get)
        verdict = next((e.verdict for e in reversed(evs) if e.verdict), None)
        levels = [e.level for e in evs if e.level]
        actions = [{"time": hhmmss(e.ts), "action": e.action,
                    "ttl_s": float(m.group(1)) if (m := TTL_RE.search(e.action or "")) else None}
                   for e in evs if e.action]
        kind = "server" if incident_id.startswith("srv-") else "tenant"
        slow: Dict[str, Optional[float]] = {}
        if kind == "server":                            # a tenant (ladder) incident is about usage, not servers
            slow = dict(inc.slow)
            for e in evs:                               # only the brain's own list of slow servers counts:
                if e.stage == "detected":               # other details also name rescue targets (healthy ones)
                    for s in SERVER_RE.findall(e.detail or ""):
                        slow.setdefault(s, None)
        start, end = evs[0].ts, evs[-1].ts
        return {
            "incident_id": incident_id,
            "kind": kind,
            "tenant": culprit,
            "tenant_name": self._name(culprit),
            "tier": self.tenants[culprit].tier if culprit in self.tenants else None,
            "opened": hhmmss(start),
            "last_event": hhmmss(end),
            "duration_s": round(end - start, 1),
            "closed": inc.closed,
            "stages": [{"time": hhmmss(e.ts), "stage": e.stage, "level": e.level, "detail": e.detail}
                       for e in evs],
            "suspects": [{"tenant": t, "name": self._name(t), "confidence_pct": round(c * 100)}
                         for t, c in sorted(suspects.items(), key=lambda kv: -kv[1])],
            "slow_servers": [{"server": s, "peer_ratio": r} for s, r in sorted(slow.items())],
            "actions": actions,
            "peak_level": _peak(levels),
            "affected": self.neighbours(culprit),
            "verdict": verdict,
        }

    def trigger_stage(self, incident_id: str) -> Optional[str]:
        inc = self.incidents.get(incident_id)
        last = next((e.stage for e in reversed(inc.events) if e.stage in TRIGGERS), None) if inc else None
        return last


_ORDER = ["normal", "warn", "throttle", "deprioritize", "quarantine", "blocked"]


def _peak(levels: List[str]) -> Optional[str]:
    return max(levels, key=_ORDER.index) if levels else None


def anonymise(pack: dict) -> dict:
    """What an affected customer may learn: when, how long, what was done. No tenant and no server is named
    (no free text from the brain is copied, so nothing can leak through it)."""
    return {
        "incident_id": pack["incident_id"],
        "opened": pack["opened"],
        "last_event": pack["last_event"],
        "duration_s": pack["duration_s"],
        "closed": pack["closed"],
        "cause": "another tenant on shared servers used far more than its fair share"
                 if pack.get("tenant") else "some servers were slower than their peers",
        "rescued": any(s["stage"] == "rescued" for s in pack["stages"]),
        "actions": ["traffic was moved to healthy servers" if "RouteWeights" in (a["action"] or "")
                    else "the cause was limited automatically" for a in pack["actions"]],
        "resolved": any(s["stage"] in ("resolved", "closed") for s in pack["stages"]),
    }
