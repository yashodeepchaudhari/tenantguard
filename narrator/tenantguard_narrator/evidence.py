"""Evidence packs (pure): everything a report may say, collected from the brain's event stream.

The pack is the single source of truth for the incident report; an anonymised subset of it (no tenant,
no project, no copied brain text) is the only input of the customer message.

Three kinds of case, as the brain tells them:
- tenant: the ladder's case about one tenant (id 'inc-...');
- split:  a hash split test of one project's unexplained CPU (id 'srv-...', rounds, maybe a culprit);
- host:   every active project slowed at once while each stayed within its allocation (id 'srv-...', no blame).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from tenantguard_common.contracts import IncidentEvent, TenantConfig

IST = timezone(timedelta(hours=5, minutes=30), "IST")
TRIGGERS = ("named", "verdict", "closed")
TTL_RE = re.compile(r"ttl=(\d+(?:\.\d+)?)s")


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")


@dataclass
class _Incident:
    incident_id: str
    events: List[IncidentEvent] = field(default_factory=list)
    seen: set = field(default_factory=set)
    closed: bool = False


def case_kind(incident_id: str, events: List[IncidentEvent]) -> str:
    """'tenant' | 'split' | 'host' (the same rule as the UI's Case File)."""
    if not incident_id.startswith("srv-"):
        return "tenant"
    host = any((e.report or {}).get("verdict") == "host_fault" or isinstance((e.report or {}).get("projects"), list)
               for e in events)
    return "host" if host else "split"


def _rounds(events: List[IncidentEvent]) -> List[dict]:
    """The split rounds, oldest first: one per `proving` event (its report is that round's evidence)."""
    out = []
    for e in events:
        r = e.report or {}
        if e.stage == "proving" and "round" in r:
            out.append({"round": r["round"], "halves": r.get("halves", []), "kept": r.get("kept", []),
                        "unattributed_ms_per_s": r.get("unattributed_ms_per_s", {})})
    return out


class EvidenceBook:
    """Keeps the events of every incident."""

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

    # ----- the pack -----
    def _name(self, t: Optional[str]) -> Optional[str]:
        cfg = self.tenants.get(t or "")
        return cfg.name if cfg else None

    def neighbours(self, t: Optional[str]) -> List[str]:
        """The tenants sharing the project (its replicas) with `t`."""
        me = self.tenants.get(t or "")
        if not me:
            return []
        return sorted(x.id for x in self.tenants.values() if x.id != me.id and x.project == me.project)

    def pack(self, incident_id: str) -> Optional[dict]:
        inc = self.incidents.get(incident_id)
        if inc is None or not inc.events:
            return None
        evs = inc.events
        kind = case_kind(incident_id, evs)
        culprit = next((e.tenant for e in reversed(evs) if e.tenant), None)
        suspects: Dict[str, float] = {}
        for e in evs:
            for s in e.suspects:
                suspects[s.tenant] = max(suspects.get(s.tenant, 0.0), s.confidence)
        if suspects and not culprit:
            culprit = max(suspects, key=suspects.get)
        verdict = next((e.verdict or (e.report or {}).get("verdict") for e in reversed(evs)
                        if e.verdict or (e.report or {}).get("verdict")), None)
        project = next(((e.report or {}).get("project") for e in evs if (e.report or {}).get("project")), None)
        if project is None and culprit in self.tenants:
            project = self.tenants[culprit].project
        projects = next(((e.report or {}).get("projects") for e in evs
                         if isinstance((e.report or {}).get("projects"), list)), [])
        levels = [e.level for e in evs if e.level]
        actions = [{"time": hhmmss(e.ts), "action": e.action,
                    "ttl_s": float(m.group(1)) if (m := TTL_RE.search(e.action or "")) else None}
                   for e in evs if e.action]
        start, end = evs[0].ts, evs[-1].ts
        return {
            "incident_id": incident_id,
            "kind": kind,
            "tenant": culprit,
            "tenant_name": self._name(culprit),
            "tier": self.tenants[culprit].tier if culprit in self.tenants else None,
            "project": project,
            "projects": sorted(projects),
            "opened": hhmmss(start),
            "last_event": hhmmss(end),
            "duration_s": round(end - start, 1),
            "closed": inc.closed,
            "stages": [{"time": hhmmss(e.ts), "stage": e.stage, "level": e.level, "detail": e.detail}
                       for e in evs],
            "suspects": [{"tenant": t, "name": self._name(t), "confidence_pct": round(c * 100)}
                         for t, c in sorted(suspects.items(), key=lambda kv: -kv[1])],
            "rounds": _rounds(evs) if kind == "split" else [],
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
    """What an affected customer may learn: when, how long, what was done. No tenant and no project is named
    (no free text from the brain is copied, so nothing can leak through it)."""
    kind, culprit = pack["kind"], pack.get("tenant")
    if kind == "host":
        cause = "the shared infrastructure was slower than usual"
    elif kind == "split" and culprit:
        cause = "another tenant sharing your capacity ran heavy background work"
    elif kind == "split":
        cause = "unexplained extra load on the capacity you share"
    else:
        cause = "another tenant sharing your capacity used far more than its fair share"
    limited = kind != "host" and bool(culprit) and any(a["action"] for a in pack["actions"])
    return {
        "incident_id": pack["incident_id"],
        "opened": pack["opened"],
        "last_event": pack["last_event"],
        "duration_s": pack["duration_s"],
        "closed": pack["closed"],
        "kind": kind,
        "cause": cause,
        "limited": limited,
        "resolved": any(s["stage"] in ("resolved", "closed") for s in pack["stages"]),
    }
