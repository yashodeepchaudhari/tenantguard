"""I/O around the pure Brain: telemetry WS, worker /stats + /canary polling, heartbeat, control sends,
start-up adoption of leftover overrides, and the UI hub."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

import httpx
import websockets
from pydantic import BaseModel

from tenantguard_common.contracts import (H_CONTROL_TOKEN, ChargeCmd, Heartbeat, IncidentEvent, LimitsCmd, OverridesView,
                                          TelemetryFrame, WorkerStats)

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


Post = Callable[[str, BaseModel], "asyncio.Future[bool]"]
Done = Callable[[BaseModel, bool], None]


class Sender:
    """Serialised delivery per key (e.g. one key per tenant's limits).

    At most one request per key is in flight; a command submitted while another is in flight replaces
    any older unsent one (latest wins), so the gateway can never end up with a stale command that
    overtook a newer one. Task references are held until the task finishes.
    """

    def __init__(self, post: Post):
        self.post = post
        self.pending: Dict[str, Tuple[str, BaseModel, Done]] = {}
        self.running: Dict[str, asyncio.Task] = {}

    def submit(self, key: str, path: str, model: BaseModel, done: Done) -> None:
        self.pending[key] = (path, model, done)
        if key not in self.running:
            self.running[key] = asyncio.create_task(self._run(key))

    async def _run(self, key: str) -> None:
        try:
            while key in self.pending:
                path, model, done = self.pending.pop(key)
                ok = False
                try:
                    ok = await self.post(path, model)
                finally:
                    done(model, ok)
        finally:
            self.running.pop(key, None)      # synchronously, before any new submit can see a stale entry

    async def close(self) -> None:
        tasks = list(self.running.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Runtime:
    def __init__(self, cfg: BrainSettings, brain: Optional[Brain] = None):
        self.cfg = cfg
        self.brain = brain or Brain(cfg)
        self.hub = Hub()
        self.client: Optional[httpx.AsyncClient] = None
        self.tasks: List[asyncio.Task] = []
        self.sender = Sender(self._post)
        self.telemetry_connected = False
        self.gateway_ok: Optional[bool] = None
        self.worker_ok: dict = {}
        self.sent_count = 0
        self.heartbeats = 0
        self.adopted: Optional[bool] = None

    @property
    def ws_url(self) -> str:
        base = self.cfg.gateway_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + "/internal/telemetry"

    @property
    def control_headers(self) -> dict:
        return {H_CONTROL_TOKEN: self.cfg.control_token}

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(1.0, connect=1.0),
                                        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
        for coro in (self._adopt_once(), self._telemetry_loop(), self._stats_loop(), self._canary_loop(),
                     self._heartbeat_loop(), self._tick_loop(), self._snapshot_loop()):
            self.tasks.append(asyncio.create_task(coro))
        log.info("brain started: gateway=%s workers=%d", self.cfg.gateway_url, len(self.cfg.worker_urls))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.sender.close()
        if self.client:
            await self.client.aclose()

    # ------------------------------------------------------------ gateway calls
    async def _post(self, path: str, model: BaseModel) -> bool:
        try:
            r = await self.client.post(self.cfg.gateway_url + path, json=model.model_dump(),
                                       headers=self.control_headers)
            if r.status_code >= 400:
                log.warning("gateway refused %s %s: %s %s", path, model, r.status_code, r.text[:200])
                return False
            return True
        except Exception as e:
            log.warning("control send %s failed (%s)", path, type(e).__name__)
            return False

    def _publish(self, events: List[IncidentEvent]) -> None:
        for ev in events:
            log.info("incident %s %s %s %s->%s: %s", ev.incident_id, ev.stage, ev.tenant,
                     ev.prev_level, ev.level, ev.detail)
            self.hub.publish(ev.model_dump_json())

    def _limits_done(self, cmd: BaseModel, ok: bool) -> None:
        self.brain.mark_sent(cmd, ok, time.time())
        if ok:
            self.sent_count += 1
            log.info("sent limits %s factor=%.2f weight_factor=%.2f ttl=%gs", cmd.tenant, cmd.factor,
                     cmd.weight_factor, cmd.ttl_s)

    def _routes_done(self, cmd: BaseModel, ok: bool) -> None:
        self.brain.s3.mark_routes(ok, time.time())
        if ok:
            self.sent_count += 1
            log.info("sent route weights for %s ttl=%gs", sorted(cmd.weights), cmd.ttl_s)

    def _control_done(self, cmd: BaseModel, ok: bool) -> None:
        if isinstance(cmd, ChargeCmd):
            if not ok:
                self.brain.charge_failed(cmd)
            return
        self.brain.control_done(cmd, ok, time.time())
        if ok:
            self.sent_count += 1
            log.info("sent %s", cmd)

    def send_limits(self, cmd: LimitsCmd) -> None:
        self.sender.submit(f"limits:{cmd.tenant}", "/control/limits", cmd, self._limits_done)

    # ------------------------------------------------------------ start-up
    async def _adopt_once(self) -> None:
        """Take over (or clear) overrides a previous brain run left on the gateway."""
        while True:
            try:
                r = await self.client.get(self.cfg.gateway_url + "/admin/overrides", headers=self.control_headers)
                r.raise_for_status()
                ov = OverridesView.model_validate(r.json())
                events, clears = self.brain.adopt_overrides(ov, time.time())
                self._publish(events)
                for c in clears:
                    self.send_limits(c)
                self.adopted = True
                if events or clears:
                    log.info("adopted %d and cleared %d leftover overrides", len(events), len(clears))
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.adopted = False
                log.info("GET /admin/overrides failed (%s); retrying", type(e).__name__)
                await asyncio.sleep(2.0)

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

    async def _canary_one(self, server: str, url: str) -> None:
        t0 = time.perf_counter()
        try:
            r = await self.client.get(url + "/canary")
            if r.status_code < 500:
                self.brain.ingest_canary(server, (time.perf_counter() - t0) * 1000.0, time.time())
        except Exception:
            pass                         # an unreachable worker shows up in /stats (workers_ok)

    async def _canary_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            await asyncio.gather(*(self._canary_one(s, u) for s, u in self.cfg.worker_urls.items()))
            await asyncio.sleep(max(0.05, self.cfg.canary_s - (time.monotonic() - t0)))

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                r = await self.client.post(self.cfg.gateway_url + "/control/heartbeat",
                                           json=Heartbeat(ts=time.time()).model_dump(),
                                           headers=self.control_headers)
                self.gateway_ok = r.status_code < 400
                self.heartbeats += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.gateway_ok = False
            await asyncio.sleep(self.cfg.heartbeat_s)

    # ------------------------------------------------------------ decide + act
    async def _tick_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                res = self.brain.tick(time.time())
                self._publish(res.events)
                for cmd in res.commands:
                    self.send_limits(cmd)
                for rc in res.route_cmds:
                    self.sender.submit("routes", "/control/route_weights", rc, self._routes_done)
                for key, path, cmd in res.control:
                    self.sender.submit(key, path, cmd, self._control_done)
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
            "mode": b.mode or self.cfg.default_mode,
            "telemetry_connected": self.telemetry_connected,
            "last_frame_age_s": round(now - b.last_frame_at, 2) if b.last_frame_at else None,
            "gateway_ok": self.gateway_ok,
            "overrides_adopted": self.adopted,
            "workers_ok": self.worker_ok,
            "heartbeats_sent": self.heartbeats,
            "commands_sent": self.sent_count,
            "sends_in_flight": len(self.sender.running),
            "ui_clients": len(self.hub.clients),
        }
