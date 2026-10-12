"""Orchestration: brain events -> evidence pack -> ReportView, republished whenever the incident changes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

from pydantic import ValidationError
from tenantguard_common.contracts import IncidentEvent, ReportView, StateSnapshot, TenantConfig

from . import template
from .evidence import TRIGGERS, EvidenceBook, anonymise

log = logging.getLogger("tenantguard.narrator")


def _digest(pack: dict) -> str:
    return hashlib.sha1(json.dumps(pack, sort_keys=True).encode()).hexdigest()


class Narrator:
    def __init__(self, tenants: Iterable[TenantConfig], max_reports: int = 200,
                 clock: Callable[[], float] = time.time):
        self.book = EvidenceBook(tenants, keep=max_reports)
        self.max_reports = max_reports
        self.clock = clock
        self.reports: Dict[str, ReportView] = {}
        self.digests: Dict[str, str] = {}
        self.subscribers: Set[asyncio.Queue] = set()
        self.stats = {"events": 0, "reports_published": 0}

    # ----- input -----
    def ingest(self, msg: dict) -> Optional[str]:
        """One brain message. Returns the incident_id if a report was (re)published. Never raises."""
        kind = msg.get("type") if isinstance(msg, dict) else None
        try:
            if kind == "state":
                self.book.add_snapshot(StateSnapshot.model_validate(msg))
                return None
            if kind != "incident":
                return None
            ev = IncidentEvent.model_validate(msg)
        except ValidationError as e:
            log.warning("bad brain message: %s", e.errors()[:1])
            return None
        self.stats["events"] += 1
        if not self.book.add_event(ev):
            return None
        if ev.stage in TRIGGERS or ev.incident_id in self.reports:
            return self.refresh(ev.incident_id)
        return None

    # ----- publishing -----
    def refresh(self, incident_id: str) -> Optional[str]:
        """(Re)build the report from the current evidence; publish only if the evidence changed."""
        pack = self.book.pack(incident_id)
        stage = self.book.trigger_stage(incident_id)
        if pack is None or stage is None:
            return None
        dig = _digest(pack)
        if self.digests.get(incident_id) == dig:
            return None
        self.digests[incident_id] = dig
        rep, cust = template.build(pack, anonymise(pack))
        now = self.clock()
        prev = self.reports.get(incident_id)
        self._publish(ReportView(
            incident_id=incident_id, tenant=pack.get("tenant"), trigger_stage=stage,
            version=(prev.version + 1) if prev else 1, report=rep, customer_message=cust, evidence=pack,
            created_at=prev.created_at if prev else now, updated_at=now))
        return incident_id

    def _publish(self, view: ReportView) -> None:
        self.reports[view.incident_id] = view
        while len(self.reports) > self.max_reports:
            oldest = min(self.reports.values(), key=lambda v: v.updated_at).incident_id
            self.reports.pop(oldest, None)
            self.digests.pop(oldest, None)
        self.stats["reports_published"] += 1
        msg = view.model_dump_json()
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    def listing(self) -> List[ReportView]:
        return sorted(self.reports.values(), key=lambda v: v.updated_at, reverse=True)
