"""FastAPI app: UI stream (WS /ws/events) and read API on :8200."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from tenantguard_common.contracts import IncidentEvent, StateSnapshot

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
