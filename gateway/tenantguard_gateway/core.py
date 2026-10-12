"""The gateway engine: identify -> classify -> admit -> queue (DRR / FIFO) -> route -> forward -> measure.

Dispatch is event-driven: `_pump()` runs whenever work is queued or a worker slot frees, and hands
queued items to free slots in scheduler order. No background dispatcher task is needed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from typing import Dict, List, Optional

import httpx
from tenantguard_common.contracts import (H_CLASS, H_CPU_MS, H_REQUEST_ID, H_TENANT, LimiterStat, Quota,
                                          TelemetryFrame, TenantAdminView, TenantConfig)

from . import router
from .admission import admit, ratelimit_headers
from .overrides import Overrides
from .scheduler import Item, Scheduler
from .settings import CLASS_COST_ALPHA, DEFAULT_CLASS_COST_MS, GatewaySettings
from .telemetry import Telemetry
from .tenants import Registry, TenantState, request_class, scale_quota

log = logging.getLogger("tenantguard.gateway")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length", "x-api-key"}
RESP_DROP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
# Headers only the gateway may set; strip any client-supplied copies.
TRUSTED = {H_TENANT.lower(), H_CLASS.lower()}


class Result:
    """What the HTTP layer turns into a response."""

    def __init__(self, status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None,
                 json: Optional[dict] = None):
        self.status, self.body, self.headers, self.json = status, body, headers or {}, json


class Gateway:
    def __init__(self, settings: GatewaySettings, tenants: List[TenantConfig], workers: Dict[str, str],
                 client: Optional[httpx.AsyncClient] = None, rng: Optional[random.Random] = None):
        self.s = settings
        self.workers = dict(workers)
        self.mode: str = settings.default_mode
        self.registry = Registry(settings.cost_bucket_s)
        now = time.perf_counter()
        for t in tenants:
            self.registry.add(t, now)
        self.overrides = Overrides()
        self.sched = Scheduler(self._weight, quantum_ms=DEFAULT_CLASS_COST_MS, fifo=self.mode == "off")
        self.window: Dict[str, int] = {w: settings.initial_window for w in self.workers}
        self.slots: Dict[str, int] = {w: 0 for w in self.workers}
        self.class_cost: Dict[tuple, float] = {}
        self.telemetry = Telemetry()
        self.latest: Optional[TelemetryFrame] = None
        self.loop_lag_ms = 0.0     # how late the last telemetry tick woke up (event-loop saturation)
        self.rng = rng or random.Random()
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_s, connect=5.0),
            limits=httpx.Limits(max_connections=1024, max_keepalive_connections=256))

    # ----- policy views (control overrides apply only in tenantguard mode) -----
    @property
    def controlled(self) -> bool:
        return self.mode == "tenantguard"

    def _limits(self, tenant: str):
        return self.overrides.limits_for(tenant) if self.controlled else None

    def factors(self, tenant: str) -> tuple:
        cmd = self._limits(tenant)
        return (cmd.factor, cmd.weight_factor) if cmd else (1.0, 1.0)

    def effective_quota(self, st: TenantState) -> Quota:
        return scale_quota(st.cfg.quota, self.factors(st.id)[0])

    def _weight(self, tenant: str) -> float:
        st = self.registry.get(tenant)
        base = st.cfg.weight if st else 1.0
        return base * self.factors(tenant)[1]

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.sched.fifo = mode == "off"
        self._pump()

    # ----- routing / dispatch -----
    def _candidates(self, st: TenantState, cls: str) -> router.Candidates:
        if not self.controlled:
            return router.candidates(st.cfg.servers, self.workers)
        return router.candidates(st.cfg.servers, self.workers, weights=self.overrides.weights_for(st.id),
                                 relocation=self.overrides.relocation_for(st.id, cls),
                                 drained=self.overrides.drained())

    def _has_slot(self, server: str) -> bool:
        return self.slots.get(server, 0) < self.window.get(server, 0)

    def _eligible(self, item: Item) -> bool:
        st = self.registry.get(item.tenant)
        if st is None:
            return False
        item.server = router.choose(self._candidates(st, item.cls), self._has_slot, self.rng)
        return item.server is not None

    def _pump(self) -> None:
        while True:
            item = self.sched.pop(self._eligible)
            if item is None:
                return
            fut: asyncio.Future = item.payload
            if fut.done():          # waiter vanished; give the slot to someone else
                continue
            self.slots[item.server] += 1
            fut.set_result(item.server)

    def _release(self, item: Item) -> None:
        """Free the dispatch slot an item was granted (idempotent)."""
        if item.meta.get("released"):
            return
        item.meta["released"] = True
        self.slots[item.server] -= 1
        self._pump()

    # ----- the request path -----
    async def handle(self, method: str, path: str, query: str, query_keys: List[str],
                     headers: Dict[str, str], body: bytes) -> Result:
        t0 = time.perf_counter()
        st = self.registry.lookup(headers.get("x-api-key"))
        if st is None:
            return Result(401, json={"error": "unauthorized", "detail": "missing or unknown X-API-Key"})
        if len(body) > self.s.body_limit_bytes:
            return Result(413, json={"error": "payload_too_large", "limit_bytes": self.s.body_limit_bytes})
        tenant = st.id
        cls = request_class(method, path, query_keys, len(body))
        rid = headers.get(H_REQUEST_ID.lower()) or uuid.uuid4().hex
        rl_headers: Dict[str, str] = {}

        if self.mode != "off":
            eff = self.effective_quota(st)
            st.configure(eff, self.s.cost_bucket_s, t0)
            blocked = self.overrides.block_remaining(tenant) if self.controlled else None
            dec = admit(st.tokens, st.cost, st.pending, eff, t0, blocked)
            rl_headers = ratelimit_headers(st.tokens, st.cost, st.pending + int(dec.ok), eff,
                                           self.s.cost_bucket_s, t0)
            if not dec.ok:
                self.telemetry.rejected(tenant, dec.reasons)
                return Result(429, headers={**rl_headers, "Retry-After": dec.retry_after_header,
                                            H_REQUEST_ID: rid},
                              json={"error": "rate_limited", "tenant": tenant, "reasons": dec.reasons,
                                    "retry_after_ms": round(dec.retry_after_s * 1000, 1)})

        self.telemetry.admitted(tenant)
        st.pending += 1
        item: Optional[Item] = None
        try:
            lane = "batch" if path in self.s.batch_routes else "interactive"
            max_wait_s = self.s.max_wait_ms.get(lane, 1000.0) / 1000.0
            fut = asyncio.get_running_loop().create_future()
            item = Item(tenant=tenant, cost=self.class_cost.get((tenant, cls), DEFAULT_CLASS_COST_MS),
                        cls=cls, payload=fut)
            self.sched.push(item)
            self._pump()
            expired = False
            try:
                await asyncio.wait({fut}, timeout=max_wait_s)
            finally:
                if item.state == "queued":          # timed out, or the client went away
                    expired = self.sched.cancel(item)
                    fut.cancel()
            if expired:
                wait_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.expired(tenant, wait_ms)
                return Result(503, headers={**rl_headers, "Retry-After": "1", H_REQUEST_ID: rid},
                              json={"error": "queue_timeout", "tenant": tenant, "waited_ms": round(wait_ms, 1),
                                    "max_wait_ms": max_wait_s * 1000})
            server = fut.result()
            t1 = time.perf_counter()
            self.telemetry.dispatched(tenant, (t1 - t0) * 1000)
            st.inflight += 1
            fwd = {k: v for k, v in headers.items() if k not in HOP_BY_HOP and k not in TRUSTED}
            fwd.update({H_TENANT: tenant, H_CLASS: cls, H_REQUEST_ID: rid})
            url = self.workers[server] + path + (f"?{query}" if query else "")
            try:
                resp = await self.client.request(method, url, content=body, headers=fwd)
                status, content = resp.status_code, resp.content
                out = {k: v for k, v in resp.headers.items() if k.lower() not in RESP_DROP}
            except httpx.HTTPError as e:
                log.warning("upstream %s failed: %r", server, e)
                status, content, out = 502, b"", {}
                resp = None
            finally:
                st.inflight -= 1
                self._release(item)
            t2 = time.perf_counter()
            cpu = _parse_cpu(resp.headers.get(H_CPU_MS)) if resp is not None else 0.0
            if cpu is None:
                self.telemetry.cpu_missing(tenant, server)
                cpu = 0.0
            if self.mode != "off" and cpu:
                st.cost.debit(cpu, t2)
            if resp is not None and cpu:
                old = self.class_cost.get((tenant, cls))
                self.class_cost[(tenant, cls)] = cpu if old is None else old + CLASS_COST_ALPHA * (cpu - old)
            self.telemetry.done(tenant, server, (t2 - t1) * 1000, (t2 - t0) * 1000, status, cpu)
            out.update(rl_headers)
            out[H_REQUEST_ID] = rid
            out["X-TenantGuard-Server"] = server
            if resp is None:
                return Result(502, headers=out, json={"error": "bad_gateway", "server": server})
            return Result(status, content, out)
        finally:
            st.pending -= 1
            # Granted a slot but cancelled (client gone) before forwarding: give the slot back.
            if item is not None and item.payload.done() and not item.payload.cancelled():
                self._release(item)

    # ----- telemetry tick -----
    def limiter_stats(self) -> List[LimiterStat]:
        return [LimiterStat(server=w, window=self.window[w], inflight=self.slots[w]) for w in self.workers]

    def tick(self) -> TelemetryFrame:
        self.overrides.sweep()
        self._pump()     # an expired drain/relocation may have freed a path
        reg = self.registry
        self.latest = self.telemetry.frame(
            self.mode, self.s.tick_ms, reg.ids(), self.sched.depth,
            lambda t: reg.get(t).inflight if reg.get(t) else 0, self.limiter_stats())
        return self.latest

    # ----- admin views -----
    def admin_view(self, st: TenantState) -> TenantAdminView:
        factor, wf = self.factors(st.id)
        reloc = self.overrides.relocation_for(st.id) if self.controlled else None
        return TenantAdminView(
            id=st.id, name=st.cfg.name, tier=st.cfg.tier, servers=list(st.cfg.servers), weight=st.cfg.weight,
            quota=st.cfg.quota, effective_quota=self.effective_quota(st), factor=factor, weight_factor=wf,
            blocked=self.controlled and self.overrides.block_remaining(st.id) is not None,
            placement=reloc or "normal", override_expires_at=self.overrides.tenant_expiry(st.id),
            monthly_revenue_rs=st.cfg.monthly_revenue_rs, protected=st.cfg.protected)

    async def aclose(self) -> None:
        await self.client.aclose()


def _parse_cpu(v: Optional[str]) -> Optional[float]:
    """Worker CPU ms from the header; None if missing or unparseable (debit 0, count it, never raise)."""
    if v is None:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if x >= 0 and math.isfinite(x) else None
