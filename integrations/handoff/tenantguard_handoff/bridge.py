"""I/O around the case book: brain WS in, Web app out (outbox), control calls, deadline ticks, id persistence."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, List, Optional

import httpx
import websockets
from pydantic import ValidationError
from tenantguard_common.contracts import H_CONTROL_TOKEN, IncidentEvent, StateSnapshot, TenantConfig

from . import wire
from .cases import CaseBook, Command
from .outbox import Outbox
from .settings import HandoffSettings

log = logging.getLogger("tenantguard.handoff")

RECONNECT_START_S = 0.5
RECONNECT_MAX_S = 10.0


class Bridge:
    def __init__(self, settings: HandoffSettings, tenants: Iterable[TenantConfig],
                 control_client: Optional[httpx.AsyncClient] = None, web_client: Optional[httpx.AsyncClient] = None):
        self.s = settings
        ids = self._load_ids()
        self.book = CaseBook(settings, tenants, next_seq=ids.get("next_seq", 1), id_map=ids.get("by_incident"))
        self.outbox = Outbox(settings.web_url, client=web_client, timeout_s=settings.push_timeout_s,
                             on_delivered=self.book.mark_delivered)
        self.control = control_client or httpx.AsyncClient(timeout=settings.push_timeout_s)
        self.mode: Optional[str] = None
        self.brain_connected = False
        self.events_seen = 0
        self.last_event_at: Optional[float] = None
        self._tasks: List[asyncio.Task] = []

    # ----- id persistence (so a restart never re-uses a case_id at the Web app) -----
    def _load_ids(self) -> dict:
        try:
            return json.loads(Path(self.s.state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_ids(self) -> None:
        p = Path(self.s.state_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.book.id_state()), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            log.warning("could not persist case ids: %s", e)

    # ----- lifecycle -----
    def start(self, brain_ws: bool = True) -> None:
        self.outbox.start()
        self._tasks.append(asyncio.create_task(self._ticker()))
        if brain_ws:
            self._tasks.append(asyncio.create_task(self._consume_brain()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self.outbox.stop()
        await self.control.aclose()

    # ----- outbound -----
    def push(self, case_id: str) -> None:
        self.outbox.put(case_id, self.book.version(case_id), self.book.to_wire(case_id).model_dump())

    def ingest(self, msg: dict, now: Optional[float] = None) -> Optional[str]:
        """One brain message (IncidentEvent or StateSnapshot). Never raises on bad input."""
        now = time.time() if now is None else now
        kind = msg.get("type") if isinstance(msg, dict) else None
        try:
            if kind == "state":
                self.mode = StateSnapshot.model_validate(msg).mode
                return None
            if kind != "incident":
                return None
            ev = IncidentEvent.model_validate(msg)
        except ValidationError as e:
            log.warning("bad brain message: %s", e.errors()[:1])
            return None
        self.events_seen += 1
        self.last_event_at = now
        before = self.book.next_seq
        case_id = self.book.on_event(ev, now)
        if case_id:
            if self.book.next_seq != before:
                self._save_ids()
            self.push(case_id)
        return case_id

    async def _consume_brain(self) -> None:
        url = self.s.brain_url.replace("http", "ws", 1).rstrip("/") + "/ws/events"
        backoff = RECONNECT_START_S
        while True:
            try:
                async with websockets.connect(url, open_timeout=5, ping_interval=20) as ws:
                    self.brain_connected = True
                    backoff = RECONNECT_START_S
                    async for raw in ws:
                        try:
                            self.ingest(json.loads(raw))
                        except ValueError:
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as e:          # brain down / restarting: keep trying, never crash
                log.info("brain stream unavailable (%s); retrying in %.1fs", type(e).__name__, backoff)
            self.brain_connected = False
            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_MAX_S, backoff * 2)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            for cid in self.book.expire(time.time()):
                self.push(cid)

    # ----- inbound -----
    async def case_update(self, body: dict) -> dict:
        """Apply an owner change or decision from the Web app; raises DecisionError / ValidationError."""
        upd = wire.parse_case_update(body)
        now = time.time()
        if isinstance(upd, wire.CaseUpdateOwner):
            cid = self.book.on_owner(upd, now)
            self.push(cid)
            return {"ok": True, "case_id": cid, "owner": upd.owner}
        cid, cmds = self.book.on_decision(upd, now)
        results = [await self._execute(cid, c) for c in cmds]
        self.push(cid)
        return {"ok": True, "case_id": cid, "decision": upd.decision, "commands": results}

    async def _execute(self, case_id: str, cmd: Command) -> dict:
        base = self.s.brain_url if cmd.target == "brain" else self.s.gateway_url
        url = base.rstrip("/") + cmd.path
        body = cmd.body.model_dump()
        out = {"target": cmd.target, "path": cmd.path, "body": body}
        try:
            r = await self.control.post(url, json=body, headers={H_CONTROL_TOKEN: self.s.control_token})
            out["status"] = r.status_code
        except (httpx.HTTPError, OSError) as e:
            out["status"] = None
            out["error"] = type(e).__name__
        now = time.time()
        what = f"{cmd.path} {json.dumps({k: v for k, v in body.items() if v is not None}, separators=(',', ':'))}"
        if out["status"] is not None and 200 <= out["status"] < 300:
            self.book.note(case_id, now, f"TenantGuard applied {what}")
        elif cmd.target == "brain" and out["status"] == 404:
            self.book.note(case_id, now, f"Best-effort: brain not ready for {cmd.path} (404); "
                                         f"timers keep their safe default")
        else:
            self.book.note(case_id, now, f"Could not apply {what}: "
                                         f"{out.get('status') or out.get('error')}; timers keep their safe default")
        return out

    def health(self) -> dict:
        return {"ok": True, "brain_connected": self.brain_connected, "mode": self.mode,
                "events_seen": self.events_seen, "cases": len(self.book.cases),
                "outbox": {**self.outbox.stats, "pending": len(self.outbox.pending),
                           "last_error": self.outbox.last_error},
                "web_url": self.s.web_url}
