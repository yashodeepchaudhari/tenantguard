"""End-to-end proof against a running gateway + workers (real HTTP, real WS).

Run: python tools/prove.py [--gateway http://127.0.0.1:8000]
Each tenant gets its own HTTP connection pool, as real tenants would. Keys come from the layout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from typing import Optional

import httpx
import websockets
from tenantguard_common.config import load_settings, load_tenants

KEYS = {t.id: t.api_key for t in load_tenants()}
GW = ""


class Run:
    def __init__(self) -> None:
        self.codes: Counter = Counter()
        self.reasons: Counter = Counter()
        self.lat: list = []
        self.sample: dict = {}

    def summary(self) -> dict:
        lat = sorted(self.lat)
        p = (lambda q: round(lat[int(q * (len(lat) - 1))], 1)) if lat else (lambda q: 0.0)
        return {"codes": dict(self.codes), "429_reasons": dict(self.reasons), "ok_p50_ms": p(0.5), "ok_p95_ms": p(0.95)}


async def _one(c: httpx.AsyncClient, run: Run, tenant: str, path: str) -> None:
    t0 = time.perf_counter()
    try:
        r = await c.get(path, headers={"X-API-Key": KEYS[tenant]})
    except httpx.HTTPError as e:
        run.codes[type(e).__name__] += 1
        return
    run.codes[r.status_code] += 1
    if r.status_code == 200:
        run.lat.append((time.perf_counter() - t0) * 1000)
    elif r.status_code == 429:
        run.reasons.update(r.json()["reasons"])
        if not run.sample:
            run.sample = {"Retry-After": r.headers.get("Retry-After"), "body": r.json(),
                          "RateLimit": r.headers.get("RateLimit"), "RateLimit-Policy": r.headers.get("RateLimit-Policy")}


async def open_loop(tenant: str, rps: float, seconds: float, path: str = "/api/light") -> Run:
    """Poisson-free open loop: one request every 1/rps s, regardless of answers (a well-behaved tenant)."""
    run = Run()
    async with httpx.AsyncClient(base_url=GW, timeout=30) as c:
        tasks, start = [], time.perf_counter()
        for i in range(int(rps * seconds)):
            delay = start + i / rps - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(_one(c, run, tenant, path)))
        await asyncio.gather(*tasks)
    return run


async def flood(tenant: str, conns: int, seconds: float, path: str = "/api/light") -> Run:
    """Closed loop as fast as `conns` connections allow, ignoring Retry-After (an abusive tenant)."""
    run = Run()
    end = time.perf_counter() + seconds
    async with httpx.AsyncClient(base_url=GW, timeout=30, limits=httpx.Limits(max_connections=conns)) as c:
        async def loop():
            while time.perf_counter() < end:
                await _one(c, run, tenant, path)
        await asyncio.gather(*[loop() for _ in range(conns)])
    return run


async def flood_proc(tenant: str, conns: int, seconds: float) -> dict:
    """Run a flood in a separate process so it cannot slow down this process's event loop (A's measurement)."""
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.abspath(__file__), "--gateway", GW, "--flood", tenant, str(conns), str(seconds),
        stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return json.loads(out.decode().strip().splitlines()[-1])


async def gateway_view(tenants, seconds: float) -> dict:
    """Max per-tick QueueStat values the gateway itself measured (end-to-end p95, queue wait p95)."""
    ws_url = GW.replace("http", "ws", 1) + "/internal/telemetry"
    out = {t: {"max_tick_p95_ms": 0.0, "max_tick_wait_p95_ms": 0.0, "completed": 0, "rejected_429": 0,
               "expired_503": 0} for t in tenants}
    end = time.perf_counter() + seconds
    async with websockets.connect(ws_url) as ws:
        while time.perf_counter() < end:
            f = json.loads(await ws.recv())
            for q in f["queues"]:
                if q["tenant"] in out:
                    o = out[q["tenant"]]
                    o["max_tick_p95_ms"] = max(o["max_tick_p95_ms"], q["p95_ms"])
                    o["max_tick_wait_p95_ms"] = max(o["max_tick_wait_p95_ms"], q["wait_p95_ms"])
                    for k in ("completed", "rejected_429", "expired_503"):
                        o[k] += q[k]
    return out


def show(title: str, obj) -> None:
    print(f"\n=== {title}")
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=1, default=str))


async def main() -> None:
    async with httpx.AsyncClient(base_url=GW, timeout=30) as c:
        r = await c.get("/api/light")
        r2 = await c.get("/api/light", headers={"X-API-Key": "tk_nobody"})
        show("1. identity: no key / unknown key", [r.status_code, r.json(), r2.status_code])

        r = await c.get("/api/light", headers={"X-API-Key": KEYS["A"]})
        show("2. A forwarded", {"status": r.status_code, "body": r.json(),
                                "X-TenantGuard-Server": r.headers.get("X-TenantGuard-Server"),
                                "RateLimit": r.headers.get("RateLimit"),
                                "RateLimit-Policy": r.headers.get("RateLimit-Policy")})

        await c.post("/admin/mode", json={"mode": "industry"})
        show("3. mode", (await c.get("/admin/mode")).json())

        calm, gv = await asyncio.gather(open_loop("A", 10, 4), gateway_view(["A"], 4))
        show("4a. A alone, 10 rps for 4 s (baseline)", {"client": calm.summary(), "gateway_telemetry": gv})
        k, a, gv = await asyncio.gather(flood_proc("K", 24, 6), open_loop("A", 10, 6), gateway_view(["A", "K"], 6))
        show("4b. K floods /api/light from another process (24 conns, closed loop, 6 s; pro quota 50 rps / "
             "burst 100) while A sends 10 rps", {"K": k, "A": a.summary(), "gateway_telemetry": gv})

        await asyncio.sleep(2.5)
        k, a = await asyncio.gather(open_loop("K", 4, 6, "/api/search?n=1000000"), open_loop("A", 10, 6))
        show("5. whale: K sends 4 rps of /api/search?n=1000000 (few but heavy; cost quota 300 CPU-ms/s) vs A",
             {"K": k.summary(), "K_sample_429": k.sample, "A": a.summary()})

        await asyncio.sleep(3)
        before = await open_loop("B", 10, 3)
        p = await c.patch("/admin/tenants/B/quota", json={"rate_rps": 2, "burst": 2})
        after = await open_loop("B", 10, 3)
        show("6. quota PATCH on B (10 rps offered for 3 s, before and after PATCH rate_rps=2 burst=2)",
             {"before": before.summary(), "patch_status": p.status_code, "patched": p.json()["quota"],
              "after": after.summary(), "after_sample_429": after.sample})
        p = await c.patch("/admin/tenants/B/quota", json={"rate_rps": 50, "burst": 100})
        show("6b. PATCH back", p.json()["quota"])

        await c.post("/admin/mode", json={"mode": "tenantguard"})
        await c.post("/control/heartbeat", json={"ts": time.time()})
        bad = await c.post("/control/limits", json={"tenant": "K", "factor": 1.5, "weight_factor": 1})
        ok = await c.post("/control/limits", json={"tenant": "K", "factor": 0.1, "weight_factor": 0.25,
                                                    "ttl_s": 3, "incident_id": "demo-1"})
        kv = (await c.get("/admin/tenants/K")).json()
        ov = (await c.get("/admin/overrides")).json()
        show("7. control limits on K (factor 0.1, ttl 3 s)",
             {"bad_factor": [bad.status_code, bad.json()], "ok": ok.json(), "K.factor": kv["factor"],
              "K.weight_factor": kv["weight_factor"], "K.effective_quota": kv["effective_quota"],
              "overrides.expires_at": ov["expires_at"], "last_heartbeat_age_s": ov["last_heartbeat_age_s"],
              "fail_static": ov["fail_static"]})
        await asyncio.sleep(3.3)
        kv = (await c.get("/admin/tenants/K")).json()
        ov = (await c.get("/admin/overrides")).json()
        show("8. 3.3 s later: TTL reverted; no heartbeat for > 2 s",
             {"K.factor": kv["factor"], "K.effective_quota": kv["effective_quota"], "overrides.limits": ov["limits"],
              "last_heartbeat_age_s": ov["last_heartbeat_age_s"], "fail_static": ov["fail_static"]})

        rel = await c.post("/control/relocate", json={"tenant": "K", "target": "spare", "ttl_s": 30})
        srv = Counter()
        for _ in range(10):
            r = await c.get("/api/light", headers={"X-API-Key": KEYS["K"]})
            srv[r.headers.get("X-TenantGuard-Server")] += 1
        clr = await c.post("/control/relocate", json={"tenant": "K", "target": None})
        srv2 = Counter()
        for _ in range(10):
            r = await c.get("/api/light", headers={"X-API-Key": KEYS["K"]})
            srv2[r.headers.get("X-TenantGuard-Server")] += 1
        show("9. relocate K -> spare, then clear", {"ack": rel.json(), "K served by": dict(srv),
                                                     "clear": clr.json(), "K served by after clear": dict(srv2)})

        ws_url = GW.replace("http", "ws", 1) + "/internal/telemetry"
        frames = []
        load = asyncio.ensure_future(asyncio.gather(flood("K", 8, 2), open_loop("A", 10, 2)))
        async with websockets.connect(ws_url) as ws:
            for _ in range(6):
                frames.append(json.loads(await ws.recv()))
        await load
        rows = []
        for f in frames:
            q = {x["tenant"]: x for x in f["queues"]}
            rows.append({"ts": round(f["ts"], 3), "mode": f["mode"], "n_cells": len(f["cells"]),
                         "K": {x: q["K"][x] for x in ("admitted", "rejected_429", "completed", "p95_ms", "depth")},
                         "A": {x: q["A"][x] for x in ("admitted", "completed", "p95_ms", "wait_p95_ms")}})
        show("10. WS /internal/telemetry: 6 consecutive frames", rows)
        show("10b. cells of the last frame (first 3)", frames[-1]["cells"][:3])

        m = (await c.get("/metrics")).text.splitlines()
        show("11. /metrics excerpt", "\n".join(
            l for l in m if ('tenant="K"' in l or 'tenant="A"' in l) and ("rejected" in l or "_count" in l)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=os.environ.get("TENANTGUARD_GATEWAY_URL",
                                                        load_settings()["services"]["gateway"]))
    ap.add_argument("--flood", nargs=3, metavar=("TENANT", "CONNS", "SECONDS"))
    args = ap.parse_args()
    GW = args.gateway
    if args.flood:
        t, n, sec = args.flood
        r = asyncio.run(flood(t, int(n), float(sec)))
        print(json.dumps({**r.summary(), "offered_rps": round(sum(r.codes.values()) / float(sec)), "sample_429": r.sample}))
    else:
        asyncio.run(main())
