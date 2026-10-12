"""FastAPI wiring: /api/* proxy, admin views, control (snapshot from the brain, operator blocks), telemetry WS,
/metrics. Tenants, projects and replicas come only from the brain's snapshot (the brain owns the registry)."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import math
import time
from typing import List, Optional, Set

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from tenantguard_common.config import ROOT, load_settings
from tenantguard_common.contracts import (H_CONTROL_TOKEN, BlockCmd, ControlAck, ModeBody, OverridesView, Snapshot,
                                          TenantAdminView, WakeCmd)

from .core import Gateway, Result
from .metrics import render_metrics
from .settings import GatewaySettings

log = logging.getLogger("tenantguard.gateway")
API_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _bad(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse(ControlAck(ok=False, detail=detail).model_dump(), status_code=status)


def _ok(detail: str = "") -> ControlAck:
    return ControlAck(ok=True, detail=detail)


def snapshot_problems(snap: Snapshot, ttl_max_s: float) -> Optional[str]:
    """Why a snapshot must be refused (400), or None."""
    tenants = {t.id for t in snap.tenants}
    for c in snap.limits:
        if c.tenant not in tenants:
            return f"limits for unknown tenant {c.tenant}"
        if not (0 < c.factor <= 1 and 0 < c.weight_factor <= 1):
            return f"limits for {c.tenant}: factor and weight_factor must be in (0, 1]"
        if not (math.isfinite(c.ttl_s) and 0 < c.ttl_s <= ttl_max_s):
            return f"limits for {c.tenant}: ttl_s must be finite and in (0, {ttl_max_s:g}]"
    known = {r.id for rs in snap.replicas.values() for r in rs}
    bad = sorted(t for t, r in snap.pins.items() if t not in tenants or r not in known)
    return f"pins for unknown tenants or replicas: {bad}" if bad else None


def brain_waker(gw: Gateway):
    """POST /control/wake to the brain for a project with no ready replica (best effort)."""
    async def wake(project: str) -> None:
        try:
            await gw.client.post(gw.s.brain_url + "/control/wake", json=WakeCmd(project=project).model_dump(),
                                 headers={H_CONTROL_TOKEN: gw.s.control_token}, timeout=2.0)
        except httpx.HTTPError as e:
            log.info("wake %s not delivered: %s", project, type(e).__name__)
    return wake


async def fetch_snapshot(gw: Gateway) -> None:
    """Until the first snapshot arrives (gateway start or restart), pull it from the brain every second."""
    while gw.snapshot is None:
        try:
            r = await gw.client.get(gw.s.brain_url + "/snapshot", headers={H_CONTROL_TOKEN: gw.s.control_token},
                                    timeout=2.0)
            if r.status_code == 200 and gw.snapshot is None:
                gw.on_snapshot(Snapshot.model_validate(r.json()))
        except (httpx.HTTPError, ValueError) as e:
            log.info("GET brain /snapshot failed (%s); retrying", type(e).__name__)
        await asyncio.sleep(1.0)


def create_app(gw: Optional[Gateway] = None, run_loops: bool = True) -> FastAPI:
    if gw is None:
        gw = Gateway(GatewaySettings.from_dict(load_settings(), root=str(ROOT)))
        gw.waker = brain_waker(gw)
    subscribers: Set[asyncio.Queue] = set()

    async def ticker() -> None:
        period = gw.s.tick_ms / 1000.0
        nxt = time.perf_counter()
        while True:
            nxt += period
            await asyncio.sleep(max(0.0, nxt - time.perf_counter()))
            lag = time.perf_counter() - nxt
            gw.loop_lag_ms = max(0.0, lag * 1000)
            if lag > period:                       # fell behind by more than a tick: don't burst-catch-up
                nxt = time.perf_counter()
            try:
                frame = gw.tick()
            except Exception:                      # never let telemetry kill the gateway
                log.exception("tick failed")
                continue
            msg = frame.model_dump_json()
            for q in list(subscribers):
                if q.full():                       # slow consumer: drop its oldest frame
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                q.put_nowait(msg)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [asyncio.create_task(ticker()), asyncio.create_task(fetch_snapshot(gw))] if run_loops else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await gw.aclose()

    app = FastAPI(title="TenantGuard gateway", lifespan=lifespan)
    app.state.gw = gw
    app.add_middleware(CORSMiddleware, allow_origins=gw.s.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["Retry-After", "RateLimit", "RateLimit-Policy"])

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Control input is validated to 400 (PLAN §2.9); admin keeps FastAPI's 422.
        if request.url.path.startswith("/control/"):
            return _bad(f"invalid body: {exc.errors()}")
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    # ---------- data plane ----------
    @app.api_route("/api/{path:path}", methods=API_METHODS)
    async def proxy(path: str, request: Request):
        headers = {k.lower(): v for k, v in request.headers.items()}

        async def read_body(limit: int) -> Optional[bytes]:
            buf = bytearray()
            async for chunk in request.stream():
                buf += chunk
                if len(buf) > limit:
                    return None
            return bytes(buf)

        res: Result = await gw.handle(request.method, request.scope["path"], request.url.query,
                                      dict(request.query_params), headers, read_body,
                                      raw_path=request.scope.get("raw_path"))
        if res.json is not None:
            return JSONResponse(res.json, status_code=res.status, headers=res.headers)
        return Response(res.body, status_code=res.status, headers=res.headers)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "mode": gw.mode, "tenants": len(gw.registry.ids()), "replicas": len(gw.urls),
                "snapshot_version": gw.overrides.snapshot_version}

    # ---------- telemetry ----------
    @app.websocket("/internal/telemetry")
    async def telemetry_ws(ws: WebSocket):
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        subscribers.add(q)

        async def sender() -> None:
            if gw.latest is not None:
                await ws.send_text(gw.latest.model_dump_json())
            while True:
                await ws.send_text(await q.get())

        async def watcher() -> None:        # notice the client leaving even when no frame is due
            while True:
                if (await ws.receive())["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(sender()), asyncio.create_task(watcher())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        finally:
            subscribers.discard(q)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @app.get("/internal/telemetry/latest")
    async def telemetry_latest():
        return (gw.latest or gw.tick()).model_dump()

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(render_metrics(gw), media_type="text/plain; version=0.0.4")

    # ---------- admin + control: both need the control token ----------
    expected = gw.s.control_token.encode("utf-8")

    def require_token(request: Request) -> None:
        got = request.headers.get(H_CONTROL_TOKEN, "").encode("utf-8")
        if not hmac.compare_digest(got, expected):
            raise HTTPException(status_code=401, detail=f"missing or wrong {H_CONTROL_TOKEN}")

    admin = APIRouter(dependencies=[Depends(require_token)])
    control = APIRouter(dependencies=[Depends(require_token)])

    @admin.get("/admin/tenants", response_model=List[TenantAdminView])
    async def list_tenants():
        return [gw.admin_view(st) for st in gw.registry.by_id.values()]

    @admin.get("/admin/tenants/{tenant}", response_model=TenantAdminView)
    async def get_tenant(tenant: str):
        st = gw.registry.get(tenant)
        if st is None:
            return JSONResponse({"detail": f"unknown tenant {tenant}"}, status_code=404)
        return gw.admin_view(st)

    @admin.get("/admin/mode", response_model=ModeBody)
    async def get_mode():
        return ModeBody(mode=gw.mode)

    @admin.post("/admin/mode", response_model=ModeBody)
    async def set_mode(body: ModeBody):
        gw.set_mode(body.mode)
        return ModeBody(mode=gw.mode)

    @admin.get("/admin/overrides", response_model=OverridesView)
    async def overrides():
        return gw.overrides.view(gw.s.fail_static_after_s)

    @admin.get("/admin/costs")
    async def costs():
        return {"seed": gw.costs.seed,
                "measured": {f"{t}/{c}": round(v, 3) for (t, c), v in sorted(gw.costs.measured.items())}}

    @control.post("/control/snapshot", response_model=ControlAck)
    async def control_snapshot(snap: Snapshot):
        err = snapshot_problems(snap, gw.s.ttl_max_s)
        if err:
            return _bad(err)
        gw.on_snapshot(snap)
        return _ok(f"snapshot v{snap.version}: {len(snap.tenants)} tenants, "
                   f"{sum(len(r) for r in snap.replicas.values())} replicas")

    @control.post("/control/block", response_model=ControlAck)
    async def control_block(cmd: BlockCmd):
        if gw.registry.get(cmd.tenant) is None:
            return _bad(f"unknown tenant {cmd.tenant}")
        if not (math.isfinite(cmd.ttl_s) and 0 < cmd.ttl_s <= gw.s.ttl_max_s):
            return _bad(f"ttl_s must be finite and in (0, {gw.s.ttl_max_s:g}]")
        gw.overrides.set_block(cmd)
        return _ok(f"{'blocked' if cmd.blocked else 'unblocked'} {cmd.tenant}")

    app.include_router(admin)
    app.include_router(control)
    return app


app = create_app()
