"""The gateway engine: identify -> classify -> retry budget -> admit -> queue (3 DRR lanes / FIFO) -> route
-> forward -> measure.

Dispatch is event-driven: `_pump()` runs whenever work is queued or a worker slot frees, and hands
queued items to free slots, express lane first, then interactive, then batch. Each lane is its own
weighted DRR across tenants, so every lane stays per-tenant fair. Per-worker windows are adaptive
(gradient limiter) in industry and tenantguard modes.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from collections import OrderedDict
from typing import Awaitable, Callable, Dict, List, Optional

import httpx
from tenantguard_common.contracts import (H_CLASS, H_CPU_MS, H_REQUEST_ID, H_TENANT, ChargeCmd, LimiterStat,
                                          Quota, TelemetryFrame, TenantAdminView, TenantConfig)

from . import router
from .admission import Decision, admit, ratelimit_headers
from .limiter import GradientLimiter
from .overrides import Overrides
from .retry import RetryBudget, fingerprint
from .scheduler import Item, Scheduler
from .security import safe_api_path, valid_request_id
from .settings import CLASS_COST_ALPHA, DEFAULT_CLASS_COST_MS, GatewaySettings
from .telemetry import Telemetry
from .tenants import Registry, TenantState, request_class, scale_quota

log = logging.getLogger("tenantguard.gateway")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length", "x-api-key"}
RESP_DROP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
H_LANE = "X-TenantGuard-Lane"
H_SERVER = "X-TenantGuard-Server"

LANES = ("express", "interactive", "batch")
BodyReader = Callable[[int], Awaitable[Optional[bytes]]]


def _client_must_not_send(name: str) -> bool:
    """Headers never forwarded from a client: hop-by-hop, the API key, anything the gateway owns."""
    return (name in HOP_BY_HOP or name.startswith("x-tenantguard-") or name in ("x-admin-token", "x-request-id"))
CHARGE_IDS_KEPT = 1024


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
        self.lanes: Dict[str, Scheduler] = {
            lane: Scheduler(self._weight, quantum_ms=DEFAULT_CLASS_COST_MS, fifo=self.mode == "off") for lane in LANES}
        self.limiters: Dict[str, GradientLimiter] = {
            w: GradientLimiter(settings.initial_window, settings.min_window, settings.max_window) for w in self.workers}
        self.slots: Dict[str, int] = {w: 0 for w in self.workers}
        self.class_cost: Dict[tuple, float] = {}
        self.reference_costs: Dict[str, float] = {}
        self.retry = RetryBudget(settings.retry_ratio, settings.retry_window_s, settings.retry_min_allowed)
        self.charge_ids: "OrderedDict[str, None]" = OrderedDict()
        self.telemetry = Telemetry()
        self.latest: Optional[TelemetryFrame] = None
        self.loop_lag_ms = 0.0     # how late the last telemetry tick woke up (event-loop saturation)
        self.rng = rng or random.Random()
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_s, connect=5.0),
            limits=httpx.Limits(max_connections=1024, max_keepalive_connections=256))

    # ----- policy views -----
    @property
    def controlled(self) -> bool:
        """Brain control commands apply only in tenantguard mode."""
        return self.mode == "tenantguard"

    @property
    def adaptive(self) -> bool:
        """Lanes, adaptive windows and the retry budget: industry + tenantguard; off = FIFO, fixed window."""
        return self.mode != "off"

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
        for s in self.lanes.values():
            s.fifo = mode == "off"
        self._pump()

    def ref_cost(self, tenant: str, cls: str) -> float:
        """Reference CPU ms of a class: brain-pushed reference (tenantguard) > measured EWMA > default."""
        if self.controlled and cls in self.reference_costs:
            return self.reference_costs[cls]
        return self.class_cost.get((tenant, cls), DEFAULT_CLASS_COST_MS)

    def lane_for(self, path: str, client_class: Optional[str], cost: float) -> str:
        if not self.adaptive:
            return "interactive"
        if (client_class or "").strip().lower() == "batch" or path in self.s.batch_routes:
            return "batch"
        return "express" if cost < self.s.express_cost_ms else "interactive"

    # ----- windows -----
    def window(self, server: str) -> int:
        return self.limiters[server].window if self.adaptive else self.s.initial_window

    def lane_cap(self, lane: str, server: str) -> int:
        """Slots a lane may occupy on a worker: express the whole window; interactive leaves headroom for
        express; batch at most half (it is shed first when the worker saturates)."""
        w = self.window(server)
        if not self.adaptive or lane == "express":
            return w
        interactive = max(1, w - max(1, w // 8))
        if lane == "interactive":
            return interactive
        return max(1, min(interactive, w // 2))

    def queued(self, tenant: Optional[str] = None) -> int:
        if tenant is None:
            return sum(s.total() for s in self.lanes.values())
        return sum(s.depth(tenant) for s in self.lanes.values())

    # ----- routing / dispatch -----
    def _candidates(self, st: TenantState, cls: str) -> router.Candidates:
        if not self.controlled:
            return router.candidates(st.cfg.servers, self.workers)
        return router.candidates(st.cfg.servers, self.workers, weights=self.overrides.weights_for(st.id),
                                 relocation=self.overrides.relocation_for(st.id, cls),
                                 drained=self.overrides.drained())

    def _eligible_in(self, lane: str):
        def eligible(item: Item) -> bool:
            st = self.registry.get(item.tenant)
            if st is None:
                return False
            item.server = router.choose(self._candidates(st, item.cls),
                                        lambda s: self.slots.get(s, 0) < self.lane_cap(lane, s), self.rng)
            return item.server is not None
        return eligible

    def _pump(self) -> None:
        while True:
            item = None
            for lane in LANES:
                item = self.lanes[lane].pop(self._eligible_in(lane))
                if item is not None:
                    break
            if item is None:
                break
            fut: asyncio.Future = item.payload
            if fut.done():          # waiter vanished; give the slot to someone else
                continue
            self.slots[item.server] += 1
            self.limiters[item.server].note_inflight(self.slots[item.server])
            fut.set_result(item.server)
        self._shed_batch()

    def _shed_batch(self) -> None:
        """Interactive/express work is waiting and a queued batch item's servers have no interactive headroom
        left (slots >= interactive cap): shed the batch item (503) instead of letting it wait its 5 s."""
        batch = self.lanes["batch"]
        if not self.adaptive or not batch.total() or not (self.lanes["express"].total() + self.lanes["interactive"].total()):
            return
        for q in list(batch.queues.values()):
            for item in list(q):
                if item.state != "queued":
                    continue
                st = self.registry.get(item.tenant)
                cands = self._candidates(st, item.cls) if st else []
                if all(self.slots.get(s, 0) >= self.lane_cap("interactive", s) for s, _ in cands):
                    batch.cancel(item)
                    item.meta["shed"] = True
                    if not item.payload.done():
                        item.payload.set_result(None)

    def _release(self, item: Item) -> None:
        """Free the dispatch slot an item was granted (idempotent)."""
        if item.meta.get("released"):
            return
        item.meta["released"] = True
        self.slots[item.server] -= 1
        self._pump()

    # ----- the request path -----
    async def handle(self, method: str, path: str, query: str, query_keys: List[str],
                     headers: Dict[str, str], read_body: BodyReader, raw_path: Optional[bytes] = None) -> Result:
        """`headers` lower-cased. `read_body(limit)` returns the body, or None once it exceeds `limit` bytes;
        it is only called after the API key, path and Content-Length checks pass."""
        t0 = time.perf_counter()
        st = self.registry.lookup(headers.get("x-api-key"))
        if st is None:
            return Result(401, json={"error": "unauthorized", "detail": "missing or unknown X-API-Key"})
        fwd_path = safe_api_path(raw_path if raw_path is not None else path.encode("ascii", "replace"), path)
        if fwd_path is None:
            return Result(400, json={"error": "bad_path", "detail": "path must stay under /api/"})
        limit = self.s.body_limit_bytes
        cl = headers.get("content-length")
        if cl is not None:
            if not cl.isdigit():
                return Result(400, json={"error": "bad_content_length"})
            if int(cl) > limit:
                return Result(413, json={"error": "payload_too_large", "limit_bytes": limit})
        body = await read_body(limit)
        if body is None:
            return Result(413, json={"error": "payload_too_large", "limit_bytes": limit})
        tenant = st.id
        cls = request_class(method, path, query_keys, len(body))
        rid = headers.get("x-request-id")
        if not valid_request_id(rid):
            rid = uuid.uuid4().hex
        rl_headers: Dict[str, str] = {}
        fp = fingerprint(method, path, query, body)

        if self.adaptive:
            rv = self.retry.check(tenant, fp, headers.get("idempotency-key"), t0)
            if rv.is_retry:
                self.telemetry.retry(tenant)
            if not rv.allowed:
                dec = Decision(False, ["retry_budget"], rv.retry_after_s)
                return self._refuse(tenant, fp, rid, dec, {})
        if self.mode != "off":
            eff = self.effective_quota(st)
            st.configure(eff, self.s.cost_bucket_s, t0)
            blocked = self.overrides.block_remaining(tenant) if self.controlled else None
            dec = admit(st.tokens, st.cost, st.pending, eff, t0, blocked)
            rl_headers = ratelimit_headers(st.tokens, st.cost, st.pending + int(dec.ok), eff,
                                           self.s.cost_bucket_s, t0)
            if not dec.ok:
                return self._refuse(tenant, fp, rid, dec, rl_headers)

        self.telemetry.admitted(tenant)
        st.pending += 1
        item: Optional[Item] = None
        try:
            lane = self.lane_for(path, headers.get(H_CLASS.lower()), self.ref_cost(tenant, cls))
            max_wait_s = self.s.max_wait_ms.get("batch" if lane == "batch" else "interactive", 1000.0) / 1000.0
            fut = asyncio.get_running_loop().create_future()
            item = Item(tenant=tenant, cost=self.ref_cost(tenant, cls), cls=cls, payload=fut)
            self.lanes[lane].push(item)
            self._pump()
            expired = False
            try:
                await asyncio.wait({fut}, timeout=max_wait_s)
            finally:
                if item.state == "queued":          # timed out, or the client went away
                    expired = self.lanes[lane].cancel(item)
                    fut.cancel()
            shed = item.meta.get("shed", False)
            if expired or shed:
                wait_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.expired(tenant, wait_ms)
                if self.adaptive:
                    self.retry.failed(tenant, fp, time.perf_counter(), 1.0)
                err = "shed" if shed else "queue_timeout"
                return Result(503, headers={**rl_headers, "Retry-After": "1", H_REQUEST_ID: rid, H_LANE: lane},
                              json={"error": err, "tenant": tenant, "lane": lane, "waited_ms": round(wait_ms, 1),
                                    "max_wait_ms": max_wait_s * 1000})
            server = fut.result()
            t1 = time.perf_counter()
            self.telemetry.dispatched(tenant, (t1 - t0) * 1000)
            st.inflight += 1
            fwd = {k: v for k, v in headers.items() if not _client_must_not_send(k)}
            fwd.update({H_TENANT.lower(): tenant, H_CLASS.lower(): cls, H_REQUEST_ID.lower(): rid})
            url = self.workers[server] + fwd_path + (f"?{query}" if query else "")
            answered = False
            try:
                resp = await self.client.request(method, url, content=body, headers=fwd)
                status, content = resp.status_code, resp.content
                out = {k.lower(): v for k, v in resp.headers.items() if k.lower() not in RESP_DROP}
                answered = True
            except httpx.HTTPError as e:
                log.warning("upstream %s failed: %r", server, e)
                status, content, out = 502, b"", {}
                resp = None
                answered = True
            finally:
                st.inflight -= 1
                self._release(item)
                if not answered and self.mode != "off":
                    # Client went away mid-flight: the worker still burns the CPU. Charge the class estimate.
                    st.cost.debit(self.ref_cost(tenant, cls), time.perf_counter())
            t2 = time.perf_counter()
            cpu = _parse_cpu(resp.headers.get(H_CPU_MS)) if resp is not None else 0.0
            if cpu is None:
                self.telemetry.cpu_missing(tenant, server)
                cpu = 0.0
            if self.mode != "off" and cpu:
                st.cost.debit(cpu, t2)
            if resp is not None:
                self.limiters[server].observe((t2 - t1) * 1000, cpu)
                if cpu:
                    old = self.class_cost.get((tenant, cls))
                    self.class_cost[(tenant, cls)] = cpu if old is None else old + CLASS_COST_ALPHA * (cpu - old)
            if self.adaptive:
                if status >= 500 or status == 429:
                    self.retry.failed(tenant, fp, t2)
                else:
                    self.retry.succeeded(tenant, fp)
            self.telemetry.done(tenant, server, (t2 - t1) * 1000, (t2 - t0) * 1000, status, cpu)
            out.update({k.lower(): v for k, v in rl_headers.items()})
            out[H_REQUEST_ID.lower()] = rid
            out[H_SERVER.lower()] = server
            out[H_LANE.lower()] = lane
            if resp is None:
                return Result(502, headers=out, json={"error": "bad_gateway", "server": server})
            return Result(status, content, out)
        finally:
            st.pending -= 1
            # Granted a slot but cancelled (client gone) before forwarding: give the slot back.
            if (item is not None and item.payload.done() and not item.payload.cancelled()
                    and item.payload.result() is not None):
                self._release(item)

    def _refuse(self, tenant: str, fp: str, rid: str, dec: Decision, rl_headers: Dict[str, str]) -> Result:
        self.telemetry.rejected(tenant, dec.reasons)
        if self.adaptive:
            self.retry.failed(tenant, fp, time.perf_counter(), dec.retry_after_s)
        return Result(429, headers={**rl_headers, "Retry-After": dec.retry_after_header, H_REQUEST_ID: rid},
                      json={"error": "rate_limited", "tenant": tenant, "reasons": dec.reasons,
                            "retry_after_ms": round(dec.retry_after_s * 1000, 1)})

    # ----- brain commands that are not overrides -----
    def charge(self, cmd: ChargeCmd) -> str:
        """Debit background CPU from the tenant's cost bucket (tenantguard mode only, idempotent by charge_id)."""
        if not self.controlled:
            return "ignored: mode"
        if cmd.charge_id is not None:
            if cmd.charge_id in self.charge_ids:
                return "duplicate charge_id"
            self.charge_ids[cmd.charge_id] = None
            while len(self.charge_ids) > CHARGE_IDS_KEPT:
                self.charge_ids.popitem(last=False)
        st = self.registry.get(cmd.tenant)
        st.cost.debit(cmd.cpu_ms, time.perf_counter())
        self.telemetry.charged(cmd.tenant, cmd.cpu_ms)
        return f"charged {cmd.tenant} {cmd.cpu_ms:.3f} ms"

    # ----- telemetry tick -----
    def limiter_stats(self) -> List[LimiterStat]:
        return [LimiterStat(server=w, window=self.window(w), inflight=self.slots[w]) for w in self.workers]

    def tick(self) -> TelemetryFrame:
        self.overrides.sweep()
        if self.adaptive:
            for lim in self.limiters.values():
                lim.update()
        self._pump()     # a grown window or an expired drain/relocation may have freed a path
        reg = self.registry
        self.latest = self.telemetry.frame(
            self.mode, self.s.tick_ms, reg.ids(), self.queued,
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
