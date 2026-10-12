"""Modest traffic for checking the UI against the real stack before the lab exists.

Calm Poisson traffic for all 12 tenants plus an optional whale (heavy /api/search) on one
tenant. Keys are the demo keys from layouts/ag23.json (tk_<ID>_demo).

    python ui/scripts/load.py --gateway http://127.0.0.1:8000 --rps 2 --whale K --whale-at 20 --whale-for 60
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections import Counter

import httpx

TENANTS = "ABCDEFGHIJKL"


async def calm(client: httpx.AsyncClient, tenant: str, rps: float, stop: float, stats: Counter) -> None:
    headers = {"X-API-Key": f"tk_{tenant}_demo"}
    while time.time() < stop:
        await asyncio.sleep(random.expovariate(rps))
        r = random.random()
        path = "/api/light" if r < 0.7 else "/api/read?key=k0001" if r < 0.95 else "/api/search?n=20000"
        asyncio.create_task(one(client, path, headers, stats, tenant))


async def one(client: httpx.AsyncClient, path: str, headers: dict, stats: Counter, tenant: str) -> None:
    try:
        resp = await client.get(path, headers=headers)
        stats[(tenant, resp.status_code)] += 1
    except httpx.HTTPError:
        stats[(tenant, "err")] += 1


async def whale(client: httpx.AsyncClient, tenant: str, start: float, end: float, n: int, conc: int, stats: Counter) -> None:
    await asyncio.sleep(max(0.0, start - time.time()))
    print(f"whale on {tenant} started (n={n}, concurrency={conc})", flush=True)
    headers = {"X-API-Key": f"tk_{tenant}_demo"}

    async def loop() -> None:
        while time.time() < end:
            await one(client, f"/api/search?n={n}", headers, stats, tenant)
            if stats[(tenant, 429)] and random.random() < 0.5:
                await asyncio.sleep(0.05)

    await asyncio.gather(*(loop() for _ in range(conc)))
    print(f"whale on {tenant} stopped", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--rps", type=float, default=2.0, help="calm requests/s per tenant")
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--whale", default="", help="tenant id for the whale, empty = none")
    ap.add_argument("--whale-at", type=float, default=20.0)
    ap.add_argument("--whale-for", type=float, default=60.0)
    ap.add_argument("--whale-n", type=int, default=400000)
    ap.add_argument("--whale-conc", type=int, default=3)
    a = ap.parse_args()

    t0 = time.time()
    stop = t0 + a.duration
    stats: Counter = Counter()
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=a.gateway, timeout=30, limits=limits) as client:
        tasks = [asyncio.create_task(calm(client, t, a.rps, stop, stats)) for t in TENANTS]
        if a.whale:
            tasks.append(asyncio.create_task(whale(client, a.whale, t0 + a.whale_at, t0 + a.whale_at + a.whale_for, a.whale_n, a.whale_conc, stats)))
        while time.time() < stop:
            await asyncio.sleep(10)
            codes = Counter()
            for (_, code), c in stats.items():
                codes[code] += c
            print(f"t={time.time() - t0:5.0f}s responses {dict(codes)}", flush=True)
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
