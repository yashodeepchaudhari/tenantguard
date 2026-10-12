"""Wave 2 proof against a running gateway + real workers.

  python tools/prove_t7.py whale  LABEL   # whale K saturates workers while A sends light requests (run twice:
                                          # gateway with express_cost_ms=5 and with 0) + window time series
  python tools/prove_t7.py storm          # retry storm capped by the retry budget
  python tools/prove_t7.py security       # traversal / token / ttl over a real socket
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from collections import Counter
from urllib.parse import urlparse

import httpx
import websockets
from tenantguard_common.config import load_settings, load_tenants
from tenantguard_common.contracts import H_CONTROL_TOKEN

GW = os.environ.get("TENANTGUARD_GATEWAY_URL", load_settings()["services"]["gateway"])
KEYS = {t.id: t.api_key for t in load_tenants()}
TOKEN = {H_CONTROL_TOKEN: os.environ.get("TENANTGUARD_CONTROL_TOKEN", "dev")}
WHALE = "/api/search?n=600000"          # ~200 ms of CPU on the T2 worker


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[int(q * (len(xs) - 1))], 1) if xs else 0.0


async def whale_loop(conns: int, seconds: float) -> Counter:
    codes: Counter = Counter()
    end = time.perf_counter() + seconds
    async with httpx.AsyncClient(base_url=GW, timeout=30, limits=httpx.Limits(max_connections=conns)) as c:
        async def loop():
            while time.perf_counter() < end:
                try:
                    r = await c.get(WHALE, headers={"X-API-Key": KEYS["K"]})
                    codes[r.status_code] += 1
                except httpx.HTTPError as e:
                    codes[type(e).__name__] += 1
        await asyncio.gather(*[loop() for _ in range(conns)])
    return codes


async def light(tenant: str, rps: float, seconds: float):
    lat, lanes, codes = [], Counter(), Counter()
    async with httpx.AsyncClient(base_url=GW, timeout=30) as c:
        async def one():
            t0 = time.perf_counter()
            r = await c.get("/api/light", headers={"X-API-Key": KEYS[tenant]})
            codes[r.status_code] += 1
            if r.status_code == 200:
                lat.append((time.perf_counter() - t0) * 1000)
                lanes[r.headers.get("x-tenantguard-lane")] += 1
        tasks, start = [], time.perf_counter()
        for i in range(int(rps * seconds)):
            d = start + i / rps - time.perf_counter()
            if d > 0:
                await asyncio.sleep(d)
            tasks.append(asyncio.create_task(one()))
        await asyncio.gather(*tasks)
    return {"codes": dict(codes), "lanes": dict(lanes), "p50_ms": pct(lat, .5), "p95_ms": pct(lat, .95),
            "max_ms": pct(lat, 1)}


async def watch(seconds: float):
    """Gateway's own view: A's per-tick wait p95, K's queue depth, worker windows over time."""
    out = {"A_wait_p95_ms": [], "A_p95_ms": [], "K_depth": [], "windows": []}
    end = time.perf_counter() + seconds
    async with websockets.connect(GW.replace("http", "ws", 1) + "/internal/telemetry") as ws:
        while time.perf_counter() < end:
            f = json.loads(await ws.recv())
            q = {x["tenant"]: x for x in f["queues"]}
            if q["A"]["completed"]:
                out["A_wait_p95_ms"].append(round(q["A"]["wait_p95_ms"], 1))
                out["A_p95_ms"].append(round(q["A"]["p95_ms"], 1))
            out["K_depth"].append(q["K"]["depth"])
            out["windows"].append({l["server"]: l["window"] for l in f["limiter"] if l["server"] == "w1"})
    return out


