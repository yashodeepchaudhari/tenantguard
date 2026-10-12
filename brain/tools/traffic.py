"""Light calm traffic for every tenant + an optional whale, through the real gateway.

    python brain/tools/traffic.py --gateway http://127.0.0.1:8030 --calm 20 --attack 40 --after 40 \
        --rps 3 --whale K --whale-rps 6 --whale-n 200000

Prints one line per 5 s: per-tenant client-side p95 and status counts. Kept modest on purpose.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tenantguard_common.config import load_tenants  # noqa: E402


def p95(xs: List[float]) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.95 * len(xs)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--calm", type=float, default=20)
    ap.add_argument("--attack", type=float, default=40)
    ap.add_argument("--after", type=float, default=40)
    ap.add_argument("--rps", type=float, default=3.0, help="calm rps per tenant")
    ap.add_argument("--whale", default="K")
    ap.add_argument("--whale-rps", type=float, default=6.0)
    ap.add_argument("--whale-n", type=int, default=200000)
    ap.add_argument("--whale-conc", type=int, default=6)
    ap.add_argument("--projects", default="", help="only drive tenants of these projects (comma list)")
    args = ap.parse_args()

    tenants = load_tenants()
    if args.projects:
        tenants = [t for t in tenants if t.project in set(args.projects.split(","))]
    keys = {t.id: t.api_key for t in tenants}
    lat: Dict[str, List[float]] = defaultdict(list)
    codes: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    t0 = time.time()
    end = t0 + args.calm + args.attack + args.after
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)

    async with httpx.AsyncClient(base_url=args.gateway, timeout=10.0, limits=limits) as c:
        async def call(tid: str, path: str) -> None:
            s = time.perf_counter()
            try:
                r = await c.get(path, headers={"X-API-Key": keys[tid]})
                codes[tid][r.status_code] += 1
                if r.status_code == 200:
                    lat[tid].append((time.perf_counter() - s) * 1000)
            except Exception:
                codes[tid][599] += 1

        async def calm(tid: str) -> None:
            pending = set()
            while time.time() < end:
                path = "/api/light" if random.random() < 0.8 else f"/api/read?key=k{random.randint(1, 50):04d}"
                pending.add(asyncio.create_task(call(tid, path)))
                pending = {p for p in pending if not p.done()}
                await asyncio.sleep(random.expovariate(args.rps))

        async def whale() -> None:
            sem = asyncio.Semaphore(args.whale_conc)

            async def one():
                async with sem:
                    await call(args.whale, f"/api/search?n={args.whale_n}")
            start, stop = t0 + args.calm, t0 + args.calm + args.attack
            await asyncio.sleep(max(0, start - time.time()))
            print(f"+{time.time() - t0:5.1f}s whale {args.whale} starts", flush=True)
            while time.time() < stop:
                asyncio.create_task(one())
                await asyncio.sleep(1.0 / args.whale_rps)
            print(f"+{time.time() - t0:5.1f}s whale {args.whale} stops", flush=True)

        async def report() -> None:
            while time.time() < end:
                await asyncio.sleep(5)
                el = time.time() - t0
                parts = []
                for t in sorted(keys):
                    cs = dict(codes[t])
                    bad = sum(v for k, v in cs.items() if k != 200)
                    parts.append(f"{t}:{p95(lat[t]):.0f}" + (f"({bad}x)" if bad else ""))
                w = dict(codes[args.whale]) if args.attack else {}
                print(f"+{el:5.1f}s p95ms {' '.join(parts)} | {args.whale} codes {w}", flush=True)
                lat.clear()
                codes.clear()

        tasks = [asyncio.create_task(calm(t)) for t in keys] + [asyncio.create_task(report())]
        if args.attack > 0 and args.whale in keys:
            tasks.append(asyncio.create_task(whale()))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
