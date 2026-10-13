"""Asyncio traffic engine: keep-alive httpx pools per tenant, calm Poisson personas, request recording."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Set, Tuple

import httpx

from .poisson import run_poisson
from .settings import Endpoint, LabConfig
from .stats import CONN_ERROR, TIMEOUT, Recorder, Sample, percentile

log = logging.getLogger("tenantguard_lab.engine")


class InflightGate:
    """Counts in-flight requests of one stream and refuses new ones past `cap` (open-loop protection)."""

    def __init__(self, cap: int):
        self.cap = cap
        self.n = 0
        self.tasks: Set[asyncio.Task] = set()

    def full(self) -> bool:
        return self.n >= self.cap

    def spawn(self, coro) -> asyncio.Task:
        self.n += 1
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self.n -= 1
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.warning("request task failed: %r", t.exception())

        task.add_done_callback(_done)
        return task

    async def cancel_all(self) -> None:
        tasks = list(self.tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class Engine:
    """Owns HTTP pools and the calm traffic. Attacks (attacks.py) reuse `send` with their own pools."""

    def __init__(self, cfg: LabConfig, recorder: Optional[Recorder] = None, seed: Optional[int] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.cfg = cfg
        self.transport = transport          # tests inject httpx.MockTransport
        self.recorder = recorder or Recorder()
        self.rng = random.Random(seed)
        self._clients: Dict[Tuple[str, str], httpx.AsyncClient] = {}
        self._calm_stop: Optional[asyncio.Event] = None
        self._calm_tasks: Dict[str, asyncio.Task] = {}
        self._calm_gates: Dict[str, InflightGate] = {}
        self.calm_started_at: Optional[float] = None
        self.loop_lag: Deque[Tuple[float, float]] = deque(maxlen=6000)   # (epoch ts, lag ms), 10 Hz
        self._lag_task: Optional[asyncio.Task] = None

    # ---------- self-check ----------
    def start_monitor(self, every_s: float = 0.1) -> None:
        """Measure event-loop lag. If the lab itself is overloaded, client-side latencies are inflated by it;
        /lab/metrics reports this so a result can be trusted (or not)."""
        if self._lag_task is not None and not self._lag_task.done():
            return

        async def run() -> None:
            while True:
                t = time.perf_counter()
                await asyncio.sleep(every_s)
                self.loop_lag.append((time.time(), max(0.0, (time.perf_counter() - t - every_s) * 1000.0)))

        self._lag_task = asyncio.ensure_future(run())

    def loop_lag_ms(self, window_s: float) -> Dict[str, float]:
        cutoff = time.time() - window_s
        lags = [lag for ts, lag in list(self.loop_lag) if ts >= cutoff]
        return {"p50": round(percentile(lags, 50), 2), "p99": round(percentile(lags, 99), 2),
                "max": round(max(lags), 2) if lags else 0.0}

    # ---------- HTTP ----------
    def client(self, tenant: str, pool: str = "calm", max_conns: Optional[int] = None) -> httpx.AsyncClient:
        """A keep-alive client for (tenant, pool). Pools are separate so an attacker's connections never
        queue a victim's requests on the client side."""
        key = (tenant, pool)
        c = self._clients.get(key)
        if c is None or c.is_closed:
            n = max_conns or int(self.cfg.client["max_inflight_per_tenant"])
            c = httpx.AsyncClient(
                base_url=self.cfg.gateway_url,
                headers={"X-API-Key": self.cfg.persona(tenant).tenant.api_key},
                timeout=httpx.Timeout(float(self.cfg.client["timeout_s"])),
                limits=httpx.Limits(max_connections=n, max_keepalive_connections=n),
                transport=self.transport,
            )
            self._clients[key] = c
        return c

    async def close_pool(self, tenant: str, pool: str) -> None:
        c = self._clients.pop((tenant, pool), None)
        if c is not None:
            await c.aclose()

    def build(self, ep: Endpoint, extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(ep.params)
        if extra_params:
            params.update(extra_params)
        req: Dict[str, Any] = {"method": ep.method, "url": ep.path}
        keyspace = int(self.cfg.client.get("keyspace", 1000))
        if ep.path.endswith("/read"):
            params.setdefault("key", f"k{self.rng.randrange(keyspace):04d}")
        if ep.method.upper() == "POST":
            req["json"] = {"key": f"k{self.rng.randrange(keyspace):04d}", "value": f"v{self.rng.random():.6f}"}
        if params:
            req["params"] = params
        return req

    async def send(self, client: httpx.AsyncClient, tenant: str, req: Dict[str, Any], src: str, kind: str = ""
                   ) -> Tuple[int, Optional[httpx.Headers]]:
        """Send one request, record its client-side latency and outcome, return (status, headers)."""
        t0 = time.perf_counter()
        headers = None
        try:
            r = await client.request(**req)
            status = r.status_code
            headers = r.headers
        except httpx.TimeoutException:
            status = TIMEOUT
        except httpx.HTTPError:
            status = CONN_ERROR
        lat = (time.perf_counter() - t0) * 1000.0
        self.recorder.add(tenant, Sample(time.time(), lat, status, src, kind))
        return status, headers

    # ---------- calm traffic ----------
    @property
    def calm_running(self) -> bool:
        return self._calm_stop is not None and not self._calm_stop.is_set()

    def pick_kind(self, mix: Dict[str, float]) -> str:
        kinds = list(mix)
        return self.rng.choices(kinds, weights=[mix[k] for k in kinds], k=1)[0]

    def start_calm(self) -> bool:
        """Start one Poisson stream per persona. Returns False if already running."""
        if self.calm_running:
            return False
        self.start_monitor()
        stop = asyncio.Event()
        self._calm_stop = stop
        self.calm_started_at = time.time()
        for tid in self.cfg.personas:
            self._start_stream(tid)
        log.info("calm traffic started for %d tenants (rate_scale %.2f)", len(self._calm_tasks), self.cfg.rate_scale)
        return True

    def _start_stream(self, tid: str) -> None:
        """One Poisson stream of calm requests for one tenant (calm traffic must be running)."""
        assert self._calm_stop is not None
        persona = self.cfg.personas[tid]
        gate = InflightGate(int(self.cfg.client["max_inflight_per_tenant"]))
        self._calm_gates[tid] = gate
        client = self.client(tid, "calm")

        def fire() -> None:
            if gate.full():
                self.recorder.drop(tid)
                return
            kind = self.pick_kind(persona.mix)
            gate.spawn(self.send(client, tid, self.build(self.cfg.endpoints[kind]), "calm", kind))

        def rate() -> float:
            return persona.rps * self.cfg.rate_scale

        self._calm_tasks[tid] = asyncio.ensure_future(
            run_poisson(rate, fire, self._calm_stop, rng=random.Random(self.rng.random())))

    def start_tenant(self, tid: str) -> None:
        """Calm traffic for a tenant added while calm traffic runs (otherwise the next start includes it)."""
        if self.calm_running and tid not in self._calm_tasks:
            self._start_stream(tid)

    async def stop_tenant(self, tid: str) -> None:
        """Stop a tenant's calm stream and close its connection pools."""
        task = self._calm_tasks.pop(tid, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        gate = self._calm_gates.pop(tid, None)
        if gate is not None:
            await gate.cancel_all()
        for key in [k for k in self._clients if k[0] == tid]:
            await self.close_pool(*key)

    async def stop_calm(self) -> bool:
        if not self.calm_running:
            return False
        assert self._calm_stop is not None
        self._calm_stop.set()
        tasks = list(self._calm_tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for g in self._calm_gates.values():
            await g.cancel_all()
        self._calm_tasks.clear()
        self._calm_gates.clear()
        self.calm_started_at = None
        log.info("calm traffic stopped")
        return True

    def calm_inflight(self) -> Dict[str, int]:
        return {t: g.n for t, g in self._calm_gates.items()}

    async def aclose(self) -> None:
        await self.stop_calm()
        if self._lag_task is not None:
            self._lag_task.cancel()
            await asyncio.gather(self._lag_task, return_exceptions=True)
        for c in list(self._clients.values()):
            await c.aclose()
        self._clients.clear()
