"""Orchestration: brain events -> evidence pack -> template ReportView (instant) -> AI ReportView (async).

Publishing never waits for the AI. Each AI run is tied to the pack it was given: if the incident changed
meanwhile (a newer pack), the stale AI result is dropped.
"""
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
from .ai import AIWriter
from .evidence import TRIGGERS, EvidenceBook, anonymise
from .grounding import check

log = logging.getLogger("tenantguard.narrator")


def _digest(pack: dict) -> str:
    return hashlib.sha1(json.dumps(pack, sort_keys=True).encode()).hexdigest()


class Narrator:
    def __init__(self, tenants: Iterable[TenantConfig], writer: Optional[AIWriter] = None,
                 max_parallel_ai: int = 2, clock: Callable[[], float] = time.time):
        tenants = list(tenants)
        self.book = EvidenceBook(tenants)
        self.names = {t.id: t.name for t in tenants}
        self.writer = writer
        self.clock = clock
        self.reports: Dict[str, ReportView] = {}
        self.digests: Dict[str, str] = {}
        self.subscribers: Set[asyncio.Queue] = set()
        self.sem = asyncio.Semaphore(max_parallel_ai)
        self.tasks: Set[asyncio.Task] = set()
        self.stats = {"events": 0, "templates": 0, "ai_done": 0, "ai_rejected": 0, "ai_error": 0}

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
        """(Re)build the template version now; schedule the AI version if enabled."""
        pack = self.book.pack(incident_id)
        stage = self.book.trigger_stage(incident_id)
        if pack is None or stage is None:
            return None
        dig = _digest(pack)
        if self.digests.get(incident_id) == dig:
            return None
        self.digests[incident_id] = dig
        anon = anonymise(pack)
        rep, cust = template.build(pack, anon)
        now = self.clock()
        prev = self.reports.get(incident_id)
        view = ReportView(
            incident_id=incident_id, tenant=pack.get("tenant"), trigger_stage=stage,
            version=(prev.version + 1) if prev else 1, source="template", grounded=True,
            ai_status="pending" if self.writer else "disabled",
            ai_detail="" if self.writer else "no API key: template only",
            report=rep, customer_message=cust, evidence=pack,
            created_at=prev.created_at if prev else now, updated_at=now)
        self._publish(view)
        self.stats["templates"] += 1
        if self.writer:
            self._spawn(self._ai(incident_id, pack, anon, dig))
        return incident_id

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:                     # no loop (pure tests): AI skipped
            coro.close()
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _ai(self, incident_id: str, pack: dict, anon: dict, dig: str) -> None:
        async with self.sem:
            if self.digests.get(incident_id) != dig:
                return                           # superseded before we started
            res = await self.writer.write(pack, anon)
        if self.digests.get(incident_id) != dig:
            return                               # superseded while the model was writing
        cur = self.reports[incident_id]
        if res.status != "done":
            self.stats["ai_error"] += 1
            self._publish(cur.model_copy(update={"ai_status": "error", "ai_detail": res.detail,
                                                 "version": cur.version + 1, "updated_at": self.clock()}))
            return
        ok, why = check(res.report, res.customer, pack, anon, self.names)
        if not ok:
            self.stats["ai_rejected"] += 1
            log.info("AI report for %s rejected: %s", incident_id, why)
            self._publish(cur.model_copy(update={"ai_status": "rejected", "ai_detail": why, "model": res.model,
                                                 "version": cur.version + 1, "updated_at": self.clock()}))
            return
        self.stats["ai_done"] += 1
        self._publish(cur.model_copy(update={
            "source": "ai", "grounded": True, "ai_status": "done", "ai_detail": "", "model": res.model,
            "report": res.report, "customer_message": res.customer,
            "version": cur.version + 1, "updated_at": self.clock()}))

    def _publish(self, view: ReportView) -> None:
        self.reports[view.incident_id] = view
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

    async def drain(self) -> None:
        """Wait for in-flight AI writes (tests, shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
