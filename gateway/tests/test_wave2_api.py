"""Wave 2 behaviour through the HTTP API: express lane, batch lane + shedding, retry budget, adaptive window
wiring (per replica)."""
from __future__ import annotations

import asyncio
import time

from tenantguard_common.contracts import H_CLASS

from test_api import client, make, run, tenant

LIGHT = "/api/light?cpu_ms=1"
HEAVY = "/api/search?cpu_ms=200&sleep_ms=200"


async def _learn(c, key: str, path: str, n: int = 1):
    for _ in range(n):
        assert (await c.get(path, headers={"X-API-Key": key})).status_code == 200


def _whale_vs_light(express_cost_ms: float):
    """W keeps a single replica's window full of 200 ms requests; A (same project) sends 5 light requests."""
    gw, app, up = make([tenant("W"), tenant("A")], replicas={"p1": ["w0"]}, window=4,
                       max_wait_ms=3000, express_cost_ms=express_cost_ms)

    async def go():
        async with client(app) as c:
            await _learn(c, "key-A", LIGHT)                    # A's light class: measured cost 1 ms
            await _learn(c, "key-W", HEAVY)                    # W's heavy class: measured cost 200 ms
            whale = [asyncio.create_task(c.get(HEAVY, headers={"X-API-Key": "key-W"})) for _ in range(30)]
            await asyncio.sleep(0.05)
            lat, lanes = [], []
            for _ in range(5):
                t0 = time.perf_counter()
                r = await c.get(LIGHT, headers={"X-API-Key": "key-A"})
                lat.append((time.perf_counter() - t0) * 1000)
                lanes.append(r.headers["X-TenantGuard-Lane"])
            await asyncio.gather(*whale)
            return lat, lanes
    return run(go())


def test_express_lane_keeps_light_requests_fast_behind_a_whale():
    lat, lanes = _whale_vs_light(express_cost_ms=5)
    assert set(lanes) == {"express"}
    assert max(lat) < 150, lat                 # never waits for a 200 ms whale slot


def test_without_express_lane_light_requests_wait_behind_the_whale():
    lat, lanes = _whale_vs_light(express_cost_ms=0)
    assert set(lanes) == {"interactive"}
    assert sorted(lat)[2] > 150, lat           # median light request waits for a whale slot


def test_batch_lane_by_header_or_route_with_longer_wait():
    gw, app, up = make([tenant("A")], batch_routes=["/api/export"])

    async def go():
        async with client(app) as c:
            h = {"X-API-Key": "key-A"}
            r1 = await c.get("/api/export", headers=h)
            r2 = await c.get("/api/x", headers={**h, H_CLASS: "batch"})
            r3 = await c.get("/api/y", headers=h)          # unmeasured class: default 5 ms, not express
            return r1, r2, r3
    r1, r2, r3 = run(go())
    assert [r.headers["X-TenantGuard-Lane"] for r in (r1, r2, r3)] == ["batch", "batch", "interactive"]
    assert len(up.seen[1].headers[H_CLASS]) == 8          # worker still gets the class hash, not "batch"


def test_batch_is_shed_first_when_worker_saturates():
    gw, app, up = make([tenant("B"), tenant("C")], replicas={"p1": ["w0"]}, window=2, max_wait_ms=2000)

    async def go():
        async with client(app) as c:
            hb = {"X-API-Key": "key-B", H_CLASS: "batch"}
            batch = [asyncio.create_task(c.get(f"/api/x?sleep_ms=300&b={i}", headers=hb)) for i in range(3)]
            await asyncio.sleep(0.05)                     # 1 batch running (batch cap = 1), 2 queued
            inter = [asyncio.create_task(c.get(f"/api/x?sleep_ms=300&c={i}", headers={"X-API-Key": "key-C"}))
                     for i in range(2)]                   # no interactive headroom on w0 -> queued batch is shed
            return await asyncio.gather(*batch), await asyncio.gather(*inter)
    batch, inter = run(go())
    assert sorted(r.status_code for r in batch) == [200, 503, 503]
    assert {r.json()["error"] for r in batch if r.status_code == 503} == {"shed"}
    assert [r.status_code for r in inter] == [200, 200]
    assert gw.queued() == 0 and gw.slots["w0"] == 0


def test_retry_storm_is_capped_by_budget_and_counted():
    gw, app, up = make([tenant("K", rate_rps=1, burst=1), tenant("A")])

    async def go():
        async with client(app) as c:
            h = {"X-API-Key": "key-K"}
            storm = [await c.get("/api/light", headers=h) for _ in range(30)]   # ignores Retry-After
            calm = [await c.get("/api/light", headers={"X-API-Key": "key-A"}) for _ in range(20)]
            m = (await c.get("/metrics")).text
            return storm, calm, m
    storm, calm, m = run(go())
    assert storm[0].status_code == 200
    reasons = [r.json()["reasons"] for r in storm[1:]]
    assert reasons[0] == ["rate"]                                    # first failure: a plain rate refusal
    assert reasons[1:4] == [["rate"]] * 3                            # 3 retries allowed (min_allowed)
    assert all(x == ["retry_budget"] for x in reasons[4:])           # the rest of the storm is capped
    assert all(r.status_code == 200 for r in calm)                   # repeats that succeed are not retries
    assert 'tenantguard_retries_total{tenant="K"} 28' in m
    assert 'tenantguard_retries_total{tenant="A"}' not in m
    assert 'tenantguard_rejected_total{tenant="K",reason="retry_budget"} 25' in m
    q = {q.tenant: q for q in gw.tick().queues}
    assert q["K"].retries == 28 and q["A"].retries == 0


def test_idempotency_key_repeat_counts_as_retry():
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            h = {"X-API-Key": "key-A", "Idempotency-Key": "op-1"}
            return [await c.post("/api/write", headers=h, content=b"v") for _ in range(6)]
    rs = run(go())
    assert [r.status_code for r in rs[:4]] == [200] * 4
    assert rs[4].status_code == 429 and rs[4].json()["reasons"] == ["retry_budget"]


def test_off_mode_has_no_retry_budget_and_fixed_window():
    gw, app, up = make([tenant("K", rate_rps=1, burst=1)], replicas={"p1": ["w0"]}, mode="off", window=8)

    async def go():
        async with client(app) as c:
            return [await c.get("/api/light", headers={"X-API-Key": "key-K", "Idempotency-Key": "x"})
                    for _ in range(10)]
    assert all(r.status_code == 200 for r in run(go()))
    gw.limiters["w0"].window_f = 2
    assert gw.window("w0") == 8


def test_adaptive_window_moves_with_worker_side_latency():
    gw, app, up = make([tenant("A"), tenant("B", project="p2")], replicas={"p1": ["w0"], "p2": ["idle"]}, window=16)

    async def go():
        async with client(app) as c:
            h = {"X-API-Key": "key-A"}
            await asyncio.gather(*[c.get(f"/api/x?i={i}", headers=h) for i in range(16)])
            gw.tick()
            before = {ls.server: ls.window for ls in gw.tick().limiter}
            for _ in range(6):
                await asyncio.gather(*[c.get(f"/api/x?sleep_ms=60&i={i}", headers=h) for i in range(16)])
                frame = gw.tick()
            return before, {ls.server: ls.window for ls in frame.limiter}
    before, after = run(go())
    assert after["w0"] < before["w0"], (before, after)
    assert after["idle"] == 16                                     # idle replica untouched
