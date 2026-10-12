"""Stub gateway for lab development (a stand-in until the real gateway is merged).

  uvicorn stub_gateway:app --app-dir lab/tools --port 8090

Default: every /api/* request sleeps ~2 ms (on Windows the timer makes that ~15 ms).
STUB_FORWARD=1: pass-through to real workers instead. Each request goes to a random server of the tenant's
barcode (worker URLs from tenantguard_common.config.worker_urls(), i.e. TENANTGUARD_WORKER_URLS), with the
X-TenantGuard-Tenant header the worker requires. No fair scheduling: this is "off" mode with a naive limiter.

In any mode other than `off` a naive per-tenant STUB_RPS (default 60) token bucket answers 429 + Retry-After,
so attacker 429 counts are exercised. POST /admin/mode switches it."""
from __future__ import annotations

import asyncio
import os
import random
import time
import uuid

import httpx
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from tenantguard_common.config import load_tenants, worker_urls
from tenantguard_common.contracts import H_REQUEST_ID, H_TENANT

TENANTS = {t.api_key: t for t in load_tenants()}
FORWARD = os.environ.get("STUB_FORWARD") == "1"
WORKERS = worker_urls()
RPS = float(os.environ.get("STUB_RPS", "60"))
state = {"mode": "tenantguard"}
buckets: dict = {}
client = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=400, max_keepalive_connections=400))
app = FastAPI()


@app.api_route("/api/{path}", methods=["GET", "POST"])
async def api(path: str, request: Request, x_api_key: str = Header(None)):
    t = TENANTS.get(x_api_key)
    if t is None:
        return JSONResponse({"detail": "unknown api key"}, 401)
    if state["mode"] != "off":
        now = time.monotonic()
        tokens, last = buckets.get(t.id, (RPS, now))
        tokens = min(RPS, tokens + (now - last) * RPS)
        if tokens < 1:
            buckets[t.id] = (tokens, now)
            return JSONResponse({"detail": "rate"}, 429, headers={"Retry-After": "1"})
        buckets[t.id] = (tokens - 1, now)
    if not FORWARD:
        await asyncio.sleep(0.002)
        return {"tenant": t.id, "path": path}
    server = random.choice(t.servers)
    r = await client.request(request.method, f"{WORKERS[server]}/api/{path}", params=request.query_params,
                             content=await request.body(),
                             headers={H_TENANT: t.id, H_REQUEST_ID: uuid.uuid4().hex,
                                      "content-type": request.headers.get("content-type", "application/json")})
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


@app.get("/admin/mode")
async def get_mode():
    return state


@app.post("/admin/mode")
async def set_mode(body: dict):
    state["mode"] = body["mode"]
    return state
