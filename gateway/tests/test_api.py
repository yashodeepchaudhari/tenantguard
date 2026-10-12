from __future__ import annotations

import asyncio
import json
import random
import time
from typing import List

import httpx
import pytest
from fastapi.testclient import TestClient
from tenantguard_common.contracts import (H_CLASS, H_CONTROL_TOKEN, H_CPU_MS, H_TENANT, OverridesView, Quota, TelemetryFrame,
                                          TenantAdminView, TenantConfig)

from tenantguard_gateway.app import create_app
from tenantguard_gateway.core import Gateway
from tenantguard_gateway.settings import GatewaySettings

TOKEN = {H_CONTROL_TOKEN: "dev"}
WORKERS = {"w0": "http://w0", "w1": "http://w1", "w2": "http://w2", "spare": "http://spare"}


def tenant(tid: str, servers=("w0", "w1", "w2"), weight=1.0, **quota) -> TenantConfig:
    q = dict(rate_rps=1000, burst=1000, cost_ms_per_s=100_000, concurrency=100)
    q.update(quota)
    return TenantConfig(id=tid, name=f"T{tid}", tier="pro", servers=list(servers), api_key=f"key-{tid}",
                        weight=weight, quota=Quota(**q))


class Upstream:
    """Fake worker fleet: records what it received; latency / cpu from the query string."""

    def __init__(self):
        self.seen: List[httpx.Request] = []

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        sleep_ms = float(req.url.params.get("sleep_ms", 0))
        if sleep_ms:
            await asyncio.sleep(sleep_ms / 1000)
        cpu = req.url.params.get("cpu_ms", "1.0")
        if "fail" in req.url.params:
            return httpx.Response(500, json={"error": "boom"})          # no CPU header at all
        return httpx.Response(200, headers={H_CPU_MS: cpu},
                              json={"server": req.url.host, "tenant": req.headers.get(H_TENANT)})


def make(tenants, mode="industry", window=8, max_wait_ms=1000.0, tick_ms=250, ticker=False, **kw):
    up = Upstream()
    s = GatewaySettings(default_mode=mode, initial_window=window, tick_ms=tick_ms,
                        max_wait_ms={"interactive": max_wait_ms, "batch": max_wait_ms * 5}, **kw)
    gw = Gateway(s, tenants, WORKERS, client=httpx.AsyncClient(transport=httpx.MockTransport(up)),
                 rng=random.Random(7))
    app = create_app(gw, run_ticker=ticker)
    return gw, app, up


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw", headers=TOKEN)


def run(coro):
    return asyncio.run(coro)


# ---------- identity ----------
def test_unknown_or_missing_key_is_401_json_and_never_forwarded():
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            r1 = await c.get("/api/light")
            r2 = await c.get("/api/light", headers={"X-API-Key": "nope"})
            return r1, r2
    r1, r2 = run(go())
    assert r1.status_code == 401 and r2.status_code == 401
    assert r1.json()["error"] == "unauthorized"
    assert up.seen == []


def test_forward_sets_trusted_headers_and_strips_spoofed_ones():
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            return await c.get("/api/search?n=5&q=x", headers={"X-API-Key": "key-A", H_TENANT: "Z"})
    r = run(go())
    assert r.status_code == 200
    req = up.seen[0]
    assert req.headers[H_TENANT] == "A"                    # spoofed tenant header replaced
    assert len(req.headers[H_CLASS]) == 8
    assert req.headers["x-request-id"] and "x-api-key" not in req.headers
    assert req.url.host in ("w0", "w1", "w2") and req.url.path == "/api/search"
    assert req.url.params["n"] == "5"
    assert r.headers["X-Request-Id"] == req.headers["x-request-id"]
    assert "RateLimit" in r.headers and "RateLimit-Policy" in r.headers


# ---------- admission ----------
def test_rate_limit_429_with_accurate_retry_after():
    gw, app, up = make([tenant("K", rate_rps=0.5, burst=2), tenant("A")])

    async def go():
        async with client(app) as c:
            ks = [await c.get("/api/light", headers={"X-API-Key": "key-K"}) for _ in range(4)]
            a = await c.get("/api/light", headers={"X-API-Key": "key-A"})
            return ks, a
    ks, a = run(go())
    assert [r.status_code for r in ks] == [200, 200, 429, 429]
    r = ks[2]
    assert r.headers["Retry-After"] == "2"                 # one token at 0.5 rps = 2 s
    assert r.json()["reasons"] == ["rate"] and 1900 <= r.json()["retry_after_ms"] <= 2000
    assert '"rate";r=0' in r.headers["RateLimit"]
    assert a.status_code == 200                            # neighbour unaffected
    assert len(up.seen) == 3


