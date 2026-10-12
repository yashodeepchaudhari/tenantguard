import asyncio
import random
import statistics

import httpx
import pytest

from tenantguard_lab.engine import Engine
from tenantguard_lab.poisson import arrival_times, gaps, run_poisson
from tenantguard_lab.settings import load_config


@pytest.mark.parametrize("rate", [2.0, 14.0, 300.0])
def test_arrival_count_within_10pct(rate):
    rng = random.Random(42)
    duration = 2000 / rate                       # ~2000 expected arrivals
    n = len(arrival_times(rate, duration, rng))
    assert abs(n - 2000) <= 200


def test_gaps_are_exponential():
    g = gaps(10.0, random.Random(1))
    xs = [next(g) for _ in range(20000)]
    assert statistics.mean(xs) == pytest.approx(0.1, rel=0.05)
    assert statistics.pstdev(xs) == pytest.approx(0.1, rel=0.05)   # exponential: sd == mean
    with pytest.raises(ValueError):
        next(gaps(0, random.Random()))


async def test_run_poisson_real_time_rate_within_10pct():
    stop = asyncio.Event()
    fired = []
    task = asyncio.ensure_future(run_poisson(lambda: 200.0, lambda: fired.append(1), stop, rng=random.Random(7)))
    await asyncio.sleep(3.0)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert abs(len(fired) - 600) <= 60


async def test_calm_engine_rate_within_10pct():
    """All 12 personas against a mock gateway: total sent rate matches sum(rps) * rate_scale."""
    cfg = load_config()
    cfg.rate_scale = 1.0
    seen_keys = set()

    def handler(req: httpx.Request) -> httpx.Response:
        seen_keys.add(req.headers["X-API-Key"])
        return httpx.Response(200, json={})

    eng = Engine(cfg, seed=3, transport=httpx.MockTransport(handler))
    eng.start_calm()
    await asyncio.sleep(4.0)
    await eng.stop_calm()
    await eng.aclose()
    expected = sum(p.rps for p in cfg.personas.values()) * 4.0
    sent = sum(t["count"] for t in eng.recorder.totals.values())
    assert abs(sent - expected) <= 0.1 * expected, (sent, expected)
    assert seen_keys == {p.tenant.api_key for p in cfg.personas.values()}   # every tenant uses its own key
