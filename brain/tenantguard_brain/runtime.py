"""I/O around the pure Brain (ladders, incidents) and ControlLoop (allocation, replicas, split tests):
gateway telemetry WS, replica /stats and /healthz polling, the replica backend, snapshot publishing, the registry
file, the audit log and the UI hub."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx
import websockets

from tenantguard_common.contracts import (H_CONTROL_TOKEN, IncidentEvent, OverridesView, Snapshot, TelemetryFrame,
                                          WorkerStats)

from . import registry as R
from . import registry_store
from .backends import Backend, DockerBackend, DockerCfg, ProcessBackend
from .control import ControlLoop, Observation
from .core import Brain
from .registry import Registry
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


def make_backend(cfg: BrainSettings) -> Backend:
    c = cfg.control
    if c.backend == "process":
        return ProcessBackend(c.process_base_port, cfg.root / "worker", cfg.root / "worker" / ".replicas")
    return DockerBackend(DockerCfg(image=c.worker_image, stack=cfg.stack, network=c.docker_network,
                                   cgroup_parent=c.cgroup_parent, socket_path=c.docker_url.removeprefix("unix://"),
                                   api=c.docker_api))


def load_registry(cfg: BrainSettings) -> Registry:
    return registry_store.load(cfg.control.registry_path, lambda: R.seed(
        cfg.seed_pool, cfg.seed_projects, cfg.seed_tenants, cfg.control.scale))


class Runtime:
    def __init__(self, cfg: BrainSettings, backend: Optional[Backend] = None, registry: Optional[Registry] = None):
        self.cfg = cfg
        reg = registry or load_registry(cfg)
        self.control = ControlLoop(cfg, reg)
        self.brain = Brain(cfg, R.snap_tenants(reg, cfg.tier_quotas), {})
        self.backend = backend or make_backend(cfg)
        self.hub = Hub()
        self.client: Optional[httpx.AsyncClient] = None
        self.tasks: List[asyncio.Task] = []
        self.kick = asyncio.Event()
        self.telemetry_connected = False
        self.gateway_ok: Optional[bool] = None
        self.worker_ok: Dict[str, bool] = {}
        self.last_snapshot: Optional[Snapshot] = None
        self.snapshots_sent = 0

    @property
    def ws_url(self) -> str:
        base = self.cfg.gateway_url
        for a, b in (("https://", "wss://"), ("http://", "ws://")):
            if base.startswith(a):
                base = b + base[len(a):]
        return base + "/internal/telemetry"

    @property
    def control_headers(self) -> dict:
        return {H_CONTROL_TOKEN: self.cfg.control_token}

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(1.0, connect=1.0),
                                        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
        try:
            self.control.adopt(await self.backend.list_replicas(), time.time())
        except Exception as e:
            log.warning("could not list existing replicas (%s); starting with none", type(e).__name__)
        for coro in (self._adopt_once(), self._telemetry_loop(), self._stats_loop(), self._ladder_loop(),
                     self._control_loop(), self._ui_loop()):
            self.tasks.append(asyncio.create_task(coro))
        log.info("brain started: gateway=%s backend=%s projects=%d", self.cfg.gateway_url, self.cfg.control.backend,
                 len(self.control.registry.projects))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.backend.aclose()
        if self.client:
            await self.client.aclose()

    # ------------------------------------------------------------------ registry (admin API)
    def set_registry(self, reg: Registry) -> None:
        """A validated registry change: persist it, then let the control loop act on it now."""
        registry_store.save(reg, self.cfg.control.registry_path)
        self.control.set_registry(reg)
        self._sync_topology()
        self.kick.set()

    def wake(self, project: str) -> bool:
        ok = self.control.wake(project, time.time())
        if ok:
            self.kick.set()
        return ok

    def _sync_topology(self) -> None:
        reg = self.control.registry
        routed = {p: [r.id for r in self.control.replicas_of(p, ("ready", "draining"))] for p in reg.projects}
        self.brain.set_topology(R.snap_tenants(reg, self.cfg.tier_quotas), routed)
        caps = {r.id: r.nano_cpus / 1e9 for r in self.control.fleet.replicas.values() if r.nano_cpus}
        self.brain.set_attribution(self.control.tenant_cpu_by_replica(), caps)

    # ------------------------------------------------------------------ audit + UI events
    def audit(self, kind: str, data: dict) -> None:
        path: Path = self.cfg.control.events_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "kind": kind, **data}) + "\n")
        except OSError as e:
            log.warning("audit log write failed: %s", e)

    def publish(self, events: List[IncidentEvent]) -> None:
        for ev in events:
            log.info("incident %s %s %s %s->%s: %s", ev.incident_id, ev.stage, ev.tenant, ev.prev_level, ev.level,
                     ev.detail)
            self.audit("incident", ev.model_dump())
            self.hub.publish(ev.model_dump_json())

    # ------------------------------------------------------------------ gateway
    async def push_snapshot(self) -> None:
        limits = self.brain.limits() if self.brain.mode in (None, "tenantguard") else []
        snap = self.control.snapshot(limits)
        self.last_snapshot = snap
        try:
            r = await self.client.post(self.cfg.gateway_url + "/control/snapshot", json=snap.model_dump(mode="json"),
                                       headers=self.control_headers)
            self.gateway_ok = r.status_code < 400
            if not self.gateway_ok:
                log.warning("gateway refused snapshot v%d: %s %s", snap.version, r.status_code, r.text[:200])
            else:
                self.snapshots_sent += 1
        except httpx.HTTPError as e:
            self.gateway_ok = False
            log.info("snapshot push failed (%s)", type(e).__name__)

    async def _adopt_once(self) -> None:
        """Take over (or clear) limits a previous brain run left on the gateway."""
        while True:
            try:
                r = await self.client.get(self.cfg.gateway_url + "/admin/overrides", headers=self.control_headers)
                r.raise_for_status()
                self.publish(self.brain.adopt_overrides(OverridesView.model_validate(r.json()), time.time()))
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("GET /admin/overrides failed (%s); retrying", type(e).__name__)
                await asyncio.sleep(2.0)

    # ------------------------------------------------------------------ ingest
    async def _telemetry_loop(self) -> None:
        backoff = 0.5
        while True:
            try:
                async with websockets.connect(self.ws_url, open_timeout=3, ping_interval=10,
                                              max_size=8 * 1024 * 1024) as ws:
                    self.telemetry_connected, backoff = True, 0.5
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

    async def _poll_one(self, rid: str, url: str) -> None:
        try:
            r = await self.client.get(url + "/stats")
            r.raise_for_status()
            self.brain.ingest_stats(WorkerStats.model_validate(r.json()), time.time())
            self.worker_ok[rid] = True
        except Exception as e:
            if self.worker_ok.get(rid, True):
                log.info("replica %s /stats failed: %s", rid, type(e).__name__)
            self.worker_ok[rid] = False

    async def _stats_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            reps = [r for r in self.control.fleet.replicas.values() if r.state in ("ready", "draining")]
            await asyncio.gather(*(self._poll_one(r.id, r.url) for r in reps))
            await asyncio.sleep(max(0.05, self.cfg.stats_poll_s - (time.monotonic() - t0)))

    async def _check_starting(self) -> None:
        async def one(rid: str, url: str) -> None:
            try:
                if (await self.client.get(url + "/healthz")).status_code == 200:
                    self.control.mark_ready(rid)
            except httpx.HTTPError:
                pass
        await asyncio.gather(*(one(r.id, r.url) for r in self.control.fleet.replicas.values() if r.state == "starting"))

    # ------------------------------------------------------------------ decide + act
    async def _ladder_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                res = self.brain.tick(time.time())
                self.publish(res.events)
                if res.changed:
                    await self.push_snapshot()
            except Exception:
                log.exception("ladder tick failed; skipping")
            await asyncio.sleep(max(0.01, self.cfg.tick_s - (time.monotonic() - t0)))

    def observation(self, now: float, container_ms: Dict[str, float]) -> Observation:
        queued, refused = self.brain.demand_signals(now, self.cfg.control.tick_s)
        frame = self.brain.frames[-1][1] if self.brain.frames else None
        return Observation(now=now, mode=self.brain.mode, fresh=self.brain.telemetry_fresh(now), queued_ms=queued,
                           refused_ms_s=refused,
                           usage=self.brain.replica_usage(now, max(self.cfg.detect_window_s, 2 * self.cfg.stats_poll_s)),
                           container_ms=container_ms,
                           inflight={ls.server: ls.inflight for ls in (frame.limiter if frame else [])},
                           slowdown=dict(self.brain.slowdowns))

    async def control_once(self, now: float) -> None:
        """One control tick: the only place that changes allocations and replicas."""
        await self._check_starting()
        ids = list(self.control.fleet.replicas)
        container = await self.backend.cpu_ms(ids) if ids else {}
        actions, findings = self.control.tick(self.observation(now, container))
        results = await self.backend.apply(actions)
        self.control.applied(results, now)
        for res in results:
            if res.action.kind != "update":
                self.audit("replica", {"action": res.action.kind, "replica": res.action.replica_id,
                                       "project": res.action.project, "ok": res.ok, "error": res.error})
        self._sync_topology()
        for f in findings:
            self.publish(self.brain.on_finding(f.kind, f.project, f.tenant, f.detail, f.evidence, now))
        await self.push_snapshot()

    async def _control_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                await self.control_once(time.time())
            except Exception:
                log.exception("control tick failed; skipping")
            self.kick.clear()
            try:
                await asyncio.wait_for(self.kick.wait(), max(0.05, self.cfg.control.tick_s - (time.monotonic() - t0)))
            except asyncio.TimeoutError:
                pass

    async def _ui_loop(self) -> None:
        while True:
            try:
                if self.hub.clients:
                    self.hub.publish(self.brain.snapshot(time.time(), self.control.pool_view()).model_dump_json())
            except Exception:
                log.exception("ui snapshot failed")
            await asyncio.sleep(0.5)

    def health(self) -> dict:
        now = time.time()
        b = self.brain
        return {"ok": True, "mode": b.mode or self.cfg.default_mode, "telemetry_connected": self.telemetry_connected,
                "last_frame_age_s": round(now - b.last_frame_at, 2) if b.last_frame_at else None,
                "gateway_ok": self.gateway_ok, "snapshots_sent": self.snapshots_sent,
                "backend": self.cfg.control.backend, "replicas": len(self.control.fleet.replicas),
                "replicas_ok": self.worker_ok, "ui_clients": len(self.hub.clients)}