def test_cost_bucket_debited_after_response_then_refuses():
    # capacity = 100 ms/s * 2 s = 200 ms. One 500 ms request overdraws by 300 ms -> refused for 3 s.
    gw, app, up = make([tenant("K", cost_ms_per_s=100)])

    async def go():
        async with client(app) as c:
            r1 = await c.get("/api/search?cpu_ms=500", headers={"X-API-Key": "key-K"})
            r2 = await c.get("/api/light", headers={"X-API-Key": "key-K"})
            return r1, r2
    r1, r2 = run(go())
    assert r1.status_code == 200
    assert r2.status_code == 429 and r2.json()["reasons"] == ["cost"]
    assert r2.headers["Retry-After"] == "3"
    assert gw.registry.get("K").cost.level_ms == pytest.approx(-300, abs=5)


def test_concurrency_cap():
    gw, app, up = make([tenant("K", concurrency=2)])

    async def go():
        async with client(app) as c:
            return await asyncio.gather(*[c.get("/api/x?sleep_ms=100", headers={"X-API-Key": "key-K"})
                                          for _ in range(4)])
    rs = run(go())
    codes = sorted(r.status_code for r in rs)
    assert codes == [200, 200, 429, 429]
    assert all(r.json()["reasons"] == ["concurrency"] for r in rs if r.status_code == 429)


def test_off_mode_has_no_limits():
    gw, app, up = make([tenant("K", rate_rps=0.5, burst=1)], mode="off")

    async def go():
        async with client(app) as c:
            return [await c.get("/api/light", headers={"X-API-Key": "key-K"}) for _ in range(5)]
    assert [r.status_code for r in run(go())] == [200] * 5


def test_quota_patch_changes_behaviour():
    gw, app, up = make([tenant("K", rate_rps=0.1, burst=1)])

    async def go():
        async with client(app) as c:
            h = {"X-API-Key": "key-K"}
            before = [(await c.get("/api/light", headers=h)).status_code for _ in range(3)]
            p = await c.patch("/admin/tenants/K/quota", json={"rate_rps": 100, "burst": 50})
            bad = await c.patch("/admin/tenants/K/quota", json={"burst": 0})
            missing = await c.patch("/admin/tenants/ZZ/quota", json={"burst": 3})
            await asyncio.sleep(0.05)      # raising burst does not mint tokens; 100 rps refills 5 in 50 ms
            after = [(await c.get("/api/light", headers=h)).status_code for _ in range(3)]
            return before, p, bad, missing, after
    before, p, bad, missing, after = run(go())
    assert before == [200, 429, 429]
    assert p.status_code == 200
    v = TenantAdminView(**p.json())
    assert v.quota.burst == 50 and v.effective_quota.rate_rps == 100
    assert bad.status_code == 400 and missing.status_code == 404
    assert after == [200, 200, 200]


# ---------- scheduler: bounded wait ----------
def test_bounded_wait_expires_with_503_and_is_never_forwarded():
    # one tenant, every worker window = 1, each request takes 300 ms, max wait 100 ms.
    gw, app, up = make([tenant("A", servers=("w0",))], window=1, max_wait_ms=100)

    async def go():
        async with client(app) as c:
            return await asyncio.gather(*[c.get(f"/api/x?sleep_ms=300&i={i}", headers={"X-API-Key": "key-A"})
                                          for i in range(4)])
    rs = run(go())
    codes = sorted(r.status_code for r in rs)
    assert codes == [200, 503, 503, 503]
    assert len(up.seen) == 1                                 # expired ones never reached a worker
    r503 = next(r for r in rs if r.status_code == 503)
    assert r503.json()["error"] == "queue_timeout" and r503.headers["Retry-After"] == "1"
    assert gw.queued() == 0 and gw.slots["w0"] == 0 and gw.registry.get("A").pending == 0
    f = gw.tick()
    q = next(q for q in f.queues if q.tenant == "A")
    assert q.expired_503 == 3 and q.admitted == 4 and q.completed == 1


def test_fair_dispatch_under_contention_victim_not_starved():
    # whale K queues 40 slow requests first; A's 5 requests still get through within the wait bound.
    gw, app, up = make([tenant("K", servers=("w0",)), tenant("A", servers=("w0",))], window=2, max_wait_ms=2000)

    async def go():
        async with client(app) as c:
            ks = [asyncio.create_task(c.get("/api/x?sleep_ms=50", headers={"X-API-Key": "key-K"}))
                  for _ in range(40)]
            await asyncio.sleep(0.02)
            t0 = time.perf_counter()
            a = await asyncio.gather(*[c.get("/api/light", headers={"X-API-Key": "key-A"}) for _ in range(5)])
            a_s = time.perf_counter() - t0
            await asyncio.gather(*ks)
            return a, a_s
    a, a_s = run(go())
    assert all(r.status_code == 200 for r in a)
    assert a_s < 0.5        # FIFO would need 40 * 50 ms / 2 = 1 s before A is served