async def whale(label: str) -> None:
    async with httpx.AsyncClient(base_url=GW, timeout=30, headers=TOKEN) as c:
        await c.post("/admin/mode", json={"mode": "tenantguard"})
        # One real worker process per logical worker: pin both tenants to w1 so one limiter governs one process.
        await c.post("/control/route_weights", json={"weights": {"K": {"w1": 1}, "A": {"w1": 1}}, "ttl_s": 120})
        # Give K a huge plan so the whale is limited only by the workers (the gateway queue is what we test).
        await c.patch("/admin/tenants/K/quota", json={"rate_rps": 1000, "burst": 1000, "cost_ms_per_s": 100000,
                                                       "concurrency": 64})
        for _ in range(3):                       # teach the gateway A's light class cost (express needs a measurement)
            await c.get("/api/light", headers={"X-API-Key": KEYS["A"]})
        base = await light("A", 10, 4)
        w0 = (await c.get("/internal/telemetry/latest")).json()["limiter"]
        wh = asyncio.ensure_future(whale_loop(10, 14))
        await asyncio.sleep(3)
        a, view = await asyncio.gather(light("A", 10, 9), watch(9))
        k = await wh
        await asyncio.sleep(2)
        after = (await c.get("/internal/telemetry/latest")).json()["limiter"]
        await c.patch("/admin/tenants/K/quota", json={"rate_rps": 50, "burst": 100, "cost_ms_per_s": 300,
                                                       "concurrency": 8})
        await c.post("/control/route_weights", json={"weights": {"K": {"w1": 1}, "A": {"w1": 1}}, "ttl_s": 0.5})
    wins = view["windows"]
    print(json.dumps({
        "label": label,
        "A_baseline": base, "A_during_whale": a, "K_whale_codes": dict(k),
        "gateway_A_wait_p95_per_tick": {"median": pct(view["A_wait_p95_ms"], .5), "max": pct(view["A_wait_p95_ms"], 1)},
        "gateway_A_e2e_p95_per_tick": {"median": pct(view["A_p95_ms"], .5), "max": pct(view["A_p95_ms"], 1)},
        "K_queue_depth": {"median": pct(view["K_depth"], .5), "max": max(view["K_depth"] or [0])},
        "windows_before": {l["server"]: l["window"] for l in w0 if l["server"] == "w1"},
        "windows_during (every 8th tick)": wins[::8],
        "windows_2s_after": {l["server"]: l["window"] for l in after if l["server"] == "w1"},
    }, indent=1))


async def storm() -> None:
    async with httpx.AsyncClient(base_url=GW, timeout=30, headers=TOKEN) as c:
        await c.post("/admin/mode", json={"mode": "industry"})
        await c.patch("/admin/tenants/K/quota", json={"rate_rps": 2, "burst": 2})
        m0 = (await c.get("/metrics")).text
        codes, reasons = Counter(), Counter()
        sent = 0
        calm = asyncio.ensure_future(light("A", 5, 6))
        end = time.perf_counter() + 6
        async with httpx.AsyncClient(base_url=GW, timeout=30) as k:
            while time.perf_counter() < end:          # ignores Retry-After entirely
                r = await k.get("/api/read?key=k0001", headers={"X-API-Key": KEYS["K"]})
                sent += 1
                codes[r.status_code] += 1
                if r.status_code == 429:
                    reasons.update(r.json()["reasons"])
        a = await calm
        m1 = (await c.get("/metrics")).text
        await c.patch("/admin/tenants/K/quota", json={"rate_rps": 50, "burst": 100})

    def metric(text, name):
        return [l for l in text.splitlines() if l.startswith(name)]
    print(json.dumps({"K_storm_sent": sent, "K_codes": dict(codes), "K_429_reasons": dict(reasons),
                      "A_during_storm": a,
                      "metrics_before": metric(m0, 'tenantguard_retries_total{tenant="K"}'),
                      "metrics_after": metric(m1, 'tenantguard_retries_total{tenant="K"}')
                      + metric(m1, 'tenantguard_rejected_total{tenant="K",reason="retry_budget"}')}, indent=1))


def raw_get(path: str, headers: dict) -> str:
    u = urlparse(GW)
    s = socket.create_connection((u.hostname, u.port or 80), timeout=5)
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\n{hdr}Connection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.split(b"\r\n")[0].decode() + " " + body[:120].decode(errors="replace")


async def security() -> None:
    key = {"X-API-Key": KEYS["A"]}
    out = {p: raw_get(p, key) for p in ("/api/%2e%2e/admin/fault", "/api/..%2fadmin/fault", "/api/./light",
                                         "/api/x/../../admin/fault", "/api/light")}
    async with httpx.AsyncClient(base_url=GW, timeout=10) as c:
        out["GET /admin/tenants without token"] = (await c.get("/admin/tenants")).status_code
        out["POST /control/limits ttl Infinity"] = (await c.post(
            "/control/limits", headers={**TOKEN, "content-type": "application/json"},
            content='{"tenant":"K","factor":0.5,"weight_factor":1,"ttl_s":Infinity}')).status_code
        out["GET /admin/overrides afterwards"] = (await c.get("/admin/overrides", headers=TOKEN)).status_code
        out["PATCH quota rate_rps 1e9"] = (await c.patch("/admin/tenants/A/quota", headers=TOKEN,
                                                         json={"rate_rps": 1e9})).status_code
        r = await c.get("/api/light", headers={**key, "X-Request-Id": "bad id!"})
        out["bad X-Request-Id replaced with"] = r.headers.get("x-request-id")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "whale":
        asyncio.run(whale(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == "storm":
        asyncio.run(storm())
    else:
        asyncio.run(security())
