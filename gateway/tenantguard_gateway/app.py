"""FastAPI wiring: /api/* proxy, admin + control API, telemetry WS, /metrics."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import List, Optional, Set

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from tenantguard_common.config import load_settings, load_tenants, worker_urls
from tenantguard_common.contracts import (BlockCmd, ControlAck, DrainCmd, Heartbeat, LimitsCmd, ModeBody,
                                          OverridesView, QuotaPatch, RelocateCmd, RouteWeightsCmd,
                                          TenantAdminView, TenantConfig)

from .core import Gateway, Result
from .metrics import render_metrics
from .settings import GatewaySettings
from .tenants import quota_problems

log = logging.getLogger("tenantguard.gateway")
API_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _bad(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse(ControlAck(ok=False, detail=detail).model_dump(), status_code=status)


def _ok(detail: str = "") -> ControlAck:
    return ControlAck(ok=True, detail=detail)


def create_app(gw: Optional[Gateway] = None, run_ticker: bool = True) -> FastAPI:
    if gw is None:
        raw = load_settings()
        gw = Gateway(GatewaySettings.from_dict(raw), load_tenants(raw), worker_urls(raw))
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
        task = asyncio.create_task(ticker()) if run_ticker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await gw.aclose()

    app = FastAPI(title="TenantGuard gateway", lifespan=lifespan)
    app.state.gw = gw
    app.add_middleware(CORSMiddleware, allow_origins=gw.s.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["Retry-After", "RateLimit", "RateLimit-Policy"])

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Control input is validated to 400 (PLAN §2.9); admin keeps FastAPI's 422.
        status = 400 if request.url.path.startswith("/control/") else 422
        if status == 400:
            return _bad(f"invalid body: {exc.errors()}")
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    # ---------- data plane ----------
    @app.api_route("/api/{path:path}", methods=API_METHODS)
    async def proxy(path: str, request: Request):
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        res: Result = await gw.handle(request.method, "/api/" + path, request.url.query,
                                      list(request.query_params.keys()), headers, body)
        if res.json is not None:
            return JSONResponse(res.json, status_code=res.status, headers=res.headers)
        return Response(res.body, status_code=res.status, headers=res.headers)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "mode": gw.mode, "tenants": len(gw.registry.ids()), "workers": len(gw.workers)}

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

    # ---------- admin ----------
    @app.get("/admin/tenants", response_model=List[TenantAdminView])
    async def list_tenants():
        return [gw.admin_view(st) for st in gw.registry.by_id.values()]

    @app.get("/admin/tenants/{tenant}", response_model=TenantAdminView)
    async def get_tenant(tenant: str):
        st = gw.registry.get(tenant)
        if st is None:
            return JSONResponse({"detail": f"unknown tenant {tenant}"}, status_code=404)
        return gw.admin_view(st)

    @app.post("/admin/tenants", response_model=TenantAdminView, status_code=201)
    async def add_tenant(cfg: TenantConfig):
        errs = quota_problems(cfg.quota)
        unknown = [s for s in cfg.servers if s not in gw.workers]
        if unknown:
            errs.append(f"unknown servers {unknown}")
        if not cfg.servers:
            errs.append("servers must not be empty")
        if not cfg.weight > 0:
            errs.append("weight must be > 0")
        if errs:
            return JSONResponse({"detail": errs}, status_code=400)
        try:
            st = gw.registry.add(cfg, time.perf_counter())
        except KeyError as e:
            return JSONResponse({"detail": str(e.args[0])}, status_code=409)
        return gw.admin_view(st)

    @app.delete("/admin/tenants/{tenant}")
    async def delete_tenant(tenant: str):
        if gw.registry.remove(tenant) is None:
            return JSONResponse({"detail": f"unknown tenant {tenant}"}, status_code=404)
        return {"ok": True}

    @app.patch("/admin/tenants/{tenant}/quota", response_model=TenantAdminView)
    async def patch_quota(tenant: str, patch: QuotaPatch):
        st = gw.registry.get(tenant)
        if st is None:
            return JSONResponse({"detail": f"unknown tenant {tenant}"}, status_code=404)
        new = st.cfg.quota.model_copy(update=patch.model_dump(exclude_none=True))
        errs = quota_problems(new)
        if errs:
            return JSONResponse({"detail": errs}, status_code=400)
        st.cfg = st.cfg.model_copy(update={"quota": new})
        st.configure(gw.effective_quota(st), gw.s.cost_bucket_s, time.perf_counter())
        return gw.admin_view(st)

    @app.get("/admin/mode", response_model=ModeBody)
    async def get_mode():
        return ModeBody(mode=gw.mode)

    @app.post("/admin/mode", response_model=ModeBody)
    async def set_mode(body: ModeBody):
        gw.set_mode(body.mode)
        return ModeBody(mode=gw.mode)

    @app.get("/admin/overrides", response_model=OverridesView)
    async def overrides():
        return gw.overrides.view(gw.s.fail_static_after_s)

    # ---------- control (brain -> gateway) ----------
    def _check_ttl(ttl: float) -> Optional[str]:
        return None if ttl > 0 else "ttl_s must be > 0"

    def _check_tenant(t: str) -> Optional[str]:
        return None if gw.registry.get(t) else f"unknown tenant {t}"

    @app.post("/control/limits", response_model=ControlAck)
    async def control_limits(cmd: LimitsCmd):
        err = _check_tenant(cmd.tenant) or _check_ttl(cmd.ttl_s)
        if not err and not 0 < cmd.factor <= 1:
            err = "factor must be in (0, 1]"
        if not err and not 0 < cmd.weight_factor <= 1:
            err = "weight_factor must be in (0, 1]"
        if err:
            return _bad(err)
        gw.overrides.set_limits(cmd)
        gw._pump()
        cleared = cmd.factor == 1.0 and cmd.weight_factor == 1.0
        return _ok("cleared" if cleared else f"limits {cmd.tenant} factor={cmd.factor} "
                                             f"weight_factor={cmd.weight_factor} ttl={cmd.ttl_s}s")

    @app.post("/control/block", response_model=ControlAck)
    async def control_block(cmd: BlockCmd):
        err = _check_tenant(cmd.tenant) or _check_ttl(cmd.ttl_s)
        if err:
            return _bad(err)
        gw.overrides.set_block(cmd)
        return _ok(f"{'blocked' if cmd.blocked else 'unblocked'} {cmd.tenant}")

    @app.post("/control/heartbeat", response_model=ControlAck)
    async def control_heartbeat(_: Heartbeat):
        gw.overrides.heartbeat()
        return _ok()

    @app.post("/control/route_weights", response_model=ControlAck)
    async def control_route_weights(cmd: RouteWeightsCmd):
        err = _check_ttl(cmd.ttl_s)
        for t, w in cmd.weights.items():
            err = err or _check_tenant(t)
            if err:
                break
            bad = [s for s in w if s not in gw.workers]
            if bad:
                err = f"unknown servers {bad} for tenant {t}"
            elif any(x < 0 or x != x for x in w.values()):
                err = f"weights for {t} must be >= 0"
            elif not any(x > 0 for x in w.values()):
                err = f"weights for {t} must have at least one positive entry"
        if err:
            return _bad(err)
        gw.overrides.set_route_weights(cmd)
        gw._pump()
        return _ok(f"route weights for {sorted(cmd.weights)}")

    @app.post("/control/relocate", response_model=ControlAck)
    async def control_relocate(cmd: RelocateCmd):
        err = _check_tenant(cmd.tenant) or _check_ttl(cmd.ttl_s)
        if not err and cmd.target is not None and cmd.target not in gw.workers:
            err = f"unknown target {cmd.target}"
        if err:
            return _bad(err)
        gw.overrides.set_relocate(cmd)
        gw._pump()
        what = cmd.tenant + (f"/{cmd.cls}" if cmd.cls else "")
        return _ok(f"relocated {what} -> {cmd.target}" if cmd.target else f"relocation of {what} cleared")

    @app.post("/control/drain", response_model=ControlAck)
    async def control_drain(cmd: DrainCmd):
        err = _check_ttl(cmd.ttl_s) or (None if cmd.server in gw.workers else f"unknown server {cmd.server}")
        if err:
            return _bad(err)
        gw.overrides.set_drain(cmd)
        gw._pump()
        return _ok(f"{'drained' if cmd.drained else 'undrained'} {cmd.server}")

    return app


app = create_app()