# ---------- control ----------
def test_control_validation_400_and_limits_apply_then_ttl_revert():
    gw, app, up = make([tenant("K", rate_rps=100, burst=10, concurrency=10)], mode="tenantguard")

    async def go():
        async with client(app) as c:
            out = {}
            out["bad_factor"] = await c.post("/control/limits", json={"tenant": "K", "factor": 0, "weight_factor": 1})
            out["bad_wf"] = await c.post("/control/limits", json={"tenant": "K", "factor": 0.5, "weight_factor": 1.5})
            out["bad_tenant"] = await c.post("/control/limits", json={"tenant": "ZZ", "factor": 0.5})
            out["bad_body"] = await c.post("/control/limits", json={"factor": "x"})
            out["bad_json"] = await c.post("/control/block", content=b"{not json",
                                           headers={"content-type": "application/json"})
            out["bad_target"] = await c.post("/control/relocate", json={"tenant": "K", "target": "w99"})
            out["bad_weights"] = await c.post("/control/route_weights", json={"weights": {"K": {"w0": 0}}})
            out["ok"] = await c.post("/control/limits", json={"tenant": "K", "factor": 0.1, "weight_factor": 0.5,
                                                               "ttl_s": 0.3})
            out["view"] = await c.get("/admin/tenants/K")
            out["ov"] = await c.get("/admin/overrides")
            await asyncio.sleep(0.35)
            out["view2"] = await c.get("/admin/tenants/K")
            out["ov2"] = await c.get("/admin/overrides")
            return out
    o = run(go())
    for k in ("bad_factor", "bad_wf", "bad_tenant", "bad_body", "bad_json", "bad_target", "bad_weights"):
        assert o[k].status_code == 400, k
        assert o[k].json()["ok"] is False
    assert o["ok"].status_code == 200 and o["ok"].json()["ok"]
    v = TenantAdminView(**o["view"].json())
    assert v.factor == 0.1 and v.weight_factor == 0.5
    assert (v.effective_quota.burst, v.effective_quota.concurrency) == (1, 1)
    assert v.effective_quota.rate_rps == pytest.approx(10) and v.quota.rate_rps == 100
    ov = OverridesView(**o["ov"].json())
    assert "limits:K" in ov.expires_at and ov.limits["K"].factor == 0.1 and ov.fail_static
    v2 = TenantAdminView(**o["view2"].json())
    assert v2.factor == 1.0 and v2.effective_quota == v2.quota      # reverted by TTL
    assert OverridesView(**o["ov2"].json()).limits == {}


def test_block_relocate_drain_heartbeat():
    gw, app, up = make([tenant("K"), tenant("A")], mode="tenantguard")

    async def go():
        async with client(app) as c:
            h = {"X-API-Key": "key-K"}
            out = {}
            await c.post("/control/block", json={"tenant": "K", "ttl_s": 30})
            out["blocked"] = await c.get("/api/light", headers=h)
            await c.post("/control/block", json={"tenant": "K", "blocked": False})
            await c.post("/control/relocate", json={"tenant": "K", "target": "spare", "ttl_s": 30})
            out["reloc"] = [await c.get("/api/light", headers=h) for _ in range(5)]
            await c.post("/control/drain", json={"server": "w0", "ttl_s": 30})
            await c.post("/control/drain", json={"server": "w1", "ttl_s": 30})
            out["drain"] = [await c.get("/api/light", headers={"X-API-Key": "key-A"}) for _ in range(10)]
            out["hb"] = await c.post("/control/heartbeat", json={"ts": 1.0})
            out["ov"] = OverridesView(**(await c.get("/admin/overrides")).json())
            out["tenants"] = (await c.get("/admin/tenants")).json()
            return out
    o = run(go())
    assert o["blocked"].status_code == 429 and o["blocked"].json()["reasons"] == ["blocked"]
    assert int(o["blocked"].headers["Retry-After"]) == 30
    assert {r.json()["server"] for r in o["reloc"]} == {"spare"}
    assert {r.json()["server"] for r in o["drain"]} == {"w2"}
    assert o["hb"].json()["ok"]
    ov = o["ov"]
    assert not ov.fail_static and ov.last_heartbeat_age_s < 1
    assert ov.relocations[0].target == "spare" and set(ov.drains) == {"w0", "w1"}
    k = next(t for t in o["tenants"] if t["id"] == "K")
    assert k["placement"] == "spare" and "api_key" not in k


