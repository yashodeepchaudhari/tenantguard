"""FastAPI app: UI stream (WS /ws/events) and read API on :8200."""
from __future__ import annotations

import asyncio
import hmac
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from tenantguard_common.contracts import H_CONTROL_TOKEN, ControlAck, IncidentEvent, OperatorCmd, StateSnapshot

from . import settings as settings_mod
from .runtime import Runtime


def create_app(cfg: Optional[settings_mod.BrainSettings] = None, start_loops: bool = True) -> FastAPI:
    cfg = cfg or settings_mod.load()
    rt = Runtime(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_loops:
            await rt.start()
        yield
        await rt.stop()

    app = FastAPI(title="TenantGuard brain", lifespan=lifespan)
    app.state.runtime = rt
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"],
                       allow_headers=["*"])

    @app.get("/healthz")
    def healthz() -> dict:
        return rt.health()

    @app.get("/api/state", response_model=StateSnapshot)
    def state() -> StateSnapshot:
        return rt.brain.snapshot(time.time())

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
        events = rt.brain.operator(cmd, time.time())
        rt._publish(events)
        return ControlAck(ok=True, detail=f"{cmd.decision} {cmd.tenant} by {cmd.by}"
                          + (f" for {cmd.until_s:g} s" if cmd.decision != "resume" else ""))

    @app.get("/api/debug")
    def debug() -> dict:
        return rt.brain.debug()

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        q = rt.hub.subscribe()

        async def pump() -> None:
            # backfill: the current state, then the recent incident log
            await ws.send_text(rt.brain.snapshot(time.time()).model_dump_json())
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
    logging.getLogger("httpx").setLevel(logging.WARNING)       # 10 polls/s would drown the incident log
    cfg = settings_mod.load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")
