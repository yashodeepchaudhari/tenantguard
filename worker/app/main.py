"""TenantGuard worker: the shared multi-tenant "SaaS app".

Run: `python -m app --port 8101` (from worker/), env SERVER_NAME=w1.
"""
from __future__ import annotations

import csv
import functools
import hashlib
import io
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenantguard_common.contracts import H_CLASS, H_CPU_MS, H_TENANT, WorkerStats

from . import work
from .cache import TenantCache
from .config import WorkerConfig, load_config
from .cpuclock import thread_cpu
from .fault import FaultInjector, FaultReq
from .jobs import BackgroundJobs
from .memory import MB, MemoryBudget, OverBudget
from .meter import Meters, Usage
from .stats import ProcessTree, cgroup_cpu_limit
from .store import TenantStore
from .tenancy import NO_CLASS, RequestMeter, request_var, tenant_var, valid_tenant

log = logging.getLogger("worker")

SEARCH_MAX_N = 2_000_000
INDEX_MAX_ROUNDS = 20_000


class Worker:
    """All per-process state; one instance per app so tests stay independent."""

    def __init__(self, cfg: WorkerConfig) -> None:
        self.cfg = cfg
        self.meters = Meters()
        self.store = TenantStore(cfg.data_dir, self.meters, cfg.seed_rows)
        self.cache = TenantCache(int(cfg.cache_mb * MB), cfg.cache_guaranteed_share)
        self.memory = MemoryBudget(cfg.tenant_mem_budget_mb, self.cache.tenant_bytes)
        self.jobs = BackgroundJobs(self.meters, cfg.bg_threads)
        self.fault = FaultInjector()
        self.tree = ProcessTree()
        self.store.reset_and_seed(cfg.tenants)

    def metered(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a sync handler: apply fault latency, then measure the handler's own thread CPU."""

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.fault.apply_latency()
            meter = request_var.get()
            t0 = thread_cpu()
            try:
                return fn(*args, **kwargs)
            finally:
                if meter is not None:
                    meter.cpu_ms += (thread_cpu() - t0) * 1000

        return wrapper

    def cpu_limit_cores(self) -> float:
        if self.cfg.cpu_limit_env is not None:
            return self.cfg.cpu_limit_env
        return cgroup_cpu_limit() or self.cfg.cpus

    def stats(self) -> WorkerStats:
        usage, classes = self.meters.snapshot()
        tenants = sorted(set(self.cfg.tenants) | set(usage))
        rows = []
        for t in tenants:
            u = usage.get(t) or Usage()
            rows.append(Meters.to_contract(t, u, mem_mb=round(self.memory.used_mb(t), 3), **self.cache.counters(t)))
        return WorkerStats(
            server=self.cfg.server, ts=time.time(), started_at=self.tree.started_at,
            cpu_total_ms=round(self.tree.cpu_total_ms(), 3), blackbox_cpu_ms=0.0,
            cpu_limit_cores=self.cpu_limit_cores(), mem_mb=round(self.tree.rss_mb(), 2),
            fault=self.fault.current(), tenants=rows, classes=classes)

    def close(self) -> None:
        self.fault.clear()
        self.jobs.shutdown()
        self.store.close()


def _error(status: int, error: str, detail: str, tenant: Optional[str] = None) -> JSONResponse:
    return JSONResponse({"error": error, "tenant": tenant, "detail": detail}, status_code=status)


class TenantMiddleware:
    """Pure ASGI (same task as the endpoint, so contextvars flow into the threadpool).

    - enforces the body limit (413) by buffering at most body_limit_bytes
    - /api/*: requires a valid X-TenantGuard-Tenant (400) and sets tenant_var
    - sets X-TenantGuard-Cpu-Ms (handler thread CPU, ms) on every response
    - records the request in the tenant's cumulative meters
    """

    def __init__(self, app: ASGIApp, worker: Worker) -> None:
        self.app = app
        self.w = worker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        tenant = headers.get(H_TENANT)
        meter = RequestMeter(tenant=None, cls=headers.get(H_CLASS) or NO_CLASS)

        async def send_with_cpu(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[H_CPU_MS] = f"{meter.cpu_ms:.3f}"
            await send(message)

        async def reject(status: int, error: str, detail: str) -> None:
            await _error(status, error, detail, tenant)(scope, receive, send_with_cpu)

        limit = self.w.cfg.body_limit_bytes
        declared = headers.get("content-length")
        if declared is not None:
            if not declared.isdigit():
                return await reject(400, "bad_request", "invalid Content-Length")
            if int(declared) > limit:
                return await reject(413, "payload_too_large", f"body {declared} B exceeds {limit} B")
        body = bytearray()
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > limit:
                return await reject(413, "payload_too_large", f"body exceeds {limit} B")

        if scope["path"].startswith("/api/"):
            if not tenant:
                return await reject(400, "missing_tenant", f"{H_TENANT} header is required")
            if not valid_tenant(tenant):
                return await reject(400, "bad_tenant", f"invalid {H_TENANT}")
            meter.tenant = tenant
            meter.bytes_in = len(body)
        else:
            tenant = None

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        t_tok = tenant_var.set(meter.tenant)
        r_tok = request_var.set(meter)
        try:
            await self.app(scope, replay, send_with_cpu)
        finally:
            request_var.reset(r_tok)
            tenant_var.reset(t_tok)
            if meter.tenant:
                self.w.meters.record_request(meter.tenant, meter.cls, meter.cpu_ms, meter.bytes_in)


class WriteReq(BaseModel):
    key: Optional[str] = Field(None, min_length=1, max_length=128)
    value: str
    index_rounds: int = Field(200, ge=0, le=INDEX_MAX_ROUNDS)  # background indexing cost knob


def create_app(cfg: Optional[WorkerConfig] = None) -> FastAPI:
    cfg = cfg or load_config()
    w = Worker(cfg)
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        w.close()

    app = FastAPI(title=f"tenantguard-worker {cfg.server}", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.worker = w
    m = w.metered

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        err = exc.detail if isinstance(exc.detail, dict) else {"error": "http_error", "detail": str(exc.detail)}
        return _error(exc.status_code, err["error"], err["detail"], tenant_var.get())

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", str(exc.errors()), tenant_var.get())

    # ---- tenant API ----
    @app.get("/api/light")
    @m
    def light() -> dict:
        return {"ok": True, "server": cfg.server, "tenant": tenant_var.get(), "v": work.light()}

    @app.get("/api/read")
    @m
    def read(key: str = Query(..., min_length=1, max_length=128)) -> dict:
        value = w.cache.get(key)
        source = "cache"
        if value is None:
            value = w.store.get(key)
            source = "db"
            if value is None:
                raise HTTPException(404, {"error": "not_found", "detail": f"key {key!r} not found"})
            w.cache.put(key, value)
        return {"key": key, "value": value, "source": source, "server": cfg.server}

    @app.get("/api/search")
    @m
    def search(n: int = Query(20_000, ge=1, le=SEARCH_MAX_N), q: str = Query("tenantguard", max_length=128)) -> dict:
        hits = work.search(n, q)
        return {"n": n, "q": q, "hits": [{"doc": d, "score": s} for s, d in hits], "server": cfg.server}

    def index_job(key: str, value: str, rounds: int) -> None:
        w.store.add_terms(key, work.index_terms(value, rounds))

    @app.post("/api/write")
    @m
    def write(req: WriteReq) -> dict:
        key = req.key or f"u{uuid.uuid4().hex[:12]}"
        w.store.put(key, req.value)
        w.cache.put(key, req.value)
        w.jobs.submit(index_job, key, req.value, req.index_rounds)
        return {"key": key, "bytes": len(req.value), "indexing": "queued", "server": cfg.server}

    @app.get("/api/memory")
    @m
    def memory(mb: float = Query(..., gt=0, le=1024), hold_ms: float = Query(500, ge=0, le=10_000)) -> dict:
        try:
            return {**w.memory.hold(mb, hold_ms / 1000), "server": cfg.server}
        except OverBudget as e:
            raise HTTPException(507, {"error": "tenant_memory_budget", "detail": str(e)})

    @app.get("/api/export")
    @m
    def export(rows: int = Query(1000, ge=1, le=100_000), chunk: int = Query(100, ge=1, le=10_000),
               pause_ms: float = Query(20, ge=0, le=1000)) -> dict:
        """Batch export: page through the tenant's rows into CSV, pausing between chunks (slow sink)."""
        digest = hashlib.sha256()
        done, size, after = 0, 0, ""
        while done < rows:
            page = w.store.page(after, min(chunk, rows - done))
            if not page:
                break
            buf = io.StringIO()
            csv.writer(buf).writerows(page)
            data = buf.getvalue().encode()
            digest.update(data)
            size += len(data)
            done += len(page)
            after = page[-1][0]
            if pause_ms:
                time.sleep(pause_ms / 1000)
        return {"rows": done, "bytes": size, "sha256": digest.hexdigest(), "server": cfg.server}

    # ---- probes / ops ----
    def canary_probe(kind: str) -> dict:
        t0 = time.perf_counter()
        if kind == "cpu":
            work.light()
        elif kind == "db":
            w.store.canary_probe()
        elif kind == "cache":
            token = tenant_var.set("_canary")
            try:
                w.cache.put("probe", "x" * 256)
                w.cache.get("probe")
            finally:
                tenant_var.reset(token)
        else:
            raise HTTPException(404, {"error": "not_found", "detail": f"unknown canary {kind!r}"})
        return {"kind": kind, "ms": round((time.perf_counter() - t0) * 1000, 3)}

    @app.get("/canary")
    @m
    def canary() -> dict:
        return {"ok": True, "server": cfg.server, "probes": [canary_probe(k) for k in ("cpu", "cache", "db")]}

    @app.get("/canary/{kind}")
    @m
    def canary_kind(kind: str) -> dict:
        return {"ok": True, "server": cfg.server, **canary_probe(kind)}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "server": cfg.server, "bg_pending": w.jobs.pending}

    @app.get("/stats", response_model=WorkerStats)
    def stats() -> WorkerStats:
        return w.stats()

    # ---- admin (fault injection) ----
    def check_admin(token: Optional[str]) -> None:
        if token != cfg.admin_token:
            raise HTTPException(401, {"error": "unauthorized", "detail": "X-Admin-Token missing or wrong"})

    @app.post("/admin/fault")
    def set_fault(req: FaultReq = Body(...), x_admin_token: Optional[str] = Header(None)) -> dict:
        check_admin(x_admin_token)
        log.warning("fault injected on %s: %s", cfg.server, req)
        return {"server": cfg.server, "fault": w.fault.set(req)}

    @app.get("/admin/fault")
    def get_fault(x_admin_token: Optional[str] = Header(None)) -> dict:
        check_admin(x_admin_token)
        return {"server": cfg.server, "fault": w.fault.current()}

    @app.delete("/admin/fault")
    def clear_fault(x_admin_token: Optional[str] = Header(None)) -> dict:
        check_admin(x_admin_token)
        w.fault.clear()
        return {"server": cfg.server, "fault": None}

    # add_middleware: last added is outermost, so CORS answers preflights before tenant checks.
    app.add_middleware(TenantMiddleware, worker=w)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=[H_CPU_MS])
    return app


def __getattr__(name: str) -> Any:
    # `uvicorn app.main:app` builds the app lazily so importing this module has no side effects.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