def test_overrides_ignored_outside_tenantguard_mode():
    gw, app, up = make([tenant("K")], mode="industry")

    async def go():
        async with client(app) as c:
            await c.post("/control/block", json={"tenant": "K", "ttl_s": 30})
            r1 = await c.get("/api/light", headers={"X-API-Key": "key-K"})
            await c.post("/admin/mode", json={"mode": "tenantguard"})
            r2 = await c.get("/api/light", headers={"X-API-Key": "key-K"})
            bad = await c.post("/admin/mode", json={"mode": "chaos"})
            return r1, r2, bad
    r1, r2, bad = run(go())
    assert r1.status_code == 200 and r2.status_code == 429 and bad.status_code == 422


# ---------- admin ----------
def test_admin_add_tenant_and_conflicts():
    gw, app, up = make([tenant("A")])
    new = tenant("M", servers=("w1", "w2", "spare")).model_dump()

    async def go():
        async with client(app) as c:
            r = await c.post("/admin/tenants", json=new)
            dup = await c.post("/admin/tenants", json=new)
            badsrv = await c.post("/admin/tenants", json={**new, "id": "N", "api_key": "k2", "servers": ["w42"]})
            use = await c.get("/api/light", headers={"X-API-Key": "key-M"})
            lst = await c.get("/admin/tenants")
            mode = await c.get("/admin/mode")
            return r, dup, badsrv, use, lst, mode
    r, dup, badsrv, use, lst, mode = run(go())
    assert r.status_code == 201 and "api_key" not in r.json()
    assert dup.status_code == 409 and badsrv.status_code == 400
    assert use.status_code == 200
    assert [t["id"] for t in lst.json()] == ["A", "M"]
    assert mode.json() == {"mode": "industry"}
    assert all(st.cfg.api_key == "" for st in gw.registry.by_id.values())     # only the hash is kept


# ---------- telemetry ----------
def test_telemetry_ws_frames_latest_and_metrics():
    gw, app, up = make([tenant("A"), tenant("K", rate_rps=0.1, burst=1)], tick_ms=100, ticker=True)
    with TestClient(app, headers=TOKEN) as tc:
        frames = []
        with tc.websocket_connect("/internal/telemetry") as ws:
            h = {"X-API-Key": "key-A"}
            for _ in range(3):
                assert tc.get("/api/light", headers=h).status_code == 200
            tc.get("/api/light", headers={"X-API-Key": "key-K"})
            tc.get("/api/light", headers={"X-API-Key": "key-K"})
            for _ in range(6):     # the queue keeps 8 frames (800 ms), far more than the requests took
                frames.append(TelemetryFrame(**json.loads(ws.receive_text())))
        latest = tc.get("/internal/telemetry/latest")
        metrics = tc.get("/metrics").text
    assert latest.status_code == 200 and TelemetryFrame(**latest.json()).mode == "industry"
    assert frames[1].ts > frames[0].ts
    cells = [c for f in frames for c in f.cells if c.tenant == "A"]
    assert sum(c.count for c in cells) == 3 and all(c.p95_ms >= c.p50_ms for c in cells)
    qa = [q for f in frames for q in f.queues if q.tenant == "A"]
    assert sum(q.admitted for q in qa) == 3 and sum(q.completed for q in qa) == 3
    qk = [q for f in frames for q in f.queues if q.tenant == "K"]
    assert sum(q.rejected_429 for q in qk) == 1
    assert {ls.server for ls in frames[0].limiter} == set(WORKERS)
    assert 'tenantguard_rejected_total{tenant="K",reason="rate"} 1' in metrics
    assert 'tenantguard_request_duration_ms_count{tenant="A"} 3' in metrics


def test_missing_or_garbage_cpu_header_debits_zero_and_is_counted():
    gw, app, up = make([tenant("K", cost_ms_per_s=100)])

    async def go():
        async with client(app) as c:
            h = {"X-API-Key": "key-K"}
            r1 = await c.get("/api/x?fail=1", headers=h)
            r2 = await c.get("/api/x?cpu_ms=abc", headers=h)
            r3 = await c.get("/api/x?cpu_ms=-5", headers=h)
            m = (await c.get("/metrics")).text
            return r1, r2, r3, m
    r1, r2, r3, m = run(go())
    assert (r1.status_code, r2.status_code, r3.status_code) == (500, 200, 200)
    assert gw.registry.get("K").cost.level_ms == pytest.approx(200, abs=1)       # nothing debited
    assert sum(n for (t, _), n in gw.telemetry.c_cpu_missing.items() if t == "K") == 3
    assert "tenantguard_cpu_header_missing_total{" in m
    assert 'tenantguard_requests_total{tenant="K",server=' in m and 'code="5xx"} 1' in m
