"""Attack traffic profiles. Each runs until `stop` is set; the manager in core.py handles duration.

Intensity (AttackReq.intensity, 0.1..5) multiplies the profile's rate (flood, whale, memory,
retry_storm) or concurrency (hog). Profiles are in lab/personas.yaml `attacks`."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict

from tenantguard_common.contracts import AttackReq

from .engine import Engine, InflightGate
from .poisson import run_poisson
from .settings import Endpoint

WAVE2 = {"background_bomb", "blackbox_bomb", "cache_flood", "faulty_server", "honest_spike"}


@dataclass
class AttackCtx:
    attack_id: str
    engine: Engine
    req: AttackReq
    stop: asyncio.Event
    counts: Dict[str, int] = field(default_factory=lambda: {"sent": 0, "ok": 0, "r429": 0, "r503": 0,
                                                            "other_err": 0, "retries": 0, "dropped": 0})

    @property
    def tenant(self) -> str:
        assert self.req.tenant is not None
        return self.req.tenant

    @property
    def profile(self) -> dict:
        return self.engine.cfg.attacks[self.req.attack]

    def tally(self, status: int) -> None:
        self.counts["sent"] += 1
        if 200 <= status < 400:
            self.counts["ok"] += 1
        elif status == 429:
            self.counts["r429"] += 1
        elif status == 503:
            self.counts["r503"] += 1
        else:
            self.counts["other_err"] += 1

    def persona_rate(self) -> float:
        return self.engine.cfg.persona(self.tenant).rps * self.engine.cfg.rate_scale


async def _open_loop(ctx: AttackCtx, rate: float, make_req: Callable[[], dict],
                     on_fire: Callable[[object], Awaitable[None]] = None) -> None:
    """Poisson arrivals at `rate`; each arrival sends one request (or runs on_fire) on the attack pool."""
    cap = int(ctx.profile.get("max_inflight", ctx.engine.cfg.client["max_inflight_per_attack"]))
    client = ctx.engine.client(ctx.tenant, ctx.attack_id, max_conns=cap)
    gate = InflightGate(cap)

    async def one() -> None:
        if on_fire is not None:
            await on_fire(client)
            return
        status, _ = await ctx.engine.send(client, ctx.tenant, make_req(), "attack", ctx.req.attack)
        ctx.tally(status)

    def fire() -> None:
        if gate.full():
            ctx.counts["dropped"] += 1
            return
        gate.spawn(one())

    try:
        await run_poisson(lambda: rate, fire, ctx.stop, rng=random.Random())
    finally:
        await gate.cancel_all()


def _endpoint(ctx: AttackCtx, kind: str) -> Endpoint:
    return ctx.engine.cfg.endpoints[kind]


async def flood(ctx: AttackCtx) -> None:
    ep = _endpoint(ctx, ctx.profile.get("endpoint", "light"))
    rate = float(ctx.profile["multiplier"]) * ctx.persona_rate() * ctx.req.intensity
    await _open_loop(ctx, rate, lambda: ctx.engine.build(ep))


async def whale(ctx: AttackCtx) -> None:
    ep = _endpoint(ctx, "search")
    params = dict(ctx.profile.get("params", {}))
    rate = float(ctx.profile["rps"]) * ctx.req.intensity
    await _open_loop(ctx, rate, lambda: ctx.engine.build(ep, params))


async def memory(ctx: AttackCtx) -> None:
    ep = Endpoint(method="GET", path=ctx.profile.get("path", "/api/memory"))
    params = dict(ctx.profile.get("params", {}))
    rate = float(ctx.profile["rps"]) * ctx.req.intensity
    await _open_loop(ctx, rate, lambda: ctx.engine.build(ep, params))


async def hog(ctx: AttackCtx) -> None:
    """Closed loop: N workers each keep one slow /api/export in flight at all times."""
    n = max(1, round(int(ctx.profile["concurrency"]) * ctx.req.intensity))
    ep = Endpoint(method="GET", path=ctx.profile.get("path", "/api/export"),
                  params=dict(ctx.profile.get("params", {})))
    client = ctx.engine.client(ctx.tenant, ctx.attack_id, max_conns=n)

    async def worker() -> None:
        while not ctx.stop.is_set():
            status, _ = await ctx.engine.send(client, ctx.tenant, ctx.engine.build(ep), "attack", "hog")
            ctx.tally(status)
            if status < 0:          # gateway unreachable: don't spin
                await asyncio.sleep(0.2)
            else:
                await asyncio.sleep(0)  # an instant answer must not starve the event loop

    tasks = [asyncio.ensure_future(worker()) for _ in range(n)]
    try:
        await ctx.stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def retry_storm(ctx: AttackCtx) -> None:
    """Open loop like a normal client, but every 429/503 is retried immediately (Retry-After ignored)."""
    ep = _endpoint(ctx, ctx.profile.get("endpoint", "read"))
    max_retries = int(ctx.profile.get("max_retries", 50))
    rate = float(ctx.profile["multiplier"]) * ctx.persona_rate() * ctx.req.intensity

    async def fire(client) -> None:
        for attempt in range(max_retries + 1):
            status, _ = await ctx.engine.send(client, ctx.tenant, ctx.engine.build(ep), "attack", "retry_storm")
            ctx.tally(status)
            if status not in (429, 503) or ctx.stop.is_set():
                return
            ctx.counts["retries"] += 1
            await asyncio.sleep(0)  # yield only; no backoff on purpose

    await _open_loop(ctx, rate, lambda: {}, on_fire=fire)


RUNNERS: Dict[str, Callable[[AttackCtx], Awaitable[None]]] = {
    "flood": flood,
    "whale": whale,
    "hog": hog,
    "memory": memory,
    "retry_storm": retry_storm,
}
