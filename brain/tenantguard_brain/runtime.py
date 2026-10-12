"""I/O around the pure Brain: telemetry WS, worker /stats polling, heartbeat, control sends, UI hub."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import List, Optional, Set

import httpx
import websockets

from tenantguard_common.contracts import Heartbeat, LimitsCmd, TelemetryFrame, WorkerStats

from .core import Brain
from .settings import BrainSettings

log = logging.getLogger("tenantguard.brain")


class Hub:
    """Fan-out to UI WebSocket clients. Slow clients drop messages instead of stalling the brain."""

    def __init__(self, max_queue: int = 256):
        self.clients: Set[asyncio.Queue] = set()
        self.max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def publish(self, text: str) -> None:
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()          # drop the oldest
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(text)


class Runtime:
    def __init__(self, cfg: BrainSettings, brain: Optional[Brain] = None):
        self.cfg = cfg
        self.brain = brain or Brain(cfg)
        self.hub = Hub()
        self.client: Optional[httpx.AsyncClient] = None
        self.tasks: List[asyncio.Task] = []
        self.telemetry_connected = False
        self.gateway_ok: Optional[bool] = None
        self.worker_ok: dict = {}
        self.sent_count = 0
        self.heartbeats = 0

    @property
    def ws_url(self) -> str:
        base = self.cfg.gateway_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + "/internal/telemetry"

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(1.0, connect=1.0),
                                        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
        for coro in (self._telemetry_loop(), self._stats_loop(), self._heartbeat_loop(),
                     self._tick_loop(), self._snapshot_loop()):
            self.tasks.append(asyncio.create_task(coro))
        log.info("brain started: gateway=%s workers=%d", self.cfg.gateway_url, len(self.cfg.worker_urls))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.client:
            await self.client.aclose()

    # ------------------------------------------------------------ ingest
    async def _telemetry_loop(self) -> None:
        backoff = 0.5
        while True:
            try:
                async with websockets.connect(self.ws_url, open_timeout=3, ping_interval=10,
                                              max_size=8 * 1024 * 1024) as ws:
                    self.telemetry_connected = True
                    backoff = 0.5
                    log.info("telemetry connected: %s", self.ws_url)
                    async for msg in ws:
                        try:
                            frame = TelemetryFrame.model_validate_json(msg)
                        except Exception as e:   # bad frame: log and skip, never die
                            log.warning("bad telemetry frame skipped: %s", e)
                            continue
                        self.brain.ingest_frame(frame, time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("telemetry disconnected (%s); retry in %.1fs", type(e).__name__, backoff)
            self.telemetry_connected = False
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 2)

    async def _poll_one(self, server: str, url: str) -> None:
        try:
            r = await self.client.get(url + "/stats")
            r.raise_for_status()
            self.brain.ingest_stats(WorkerStats.model_validate(r.json()), time.time())
            self.worker_ok[server] = True
        except Exception as e:
            if self.worker_ok.get(server, True):
                log.info("worker %s /stats failed: %s", server, type(e).__name__)
            self.worker_ok[server] = False

    async def _stats_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            await asyncio.gather(*(self._poll_one(s, u) for s, u in self.cfg.worker_urls.items()))
            await asyncio.sleep(max(0.05, self.cfg.stats_poll_s - (time.monotonic() - t0)))

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                r = await self.client.post(self.cfg.gateway_url + "/control/heartbeat",
                                           json=Heartbeat(ts=time.time()).model_dump())
                self.gateway_ok = r.status_code < 400
                self.heartbeats += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.gateway_ok = False
            await asyncio.sleep(self.cfg.heartbeat_s)

    # ------------------------------------------------------------ decide + act
    async def _send(self, cmd: LimitsCmd) -> None:
        ok = False
        try:
            r = await self.client.post(self.cfg.gateway_url + "/control/limits", json=cmd.model_dump())
            ok = r.status_code < 400
            if not ok:
                log.warning("gateway refused %s: %s %s", cmd, r.status_code, r.text[:200])
        except Exception as e:
            log.warning("control send failed (%s): %s", type(e).__name__, cmd)
        self.brain.mark_sent(cmd, ok, time.time())
        if ok:
            self.sent_count += 1
            log.info("sent limits %s factor=%.2f weight_factor=%.2f ttl=%gs", cmd.tenant, cmd.factor,
                     cmd.weight_factor, cmd.ttl_s)

    async def _tick_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                res = self.brain.tick(time.time())
                for ev in res.events:
                    log.info("incident %s %s %s %s->%s: %s", ev.incident_id, ev.stage, ev.tenant,
                             ev.prev_level, ev.level, ev.detail)
                    self.hub.publish(ev.model_dump_json())
                for cmd in res.commands:
                    asyncio.create_task(self._send(cmd))
            except Exception:
                log.exception("tick failed; skipping")
            await asyncio.sleep(max(0.01, self.cfg.tick_s - (time.monotonic() - t0)))

    async def _snapshot_loop(self) -> None:
        while True:
            try:
                if self.hub.clients:
                    self.hub.publish(self.brain.snapshot(time.time()).model_dump_json())
            except Exception:
                log.exception("snapshot failed")
            await asyncio.sleep(0.5)

    def health(self) -> dict:
        now = time.time()
        b = self.brain
        return {
            "ok": True,
            "mode": b.mode,
            "telemetry_connected": self.telemetry_connected,
            "last_frame_age_s": round(now - b.last_frame_at, 2) if b.last_frame_at else None,
            "gateway_ok": self.gateway_ok,
            "workers_ok": self.worker_ok,
            "heartbeats_sent": self.heartbeats,
            "commands_sent": self.sent_count,
            "ui_clients": len(self.hub.clients),
        }


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
