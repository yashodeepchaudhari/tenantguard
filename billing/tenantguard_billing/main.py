"""TenantGuard billing: fair bills and "Why was I slow?" on :8600. Read-only: never sends control commands.

Inputs: every worker's /stats (cumulative counters), the brain's /api/incidents + WS /ws/events
(StateSnapshot at 2 Hz and IncidentEvents), the gateway's /admin/tenants (names/tiers), the layout.
Run: TENANTGUARD_ROOT=<repo> python -m tenantguard_billing --port 8600
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List, Optional

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from tenantguard_common.contracts import (H_CONTROL_TOKEN, BillView, IncidentEvent, StateSnapshot, TenantAdminView,
                                          WhyView, WorkerStats)

from . import settings as settings_mod
from .bill import _Ctx, build_bill, build_why
from .meter import Meter
from .pricing import Pricer
from .settings import BillingSettings
from .timeline import LatencyLog, Timeline

log = logging.getLogger("billing")

WHY_DEFAULT_WINDOW_S = 900


class Billing:
    """All state; pure methods, so tests drive it directly without any network."""

    def __init__(self, cfg: BillingSettings, started_at: Optional[float] = None) -> None:
        self.cfg = cfg
        self.started_at = time.time() if started_at is None else started_at
        self.meter = Meter(cfg.bucket_s, self.started_at, cfg.retention_s)
        self.pricer = Pricer(cfg.ref_alpha, cfg.bucket_s, settle_s=2 * cfg.poll_s)
        self.timeline = Timeline()
        self.latency = LatencyLog()
        self.sources: Dict[str, dict] = {}

    def ctx(self, now: float) -> _Ctx:
        return _Ctx(self.cfg, self.meter, self.pricer, self.timeline, self.latency, now)

    def on_stats(self, s: WorkerStats) -> None:
        self.meter.ingest(s)

    def on_events(self, evs: List[IncidentEvent]) -> None:
        self.timeline.ingest(evs)

    def on_snapshot(self, snap: StateSnapshot) -> None:
        self.latency.ingest(snap)

    def tick(self, now: float) -> None:
        self.pricer.close(self.meter, self.timeline, now)
        cutoff = now - self.cfg.retention_s
        self.meter.prune(now)
        self.pricer.prune(cutoff)
        self.timeline.prune(cutoff)
        self.latency.prune(cutoff)

    def bill(self, tenant: str, start: Optional[float], end: Optional[float], now: float) -> BillView:
        return build_bill(self.ctx(now), tenant, self.started_at if start is None else start,
                          now if end is None else end)

    def why(self, tenant: str, start: Optional[float], end: Optional[float], now: float) -> WhyView:
        end = now if end is None else end
        return build_why(self.ctx(now), tenant, end - WHY_DEFAULT_WINDOW_S if start is None else start, end)

    def apply_admin_tenants(self, views: List[TenantAdminView]) -> None:
        for v in views:
            t = self.cfg.tenants.get(v.id)
            if t is not None:  # name/tier can change at runtime; barcodes come from the layout
                self.cfg.tenants[v.id] = t.model_copy(update={"name": v.name, "tier": v.tier})


# ------------------------------------------------------------------ ingest loops
def _ok(b: Billing, source: str, ok: bool, detail: str = "") -> None:
    st = b.sources.setdefault(source, {"ok": False, "last_ok": None, "error": ""})
    st["ok"] = ok
    if ok:
        st["last_ok"] = time.time()
        st["error"] = ""
    else:
        st["error"] = detail[:200]


async def poll_loop(b: Billing, client: httpx.AsyncClient) -> None:
    cfg = b.cfg
    while True:
        async def one(name: str, url: str) -> None:
            try:
                r = await client.get(url + "/stats")
                r.raise_for_status()
                b.on_stats(WorkerStats(**r.json()))
                _ok(b, f"worker:{name}", True)
            except Exception as e:  # a down worker never stops billing
                _ok(b, f"worker:{name}", False, repr(e))

        await asyncio.gather(*(one(n, u) for n, u in cfg.worker_urls.items()))
        try:
            r = await client.get(cfg.brain_url + "/api/incidents", params={"limit": 1000})
            r.raise_for_status()
            b.on_events([IncidentEvent(**e) for e in r.json()])
            _ok(b, "brain:incidents", True)
        except Exception as e:
            _ok(b, "brain:incidents", False, repr(e))
        try:
            r = await client.get(cfg.gateway_url + "/admin/tenants", headers={H_CONTROL_TOKEN: cfg.control_token})
            r.raise_for_status()
            b.apply_admin_tenants([TenantAdminView(**t) for t in r.json()])
            _ok(b, "gateway:tenants", True)
        except Exception as e:
            _ok(b, "gateway:tenants", False, repr(e))
        b.tick(time.time())
        await asyncio.sleep(cfg.poll_s)


def handle_ws_message(b: Billing, raw: str) -> None:
    msg = json.loads(raw)
    kind = msg.get("type")
    if kind == "state":
        b.on_snapshot(StateSnapshot(**msg))
    elif kind == "incident":
        b.on_events([IncidentEvent(**msg)])


async def ws_loop(b: Billing) -> None:
    url = b.cfg.brain_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/events"
    while True:
        try:
            async with websockets.connect(url, open_timeout=5, max_size=2**22) as ws:
                _ok(b, "brain:ws", True)
                async for raw in ws:
                    try:
                        handle_ws_message(b, raw)
                        _ok(b, "brain:ws", True)
                    except Exception as e:
                        log.warning("bad brain message: %r", e)
        except Exception as e:
            _ok(b, "brain:ws", False, repr(e))
        await asyncio.sleep(1.0)


# ------------------------------------------------------------------ app
def create_app(cfg: Optional[BillingSettings] = None, *, ingest: bool = True,
               billing: Optional[Billing] = None) -> FastAPI:
    cfg = cfg or settings_mod.load()
    b = billing or Billing(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks = []
        client = httpx.AsyncClient(timeout=max(1.0, cfg.poll_s * 0.8))
        if ingest:
            tasks = [asyncio.ensure_future(poll_loop(b, client)), asyncio.ensure_future(ws_loop(b))]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()

    app = FastAPI(title="tenantguard-billing", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.billing = b

    def known(tenant: str) -> str:
        if tenant not in cfg.tenants:  # exact id match only
            raise HTTPException(404, f"unknown tenant {tenant!r}")
        return tenant

    def window(start: Optional[float], end: Optional[float]) -> None:
        if start is not None and end is not None and end <= start:
            raise HTTPException(400, "'to' must be after 'from'")

    # handlers are async: they run on the event loop, so they never race the ingest loops
    @app.get("/bills", response_model=List[BillView])
    async def bills(start: Optional[float] = Query(None, alias="from"),
                    end: Optional[float] = Query(None, alias="to")) -> List[BillView]:
        window(start, end)
        now = time.time()
        return [b.bill(t, start, end, now) for t in sorted(cfg.tenants)]

    @app.get("/bills/{tenant}", response_model=BillView)
    async def bill(tenant: str, start: Optional[float] = Query(None, alias="from"),
                   end: Optional[float] = Query(None, alias="to")) -> BillView:
        window(start, end)
        return b.bill(known(tenant), start, end, time.time())

    @app.get("/why/{tenant}", response_model=WhyView)
    async def why(tenant: str, start: Optional[float] = Query(None, alias="from"),
                  end: Optional[float] = Query(None, alias="to")) -> WhyView:
        window(start, end)
        return b.why(known(tenant), start, end, time.time())

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "started_at": b.started_at, "sources": b.sources,
                "episodes": len(b.timeline.episodes), "ticks": len(b.latency.ticks),
                "buckets": len(b.meter.buckets), "worker_restarts": dict(b.meter.restarts)}

    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["GET"], allow_headers=["*"])
    return app
