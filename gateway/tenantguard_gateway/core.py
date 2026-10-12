"""The gateway engine: identify -> classify -> retry budget -> admit -> queue (per project: 3 DRR lanes / FIFO)
-> route (pin, else rendezvous over the project's ready replicas) -> forward -> measure.

Every project has its own queues and its own replicas, so a flood in one project never delays dequeue in
another (I6: a replica only serves its own project's tenants). Inside a project each lane is a weighted DRR
across tenants charged by the request's reference cost. Dispatch is event-driven: `_pump()` runs whenever
work is queued or a replica slot frees. A project with no ready replica gets a wake request to the brain and
its requests wait up to cold_wait_ms.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from typing import Awaitable, Callable, Dict, List, Optional

import httpx
from tenantguard_common.contracts import (H_CLASS, H_CPU_MS, H_REPLICA, H_REQUEST_ID, H_TENANT, LimiterStat, Quota,
                                          ReplicaView, Snapshot, TelemetryFrame, TenantAdminView)

from . import router
from .admission import Decision, admit, ratelimit_headers
from .costs import Costs, load_seed
from .limiter import GradientLimiter
from .overrides import Overrides
from .retry import RetryBudget, fingerprint
from .scheduler import Item, Scheduler
from .security import safe_api_path, valid_request_id
from .settings import DEFAULT_CLASS_COST_MS, GatewaySettings
from .telemetry import Telemetry
from .tenants import Registry, TenantState, request_class, scale_quota, size_bucket

log = logging.getLogger("tenantguard.gateway")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length", "x-api-key"}
RESP_DROP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
H_LANE = "X-TenantGuard-Lane"
H_SERVER = "X-TenantGuard-Server"

LANES = ("express", "interactive", "batch")
BodyReader = Callable[[int], Awaitable[Optional[bytes]]]
Waker = Callable[[str], Awaitable[None]]


def _client_must_not_send(name: str) -> bool:
    """Headers never forwarded from a client: hop-by-hop, the API key, anything the gateway owns."""
    return (name in HOP_BY_HOP or name.startswith("x-tenantguard-") or name in ("x-admin-token", "x-request-id"))


class Result:
    """What the HTTP layer turns into a response."""

    def __init__(self, status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None,
                 json: Optional[dict] = None):
        self.status, self.body, self.headers, self.json = status, body, headers or {}, json


class Gateway:
    def __init__(self, settings: GatewaySettings, client: Optional[httpx.AsyncClient] = None,
                 waker: Optional[Waker] = None):
        self.s = settings
        self.mode: str = settings.default_mode
        self.registry = Registry()
        self.overrides = Overrides()
        self.snapshot: Optional[Snapshot] = None
        self.replicas: Dict[str, List[ReplicaView]] = {}         # project -> replicas (from the snapshot)
        self.urls: Dict[str, str] = {}                             # replica id -> base URL
        self.queues: Dict[str, Dict[str, Scheduler]] = {}          # project -> lane -> DRR
        self.limiters: Dict[str, GradientLimiter] = {}
        self.slots: Dict[str, int] = {}
        self.costs = Costs(load_seed(settings.cost_seed))
        self.retry = RetryBudget(settings.retry_ratio, settings.retry_window_s, settings.retry_min_allowed)
        self.telemetry = Telemetry()
        self.latest: Optional[TelemetryFrame] = None
        self.loop_lag_ms = 0.0     # how late the last telemetry tick woke up (event-loop saturation)
        self.wake_sent: Dict[str, float] = {}
        self.waker = waker
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_s, connect=5.0),
            limits=httpx.Limits(max_connections=1024, max_keepalive_connections=256))

    # ----- policy views -----
    @property
    def controlled(self) -> bool:
        """Brain overrides (limits, pins) apply only in tenantguard mode."""
        return self.mode == "tenantguard"

    @property
    def adaptive(self) -> bool:
        """Lanes, adaptive windows and the retry budget: industry + tenantguard; off = FIFO, fixed window."""
        return self.mode != "off"

    def factors(self, tenant: str) -> tuple:
        cmd = self.overrides.limits_for(tenant) if self.controlled else None
        return (cmd.factor, cmd.weight_factor) if cmd else (1.0, 1.0)

    def effective_quota(self, st: TenantState) -> Quota:
        return scale_quota(st.cfg.quota, self.factors(st.id)[0])

    def _weight(self, tenant: str) -> float:
        st = self.registry.get(tenant)
        return (st.cfg.weight if st else 1.0) * self.factors(tenant)[1]

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        for lanes in self.queues.values():
            for s in lanes.values():
                s.fifo = mode == "off"
        self._pump()

    def lane_for(self, path: str, client_class: Optional[str], cost: float) -> str:
        if not self.adaptive:
            return "interactive"
        if (client_class or "").strip().lower() == "batch" or path in self.s.batch_routes:
            return "batch"
        return "express" if cost < self.s.express_cost_ms else "interactive"

    # ----- snapshot -----
    def on_snapshot(self, snap: Snapshot) -> None:
        """Swap in the brain's snapshot: tenants, replicas, limits, pins. Replica windows survive for replicas
        that stay; queued requests of removed tenants expire on their own."""
        now = time.perf_counter()
        self.registry.sync(snap.tenants, now)
        self.overrides.apply_snapshot(snap)
        self.replicas = {p: list(rs) for p, rs in snap.replicas.items()}
        self.urls = {r.id: r.url for rs in self.replicas.values() for r in rs}
        for rid in self.urls:
            if rid not in self.limiters:
                self.limiters[rid] = GradientLimiter(self.s.initial_window, self.s.min_window, self.s.max_window)
                self.slots.setdefault(rid, 0)
        for rid in [r for r in self.limiters if r not in self.urls and not self.slots.get(r)]:
            self.limiters.pop(rid, None)
            self.slots.pop(rid, None)
        self.snapshot = snap
        self._pump()

    def ready(self, project: str) -> List[str]:
        return [r.id for r in self.replicas.get(project, ()) if r.state == "ready"]

    # ----- windows -----
    def window(self, rid: str) -> int:
        lim = self.limiters.get(rid)
        return lim.window if lim and self.adaptive else self.s.initial_window

    def lane_cap(self, lane: str, rid: str) -> int:
        """Slots a lane may occupy on a replica: express the whole window; interactive leaves headroom for
        express; batch at most half (it is shed first when the replica saturates)."""
        w = self.window(rid)
        if not self.adaptive or lane == "express":
            return w
        interactive = max(1, w - max(1, w // 8))
        if lane == "interactive":
            return interactive
        return max(1, min(interactive, w // 2))

    def _lanes(self, project: str) -> Dict[str, Scheduler]:
        lanes = self.queues.get(project)
        if lanes is None:
            lanes = self.queues[project] = {
                lane: Scheduler(self._weight, quantum_ms=DEFAULT_CLASS_COST_MS, fifo=self.mode == "off") for lane in LANES}
        return lanes

    def queued(self, tenant: Optional[str] = None) -> int:
        if tenant is None:
            return sum(s.total() for lanes in self.queues.values() for s in lanes.values())
        st = self.registry.get(tenant)
        lanes = self.queues.get(st.project) if st else None
        return sum(s.depth(tenant) for s in lanes.values()) if lanes else 0

    def queued_cost(self, tenant: str) -> float:
        st = self.registry.get(tenant)
        lanes = self.queues.get(st.project) if st else None
        if not lanes:
            return 0.0
        return sum(i.cost for s in lanes.values() for i in s.queues.get(tenant, ()) if i.state == "queued")

    # ----- routing / dispatch -----
    def _route(self, item: Item, lane: str) -> Optional[str]:
        st = self.registry.get(item.tenant)
        if st is None:
            return None
        pin = self.overrides.pin_for(item.tenant) if self.controlled else None
        return router.choose(item.tenant, self.ready(st.project),
                             lambda r: self.slots.get(r, 0) < self.lane_cap(lane, r), pin)

    def _eligible_in(self, lane: str):
        def eligible(item: Item) -> bool:
            item.server = self._route(item, lane)
            return item.server is not None
        return eligible

    def _pump_project(self, project: str) -> None:
        lanes = self.queues[project]
        while True:
            item = None
            for lane in LANES:
                item = lanes[lane].pop(self._eligible_in(lane))
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
        self._shed_batch(project)

    def _pump(self) -> None:
        for project in list(self.queues):
            self._pump_project(project)

    def _shed_batch(self, project: str) -> None:
        """Interactive/express work is waiting and the project's replicas have no interactive headroom left:
        shed queued batch items (503) instead of letting them wait their 5 s."""
        lanes = self.queues[project]
        batch = lanes["batch"]
        if not self.adaptive or not batch.total() or not (lanes["express"].total() + lanes["interactive"].total()):
            return
        if any(self.slots.get(r, 0) < self.lane_cap("interactive", r) for r in self.ready(project)):
            return
        for q in list(batch.queues.values()):
            for item in list(q):
                if item.state == "queued" and batch.cancel(item):
                    item.meta["shed"] = True
                    if not item.payload.done():
                        item.payload.set_result(None)

    def _release(self, item: Item) -> None:
        """Free the dispatch slot an item was granted (idempotent)."""
        if item.meta.get("released"):
            return
        item.meta["released"] = True
        if item.server in self.slots:
            self.slots[item.server] -= 1
        self._pump()

    def request_wake(self, project: str) -> None:
        """Ask the brain to start a replica for a project that has none ready (debounced, fire-and-forget)."""
        now = time.monotonic()
        if self.waker is None or now - self.wake_sent.get(project, -1e9) < self.s.wake_debounce_s:
            return
        self.wake_sent[project] = now
        asyncio.get_running_loop().create_task(self.waker(project))

    # ----- the request path -----
    async def handle(self, method: str, path: str, query: str, query_params: Dict[str, str],
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
        cls = request_class(method, path, query_params.keys(), len(body), size_bucket(query_params, self.s.size_params))
        cost = self.costs.reference_cost(tenant, cls, method, path)
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
                return self._refuse(tenant, fp, rid, Decision(False, ["retry_budget"], rv.retry_after_s), {}, cost)
        if self.mode != "off":
            eff = self.effective_quota(st)
            st.configure(eff, t0)
            blocked = self.overrides.block_remaining(tenant) if self.controlled else None
            dec = admit(st.tokens, st.pending, eff, t0, blocked)
            rl_headers = ratelimit_headers(st.tokens, st.pending + int(dec.ok), eff, t0)
            if not dec.ok:
                return self._refuse(tenant, fp, rid, dec, rl_headers, cost)

        self.telemetry.admitted(tenant)
        st.pending += 1
        item: Optional[Item] = None
        try:
            lane = self.lane_for(path, headers.get(H_CLASS.lower()), cost)
            max_wait_s = self.s.max_wait_ms.get("batch" if lane == "batch" else "interactive", 1000.0) / 1000.0
            if not self.ready(st.project):
                self.request_wake(st.project)
                max_wait_s = max(max_wait_s, self.s.cold_wait_ms / 1000.0)
            fut = asyncio.get_running_loop().create_future()
            item = Item(tenant=tenant, cost=cost, cls=cls, payload=fut)
            sched = self._lanes(st.project)[lane]
            sched.push(item)
            self._pump()
            expired = False
            try:
                await asyncio.wait({fut}, timeout=max_wait_s)
            finally:
                if item.state == "queued":          # timed out, or the client went away
                    expired = sched.cancel(item)
                    fut.cancel()
            shed = item.meta.get("shed", False)
            if expired or shed:
                wait_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.expired(tenant, wait_ms, cost)
                if self.adaptive:
                    self.retry.failed(tenant, fp, time.perf_counter(), 1.0)
                err = "shed" if shed else "queue_timeout"
                return Result(503, headers={**rl_headers, "Retry-After": "1", H_REQUEST_ID: rid, H_LANE: lane},
                              json={"error": err, "tenant": tenant, "lane": lane, "waited_ms": round(wait_ms, 1),
                                    "max_wait_ms": max_wait_s * 1000})
            req = (method, fwd_path, query, headers, body)
            return await self._forward(st, item, lane, req, (rid, fp, rl_headers, t0))
        finally:
            st.pending -= 1
            # Granted a slot but cancelled (client gone) before forwarding: give the slot back.
            if (item is not None and item.payload.done() and not item.payload.cancelled()
                    and item.payload.result() is not None):
                self._release(item)

    async def _forward(self, st: TenantState, item: Item, lane: str, req: tuple, ctx: tuple) -> Result:
        """Send one dispatched request to its replica and record what it cost."""
        method, fwd_path, query, headers, body = req
        rid, fp, rl_headers, t0 = ctx
        tenant, replica = st.id, item.payload.result()
        t1 = time.perf_counter()
        self.telemetry.dispatched(tenant, (t1 - t0) * 1000)
        st.inflight += 1
        fwd = {k: v for k, v in headers.items() if not _client_must_not_send(k)}
        fwd.update({H_TENANT.lower(): tenant, H_CLASS.lower(): item.cls, H_REQUEST_ID.lower(): rid,
                    H_REPLICA.lower(): replica})
        url = self.urls.get(replica, "") + fwd_path + (f"?{query}" if query else "")
        resp = None
        try:
            resp = await self.client.request(method, url, content=body, headers=fwd)
            status, content = resp.status_code, resp.content
            out = {k.lower(): v for k, v in resp.headers.items() if k.lower() not in RESP_DROP}
        except httpx.HTTPError as e:
            log.warning("upstream %s failed: %r", replica, e)
            status, content, out = 502, b"", {}
        finally:
            st.inflight -= 1
            self._release(item)
        self._measure(tenant, item, replica, resp, status, fp, t0, t1)
        out.update({k.lower(): v for k, v in rl_headers.items()})
        out.update({H_REQUEST_ID.lower(): rid, H_SERVER.lower(): replica, H_LANE.lower(): lane})
        if resp is None:
            return Result(502, headers=out, json={"error": "bad_gateway", "server": replica})
        return Result(status, content, out)

    def _measure(self, tenant: str, item: Item, replica: str, resp: Optional[httpx.Response], status: int,
                 fp: str, t0: float, t1: float) -> None:
        """Feed the measured CPU to the cost estimate and the replica window; record telemetry and retries."""
        t2 = time.perf_counter()
        cpu = _parse_cpu(resp.headers.get(H_CPU_MS)) if resp is not None else 0.0
        if cpu is None:
            self.telemetry.cpu_missing(tenant, replica)
            cpu = 0.0
        if resp is not None and replica in self.limiters:
            self.limiters[replica].observe((t2 - t1) * 1000, cpu)
        if cpu:
            self.costs.update(tenant, item.cls, cpu)
        if self.adaptive:
            if status >= 500 or status == 429:
                self.retry.failed(tenant, fp, t2)
            else:
                self.retry.succeeded(tenant, fp)
        self.telemetry.done(tenant, replica, (t2 - t1) * 1000, (t2 - t0) * 1000, status, cpu)

    def _refuse(self, tenant: str, fp: str, rid: str, dec: Decision, rl_headers: Dict[str, str],
                cost: float) -> Result:
        self.telemetry.rejected(tenant, dec.reasons, cost)
        if self.adaptive:
            self.retry.failed(tenant, fp, time.perf_counter(), dec.retry_after_s)
        return Result(429, headers={**rl_headers, "Retry-After": dec.retry_after_header, H_REQUEST_ID: rid},
                      json={"error": "rate_limited", "tenant": tenant, "reasons": dec.reasons,
                            "retry_after_ms": round(dec.retry_after_s * 1000, 1)})

    # ----- telemetry tick -----
    def limiter_stats(self) -> List[LimiterStat]:
        return [LimiterStat(server=r, window=self.window(r), inflight=self.slots.get(r, 0)) for r in self.urls]

    def tick(self) -> TelemetryFrame:
        self.overrides.sweep()
        if self.adaptive:
            for lim in self.limiters.values():
                lim.update()
        for project, lanes in self.queues.items():       # still waiting for a replica: ask again (debounced)
            if not self.ready(project) and any(s.total() for s in lanes.values()):
                self.request_wake(project)
        self._pump()     # a grown window or an expired pin may have freed a path
        reg = self.registry
        self.latest = self.telemetry.frame(
            self.mode, self.s.tick_ms, reg.ids(), self.queued,
            lambda t: reg.get(t).inflight if reg.get(t) else 0, self.limiter_stats(),
            queued_cost=self.queued_cost)
        return self.latest

    # ----- admin views -----
    def admin_view(self, st: TenantState) -> TenantAdminView:
        factor, wf = self.factors(st.id)
        return TenantAdminView(
            id=st.id, name=st.cfg.name, project=st.project, tier=st.cfg.tier,
            replicas=router.rank(st.id, self.ready(st.project)), weight=st.cfg.weight, quota=st.cfg.quota,
            effective_quota=self.effective_quota(st), factor=factor, weight_factor=wf,
            blocked=self.controlled and self.overrides.block_remaining(st.id) is not None,
            pinned_to=self.overrides.pin_for(st.id) if self.controlled else None,
            override_expires_at=self.overrides.tenant_expiry(st.id),
            monthly_revenue_rs=st.cfg.monthly_revenue_rs, protected=st.cfg.protected)

    async def aclose(self) -> None:
        await self.client.aclose()


def _parse_cpu(v: Optional[str]) -> Optional[float]:
    """Worker CPU ms from the header; None if missing or unparseable (counted, never raises)."""
    if v is None:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if x >= 0 and math.isfinite(x) else None
