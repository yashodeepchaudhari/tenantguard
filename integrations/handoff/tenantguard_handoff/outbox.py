"""Delivery of cases to the Web app: latest-wins per case, retry with exponential backoff, never blocks.

`put()` is synchronous and O(1): it replaces whatever version of that case was waiting, so a slow or dead
Web app can only ever make us skip stale versions, never queue them up or slow the bridge down.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from typing import Callable, Dict, Optional

import httpx

log = logging.getLogger("tenantguard.handoff.outbox")

BACKOFF_START_S = 0.5
BACKOFF_MAX_S = 8.0


class Outbox:
    def __init__(self, web_url: str, client: Optional[httpx.AsyncClient] = None, timeout_s: float = 3.0,
                 on_delivered: Optional[Callable[[str, int, float], None]] = None,
                 backoff_start_s: float = BACKOFF_START_S, backoff_max_s: float = BACKOFF_MAX_S):
        self.url = web_url.rstrip("/") + "/cases"
        self.client = client or httpx.AsyncClient(timeout=timeout_s)
        self.on_delivered = on_delivered
        self.backoff_start_s, self.backoff_max_s = backoff_start_s, backoff_max_s
        self.pending: "OrderedDict[str, tuple]" = OrderedDict()   # case_id -> (version, body dict)
        self.delivered: Dict[str, int] = {}                        # case_id -> last delivered version
        self.wake = asyncio.Event()
        self.stats = {"sent": 0, "failed": 0, "superseded": 0}
        self.last_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    def put(self, case_id: str, version: int, body: dict) -> None:
        if self.delivered.get(case_id, -1) >= version:
            return
        if case_id in self.pending:
            self.stats["superseded"] += 1
            if self.pending[case_id][0] >= version:
                return
        self.pending[case_id] = (version, body)
        self.wake.set()

    def forget(self, case_id: str) -> None:
        """An evicted case: drop its delivery record (a still-pending last version is delivered first)."""
        if case_id not in self.pending:
            self.delivered.pop(case_id, None)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.client.aclose()

    async def _run(self) -> None:
        backoff = self.backoff_start_s
        while True:
            if not self.pending:
                self.wake.clear()
                await self.wake.wait()
                continue
            case_id, (version, body) = next(iter(self.pending.items()))
            ok = await self._send(body)
            if ok:
                backoff = self.backoff_start_s
                if self.pending.get(case_id, (None,))[0] == version:
                    del self.pending[case_id]
                self.delivered[case_id] = max(version, self.delivered.get(case_id, -1))
                if self.on_delivered:
                    self.on_delivered(case_id, version, time.time())
            else:
                self.pending.move_to_end(case_id)          # other cases get a turn
                await asyncio.sleep(backoff * (0.8 + 0.4 * random.random()))
                backoff = min(self.backoff_max_s, backoff * 2)

    async def _send(self, body: dict) -> bool:
        try:
            r = await self.client.post(self.url, json=body)
        except (httpx.HTTPError, OSError) as e:
            self.stats["failed"] += 1
            self.last_error = f"{type(e).__name__}: {e}"
            return False
        if 200 <= r.status_code < 300:
            self.stats["sent"] += 1
            self.last_error = None
            return True
        self.stats["failed"] += 1
        self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
        if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
            log.warning("Web app rejected case %s: %s", body.get("case_id"), self.last_error)
            return True          # a permanent rejection: retrying the same body can't succeed
        return False
