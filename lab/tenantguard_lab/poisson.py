"""Poisson arrival process (exponential inter-arrival gaps) on an absolute schedule."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, Iterator, List, Optional


def gaps(rate: float, rng: random.Random) -> Iterator[float]:
    """Endless exponential inter-arrival gaps (seconds) for `rate` arrivals per second."""
    if rate <= 0:
        raise ValueError("rate must be > 0")
    while True:
        yield rng.expovariate(rate)


def arrival_times(rate: float, duration_s: float, rng: random.Random) -> List[float]:
    """Arrival offsets in [0, duration_s) for a Poisson process (used by tests and planning)."""
    out: List[float] = []
    t = 0.0
    for g in gaps(rate, rng):
        t += g
        if t >= duration_s:
            return out
        out.append(t)
    return out  # pragma: no cover


async def run_poisson(rate_fn: Callable[[], float], fire: Callable[[], None], stop: asyncio.Event,
                      rng: Optional[random.Random] = None, clock: Callable[[], float] = time.monotonic,
                      max_gap_s: float = 1.0) -> None:
    """Call `fire()` at Poisson arrival times until `stop` is set.

    The schedule is absolute (next = previous + gap), so event-loop jitter does not lower the rate:
    late arrivals fire immediately and catch up. `rate_fn` is re-read every arrival so the rate can
    change live. A rate <= 0 idles (re-checked every max_gap_s)."""
    rng = rng or random.Random()
    nxt = clock()
    while not stop.is_set():
        rate = rate_fn()
        if rate <= 0:
            nxt = clock() + max_gap_s
        else:
            nxt += rng.expovariate(rate)
        delay = nxt - clock()
        if delay > 0:
            await asyncio.sleep(delay)  # stop is re-checked below; tasks are also cancelled on stop
        if rate > 0 and not stop.is_set():
            fire()
        # Don't let a long stall (e.g. a paused laptop) cause an unbounded burst of catch-up arrivals.
        if clock() - nxt > 5.0:
            nxt = clock()
