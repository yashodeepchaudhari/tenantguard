"""Incident book-keeping (pure, no I/O). One open incident per tenant at a time."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from tenantguard_common.contracts import IncidentEvent, Suspect


@dataclass
class Incident:
    incident_id: str
    tenant: str
    opened_at: float
    level: str = "normal"
    peak: str = "normal"
    closed_at: Optional[float] = None
    events: List[IncidentEvent] = field(default_factory=list)


class IncidentBook:
    def __init__(self, keep_events: int = 1000, keep_closed: int = 200):
        self.open: Dict[str, Incident] = {}             # tenant -> incident
        self.findings: Dict[str, Incident] = {}         # finding key ("split:<project>", "contention") -> its case
        self.by_id: Dict[str, Incident] = {}
        self.log: Deque[IncidentEvent] = deque(maxlen=keep_events)
        self._closed: Deque[str] = deque()
        self._keep_closed = keep_closed
        self._seq = 0

    def open_for(self, tenant: str, now: float) -> Incident:
        inc = self.open.get(tenant)
        if inc is None:
            self._seq += 1
            iid = f"inc-{int(now)}-{tenant}-{self._seq}"
            inc = Incident(incident_id=iid, tenant=tenant, opened_at=now)
            self.open[tenant] = inc
            self.by_id[iid] = inc
        return inc

    def open_finding(self, key: str, now: float) -> Incident:
        """The open case of a control-loop finding (a hash split of one project, a host-wide slowdown), not
        tied to one tenant: id 'srv-...'. Every event of the same finding goes into the same case until it closes."""
        inc = self.findings.get(key)
        if inc is None:
            self._seq += 1
            inc = Incident(incident_id=f"srv-{int(now)}-{self._seq}", tenant="", opened_at=now)
            self.findings[key] = inc
            self.by_id[inc.incident_id] = inc
        return inc

    def emit(self, inc: Incident, now: float, stage: str, *, level: Optional[str] = None,
             prev_level: Optional[str] = None, action: Optional[str] = None, detail: str = "",
             confidence: Optional[float] = None, report: Optional[dict] = None,
             tenant: Optional[str] = None) -> IncidentEvent:
        """`tenant` names the suspect of a finding case (which has no tenant of its own)."""
        who = tenant or inc.tenant
        ev = IncidentEvent(
            incident_id=inc.incident_id, stage=stage, ts=now, tenant=who or None,
            level=level, prev_level=prev_level, action=action, detail=detail, report=report,
            suspects=[Suspect(tenant=who, confidence=round(confidence, 3))] if confidence is not None else [],
        )
        if level is not None:
            inc.level = level
        inc.events.append(ev)
        self.log.append(ev)
        return ev

    def close(self, inc: Incident, now: float) -> None:
        inc.closed_at = now
        if self.open.get(inc.tenant) is inc:
            self.open.pop(inc.tenant, None)
        for key in [k for k, v in self.findings.items() if v is inc]:
            del self.findings[key]
        self._closed.append(inc.incident_id)
        while len(self._closed) > self._keep_closed:
            self.by_id.pop(self._closed.popleft(), None)

    def events(self, *, incident_id: Optional[str] = None, tenant: Optional[str] = None,
               limit: int = 200) -> List[IncidentEvent]:
        evs = list(self.log)
        if incident_id:
            evs = [e for e in evs if e.incident_id == incident_id]
        if tenant:
            evs = [e for e in evs if e.tenant == tenant]
        return evs[-limit:] if limit > 0 else evs
