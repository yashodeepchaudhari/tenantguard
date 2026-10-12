"""FastAPI app on :8200: admin API (pool, projects, tenants), gateway control (snapshot, wake), operator,
UI stream (WS /ws/events) and read API."""
from __future__ import annotations

import asyncio
import hmac
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tenantguard_common.contracts import (H_CONTROL_TOKEN, ControlAck, IncidentEvent, OperatorCmd, PoolSpec, PoolView,
                                          ProjectPatch, ProjectSpec, Snapshot, StateSnapshot, TenantCreate,
                                          TenantCreated, WakeCmd)

from . import registry as R
from . import settings as settings_mod
from .runtime import Runtime


def _registry_call(rt: Runtime, change: Callable[[], R.Registry]) -> Optional[JSONResponse]:
    """Run a registry change; on success apply it, else map the error to 404 / 409 / 422."""
    try:
        reg = change()
    except R.NotFound as e:
        return JSONResponse({"detail": f"unknown {e.args[0]}"}, status_code=404)
    except R.Conflict as e:
        return JSONResponse({"detail": str(e)}, status_code=409)
    except R.Invalid as e:
        return JSONResponse({"detail": str(e)}, status_code=422)
    rt.set_registry(reg)
    return None


def admin_router(rt: Runtime, require_token) -> APIRouter:
    api = APIRouter(dependencies=[Depends(require_token)])
    scale = rt.cfg.control.scale

    @api.get("/admin/registry")
    def get_registry() -> dict:
        reg = rt.control.registry
        return {"pool": reg.pool.model_dump(), "projects": [p.model_dump() for p in reg.projects.values()],
                "tenants": [t.model_dump(exclude={"key_sha256"}) for t in reg.tenants.values()],
                "version": reg.version}

    @api.put("/admin/pool", response_model=PoolView)
    def put_pool(pool: PoolSpec):
        return _registry_call(rt, lambda: R.set_pool(rt.control.registry, pool, scale)) or rt.control.pool_view()

    @api.post("/admin/projects", response_model=PoolView, status_code=201)
    def post_project(spec: ProjectSpec):
        return _registry_call(rt, lambda: R.create_project(rt.control.registry, spec, scale)) or rt.control.pool_view()

    @api.patch("/admin/projects/{pid}", response_model=PoolView)
    def patch_project(pid: str, patch: ProjectPatch):
        return (_registry_call(rt, lambda: R.update_project(rt.control.registry, pid, patch, scale))
                or rt.control.pool_view())

    @api.delete("/admin/projects/{pid}", response_model=PoolView)
    def delete_project(pid: str):
        return _registry_call(rt, lambda: R.remove_project(rt.control.registry, pid)) or rt.control.pool_view()

    @api.post("/admin/projects/{pid}/tenants", response_model=TenantCreated, status_code=201)
    def post_tenant(pid: str, req: TenantCreate):
        out = {}

        def change() -> R.Registry:
            reg, out["key"] = R.add_tenant(rt.control.registry, pid, req, scale)
            return reg
        err = _registry_call(rt, change)
        return err or TenantCreated(tenant=req.id, project=pid, api_key=out["key"])

    @api.delete("/admin/tenants/{tid}")
    def delete_tenant(tid: str):
        return _registry_call(rt, lambda: R.remove_tenant(rt.control.registry, tid)) or {"ok": True}

    @api.get("/snapshot", response_model=Snapshot)
    def snapshot():
        return rt.last_snapshot or rt.control.snapshot(rt.brain.limits())

    @api.post("/control/wake", response_model=ControlAck)
    def wake(cmd: WakeCmd):
        if not rt.wake(cmd.project):
            return JSONResponse(ControlAck(ok=False, detail=f"unknown project {cmd.project}").model_dump(), status_code=404)
        return ControlAck(ok=True, detail=f"waking {cmd.project}")

    return api


def create_app(cfg: Optional[settings_mod.BrainSettings] = None, start_loops: bool = True,
               runtime: Optional[Runtime] = None) -> FastAPI:
    cfg = cfg or settings_mod.load()
    rt = runtime or Runtime(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_loops:
            await rt.start()
        yield
        await rt.stop()

    app = FastAPI(title="TenantGuard brain", lifespan=lifespan)
    app.state.runtime = rt
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"], allow_headers=["*"])

    def require_token(request: Request) -> None:
        if not hmac.compare_digest(request.headers.get(H_CONTROL_TOKEN, ""), cfg.control_token):
            raise HTTPException(status_code=401, detail=f"missing or wrong {H_CONTROL_TOKEN}")

    app.include_router(admin_router(rt, require_token))

    @app.get("/healthz")
    def healthz() -> dict:
        return rt.health()

    @app.get("/api/state", response_model=StateSnapshot)
    def state() -> StateSnapshot:
        return rt.brain.snapshot(time.time(), rt.control.pool_view())

    @app.get("/api/pool", response_model=PoolView)
    def pool() -> PoolView:
        return rt.control.pool_view()

    @app.get("/api/incidents", response_model=List[IncidentEvent])
    def incidents(incident_id: Optional[str] = None, tenant: Optional[str] = None,
                  limit: int = 200) -> List[IncidentEvent]:
        return rt.brain.book.events(incident_id=incident_id, tenant=tenant, limit=limit)

    @app.post("/api/operator", response_model=ControlAck)
    def operator(cmd: OperatorCmd, token: Optional[str] = Header(None, alias=H_CONTROL_TOKEN)):
        def fail(code: int, detail: str):
            return JSONResponse(ControlAck(ok=False, detail=detail).model_dump(), status_code=code)
        if not hmac.compare_digest(token or "", cfg.control_token):
            return fail(401, "missing or wrong control token")
        if not math.isfinite(cmd.until_s) or not (0 < cmd.until_s <= cfg.ttl_max_s):
            return fail(400, f"until_s must be finite and in (0, {cfg.ttl_max_s:g}]")
        if cmd.tenant not in rt.brain.tenants:
            return fail(404, f"unknown tenant {cmd.tenant}")
        rt.publish(rt.brain.operator(cmd, time.time()))
        rt.kick.set()
        return ControlAck(ok=True, detail=f"{cmd.decision} {cmd.tenant} by {cmd.by}"
                          + (f" for {cmd.until_s:g} s" if cmd.decision != "resume" else ""))

    @app.get("/api/debug")
    def debug() -> dict:
        return {**rt.brain.debug(), "pool": rt.control.pool_view().model_dump()}

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        q = rt.hub.subscribe()

        async def pump() -> None:
            # backfill: the current state, then the recent incident log
            await ws.send_text(rt.brain.snapshot(time.time(), rt.control.pool_view()).model_dump_json())
            for ev in rt.brain.book.events(limit=100):
                await ws.send_text(ev.model_dump_json())
            while True:
                await ws.send_text(await q.get())

        async def drain() -> None:
            # the UI never sends anything we need, but reading is how a disconnect is noticed
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(drain())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    pass
                except Exception:
                    logging.getLogger("tenantguard.brain").debug("ui client dropped", exc_info=True)
            rt.hub.unsubscribe(q)

    return app


def run() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # replica polls would drown the incident log
    cfg = settings_mod.load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")
