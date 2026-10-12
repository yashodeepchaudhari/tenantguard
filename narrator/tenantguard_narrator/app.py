"""FastAPI wiring for the narrator (:8700): brain consumer, report API, WS push."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import List, Optional

import httpx2 as httpx   # anthropic 1.x ships httpx2; reuse it for the brain backfill
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from tenantguard_common.config import load_settings, load_tenants
from tenantguard_common.contracts import ReportView

from .ai import AIWriter
from .service import Narrator
from .settings import NarratorSettings

log = logging.getLogger("tenantguard.narrator")


def make_writer(s: NarratorSettings) -> Optional[AIWriter]:
    if not s.ai_enabled:
        return None
    import anthropic
    return AIWriter(anthropic.AsyncAnthropic(), s.model, s.effort, s.max_tokens, s.timeout_s)


async def consume_brain(n: Narrator, brain_url: str, state: dict) -> None:
    """Backfill from /api/incidents, then follow WS /ws/events; reconnect with backoff forever."""
    ws_url = brain_url.replace("http", "ws", 1).rstrip("/") + "/ws/events"
    backoff = 0.5
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(brain_url.rstrip("/") + "/api/incidents", params={"limit": 500})
                if r.status_code == 200:
                    for ev in r.json():
                        n.ingest(ev)
            async with websockets.connect(ws_url, open_timeout=5, ping_interval=20) as ws:
                state["brain_connected"], backoff = True, 0.5
                async for raw in ws:
                    try:
                        n.ingest(json.loads(raw))
                    except ValueError:
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as e:                  # brain down or restarting: keep trying, never crash
            log.info("brain unavailable (%s); retrying in %.1fs", type(e).__name__, backoff)
        state["brain_connected"] = False
        await asyncio.sleep(backoff)
        backoff = min(10.0, backoff * 2)


def create_app(narrator: Optional[Narrator] = None, settings: Optional[NarratorSettings] = None,
               follow_brain: bool = True) -> FastAPI:
    raw = load_settings() if (narrator is None or settings is None) else None
    s = settings or NarratorSettings.from_dict(raw)
    n = narrator or Narrator(load_tenants(raw), make_writer(s))
    state = {"brain_connected": False}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(consume_brain(n, s.brain_url, state)) if follow_brain else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for t in list(n.tasks):
                t.cancel()

    app = FastAPI(title="TenantGuard narrator", lifespan=lifespan)
    app.state.narrator = n
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["GET"], allow_headers=["*"])

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "brain_connected": state["brain_connected"], "ai_enabled": n.writer is not None,
                "model": s.model if n.writer else None, "reports": len(n.reports), **n.stats}

    @app.get("/reports", response_model=List[ReportView])
    async def reports(limit: int = 50):
        return n.listing()[: max(1, min(limit, 500))]

    @app.get("/reports/{incident_id}", response_model=ReportView)
    async def report(incident_id: str):
        v = n.reports.get(incident_id)
        if v is None:
            return JSONResponse({"detail": f"no report for {incident_id}"}, status_code=404)
        return v

    @app.websocket("/ws/reports")
    async def ws_reports(ws: WebSocket):
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        n.subscribers.add(q)

        async def sender():
            for v in reversed(n.listing()[:50]):          # backfill, oldest first
                await ws.send_text(v.model_dump_json())
            while True:
                await ws.send_text(await q.get())

        async def watcher():
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(sender()), asyncio.create_task(watcher())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        finally:
            n.subscribers.discard(q)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
