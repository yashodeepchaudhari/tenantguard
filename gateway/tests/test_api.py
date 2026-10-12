from __future__ import annotations

import asyncio
import json
import time
from typing import Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient
from tenantguard_common.contracts import (H_CLASS, H_CONTROL_TOKEN, H_CPU_MS, H_REPLICA, H_TENANT, LimitsCmd,
                                          OverridesView, Quota, ReplicaView, Snapshot, SnapTenant, TelemetryFrame,
                                          TenantAdminView)

from tenantguard_gateway.app import create_app
from tenantguard_gateway.core import Gateway
from tenantguard_gateway.settings import GatewaySettings
from tenantguard_gateway.tenants import hash_key

TOKEN = {H_CONTROL_TOKEN: "dev"}


def tenant(tid: str, project: str = "p1", **quota) -> SnapTenant:
    q = dict(rate_rps=1000, burst=1000, concurrency=100)
    q.update(quota)
    return SnapTenant(id=tid, name=f"T{tid}", project=project, tier="pro", key_sha256=hash_key(f"key-{tid}"),
                      quota=Quota(**q))


def replica(rid: str, project: str = "p1", state: str = "ready") -> ReplicaView:
    return ReplicaView(id=rid, project=project, url=f"http://{rid}", state=state)


def snapshot(tenants: List[SnapTenant], replicas: Dict[str, List[str]] = None, version: int = 1,
             limits=(), pins=None, states: Dict[str, str] = None) -> Snapshot:
    replicas = {"p1": ["r1", "r2", "r3"]} if replicas is None else replicas
    states = states or {}
    return Snapshot(version=version, issued_at=time.time(), tenants=tenants,
                    replicas={p: [replica(r, p, states.get(r, "ready")) for r in rs] for p, rs in replicas.items()},
                    limits=list(limits), pins=pins or {})


class Upstream:
    """Fake replicas: records what they received; latency / cpu from the query string."""

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


def make(tenants, replicas=None, mode="industry", window=8, max_wait_ms=1000.0, tick_ms=250, ticker=False,
         waker=None, **kw):
    up = Upstream()
    s = GatewaySettings(default_mode=mode, initial_window=window, tick_ms=tick_ms,
                        max_wait_ms={"interactive": max_wait_ms, "batch": max_wait_ms * 5}, **kw)
    gw = Gateway(s, client=httpx.AsyncClient(transport=httpx.MockTransport(up)), waker=waker)
    gw.on_snapshot(snapshot(tenants, replicas))
    app = create_app(gw, run_loops=ticker)
    return gw, app, up


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw", headers=TOKEN)


def run(coro):
    return asyncio.run(coro)


def key(t: str) -> dict:
    return {"X-API-Key": f"key-{t}"}


# ---------- identity ----------
def test_unknown_or_missing_key_is_401_json_and_never_forwarded():
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            return await c.get("/api/light"), await c.get("/api/light", headers={"X-API-Key": "nope"})
    r1, r2 = run(go())
    assert r1.status_code == 401 and r2.status_code == 401 and r1.json()["error"] == "unauthorized"
    assert up.seen == []


def test_forward_sets_trusted_headers_and_strips_spoofed_ones():
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            return await c.get("/api/search?n=5&q=x", headers={**key("A"), H_TENANT: "Z", H_REPLICA: "evil"})
    r = run(go())
    assert r.status_code == 200
    req = up.seen[0]
    assert req.headers[H_TENANT] == "A" and len(req.headers[H_CLASS]) == 8
    assert req.headers[H_REPLICA] == req.url.host and req.url.host in ("r1", "r2", "r3")
    assert req.headers["x-request-id"] and "x-api-key" not in req.headers
    assert req.url.path == "/api/search" and req.url.params["n"] == "5"
    assert r.headers["X-Request-Id"] == req.headers["x-request-id"]
    assert "RateLimit" in r.headers and "RateLimit-Policy" in r.headers


def test_tenant_removed_by_snapshot_is_401():
    gw, app, up = make([tenant("A"), tenant("B")])
    gw.on_snapshot(snapshot([tenant("B")], version=2))

    async def go():
        async with client(app) as c:
            return await c.get("/api/light", headers=key("A")), await c.get("/api/light", headers=key("B"))
    a, b = run(go())
    assert a.status_code == 401 and b.status_code == 200


# ---------- routing ----------
def test_rendezvous_is_sticky_and_only_uses_own_project():
    gw, app, up = make([tenant("A"), tenant("B", project="p2")], replicas={"p1": ["r1", "r2", "r3"], "p2": ["q1"]})

    async def go():
        async with client(app) as c:
            a = [await c.get("/api/light", headers=key("A")) for _ in range(6)]
            b = [await c.get("/api/light", headers=key("B")) for _ in range(3)]
            return a, b
    a, b = run(go())
    assert len({r.json()["server"] for r in a}) == 1 and a[0].json()["server"] in ("r1", "r2", "r3")
    assert {r.json()["server"] for r in b} == {"q1"}                    # I6: never another project's replica


def test_pin_routes_to_the_pinned_replica_in_tenantguard_mode():
    gw, app, up = make([tenant("A")], mode="tenantguard")
    sticky = gw.admin_view(gw.registry.get("A")).replicas[0]
    other = next(r for r in ("r1", "r2", "r3") if r != sticky)
    gw.on_snapshot(snapshot([tenant("A")], version=2, pins={"A": other}))

    async def go():
        async with client(app) as c:
            return [await c.get("/api/light", headers=key("A")) for _ in range(3)]
    assert {r.json()["server"] for r in run(go())} == {other}
    assert gw.admin_view(gw.registry.get("A")).pinned_to == other


def test_starting_and_draining_replicas_get_no_traffic():
    gw, app, up = make([tenant("A")])
    gw.on_snapshot(snapshot([tenant("A")], version=2, states={"r1": "starting", "r2": "draining"}))

    async def go():
        async with client(app) as c:
            return [await c.get("/api/light", headers=key("A")) for _ in range(4)]
    assert {r.json()["server"] for r in run(go())} == {"r3"}


# ---------- admission ----------
def test_rate_limit_429_with_accurate_retry_after():
    gw, app, up = make([tenant("K", rate_rps=0.5, burst=2), tenant("A")])

    async def go():
        async with client(app) as c:
            ks = [await c.get("/api/light", headers=key("K")) for _ in range(4)]
            return ks, await c.get("/api/light", headers=key("A"))
    ks, a = run(go())
    assert [r.status_code for r in ks] == [200, 200, 429, 429]
    r = ks[2]
    assert r.headers["Retry-After"] == "2"                 # one token at 0.5 rps = 2 s
    assert r.json()["reasons"] == ["rate"] and 1900 <= r.json()["retry_after_ms"] <= 2000
    assert '"rate";r=0' in r.headers["RateLimit"]
    assert a.status_code == 200 and len(up.seen) == 3


def test_concurrency_cap():
    gw, app, up = make([tenant("K", concurrency=2)])

    async def go():
        async with client(app) as c:
            return await asyncio.gather(*[c.get("/api/x?sleep_ms=100", headers=key("K")) for _ in range(4)])
    rs = run(go())
    assert sorted(r.status_code for r in rs) == [200, 200, 429, 429]
    assert all(r.json()["reasons"] == ["concurrency"] for r in rs if r.status_code == 429)


def test_off_mode_has_no_limits():
    gw, app, up = make([tenant("K", rate_rps=0.5, burst=1)], mode="off")

    async def go():
        async with client(app) as c:
            return [await c.get("/api/light", headers=key("K")) for _ in range(5)]
    assert [r.status_code for r in run(go())] == [200] * 5


# ---------- scheduler ----------
def test_bounded_wait_expires_with_503_and_is_never_forwarded():
    gw, app, up = make([tenant("A")], replicas={"p1": ["r1"]}, window=1, max_wait_ms=100)

    async def go():
        async with client(app) as c:
            return await asyncio.gather(*[c.get(f"/api/x?sleep_ms=300&i={i}", headers=key("A")) for i in range(4)])
    rs = run(go())
    assert sorted(r.status_code for r in rs) == [200, 503, 503, 503]
    assert len(up.seen) == 1                                 # expired ones never reached a replica
    r503 = next(r for r in rs if r.status_code == 503)
    assert r503.json()["error"] == "queue_timeout" and r503.headers["Retry-After"] == "1"
    assert gw.queued() == 0 and gw.slots["r1"] == 0 and gw.registry.get("A").pending == 0
    q = next(q for q in gw.tick().queues if q.tenant == "A")
    assert q.expired_503 == 3 and q.admitted == 4 and q.completed == 1 and q.refused_cost_ms > 0


def test_fair_dispatch_inside_a_project_victim_not_starved():
    gw, app, up = make([tenant("K"), tenant("A")], replicas={"p1": ["r1"]}, window=2, max_wait_ms=2000)

    async def go():
        async with client(app) as c:
            ks = [asyncio.create_task(c.get("/api/x?sleep_ms=50", headers=key("K"))) for _ in range(40)]
            await asyncio.sleep(0.02)
            t0 = time.perf_counter()
            a = await asyncio.gather(*[c.get("/api/light", headers=key("A")) for _ in range(5)])
            a_s = time.perf_counter() - t0
            await asyncio.gather(*ks)
            return a, a_s
    a, a_s = run(go())
    assert all(r.status_code == 200 for r in a)
    assert a_s < 0.5        # FIFO would need 40 * 50 ms / 2 = 1 s before A is served


def test_flood_in_one_project_never_delays_another_project():
    gw, app, up = make([tenant("K"), tenant("A", project="p2")], replicas={"p1": ["r1"], "p2": ["q1"]},
                       window=1, max_wait_ms=3000)

    async def go():
        async with client(app) as c:
            ks = [asyncio.create_task(c.get("/api/x?sleep_ms=100", headers=key("K"))) for _ in range(10)]
            await asyncio.sleep(0.02)
            t0 = time.perf_counter()
            a = [await c.get("/api/light", headers=key("A")) for _ in range(3)]
            a_s = time.perf_counter() - t0
            await asyncio.gather(*ks)
            return a, a_s
    a, a_s = run(go())
    assert all(r.status_code == 200 for r in a) and a_s < 0.3     # K's 1 s backlog is K's alone


# ---------- wake ----------
def test_project_without_ready_replica_wakes_the_brain_and_waits():
    woken = []

    async def waker(project):
        woken.append(project)

    gw, app, up = make([tenant("A")], replicas={"p1": []}, waker=waker, cold_wait_ms=2000)

    async def go():
        async with client(app) as c:
            req = asyncio.create_task(c.get("/api/light", headers=key("A")))
            second = asyncio.create_task(c.get("/api/light", headers=key("A")))
            await asyncio.sleep(0.2)
            gw.on_snapshot(snapshot([tenant("A")], replicas={"p1": ["r9"]}, version=2))
            return await req, await second
    r, r2 = run(go())
    assert r.status_code == 200 and r.json()["server"] == "r9" and r2.status_code == 200
    assert woken == ["p1"]                                   # debounced: one wake for both requests


def test_cold_wait_expires_with_503():
    gw, app, up = make([tenant("A")], replicas={"p1": []}, cold_wait_ms=150, max_wait_ms=50)

    async def go():
        async with client(app) as c:
            return await c.get("/api/light", headers=key("A"))
    r = run(go())
    assert r.status_code == 503 and r.json()["max_wait_ms"] == 150


# ---------- control ----------
def test_snapshot_validation_400_and_limits_apply_then_ttl_revert():
    gw, app, up = make([tenant("K", rate_rps=100, burst=10, concurrency=10)], mode="tenantguard")
    base = snapshot([tenant("K", rate_rps=100, burst=10, concurrency=10)], version=2)

    def with_(**kw):
        return json.loads(base.model_copy(update=kw).model_dump_json())

    async def go():
        async with client(app) as c:
            post = c.post
            out = {"bad_factor": await post("/control/snapshot", json=with_(limits=[LimitsCmd(tenant="K", factor=0)])),
                   "bad_wf": await post("/control/snapshot", json=with_(limits=[LimitsCmd(tenant="K", weight_factor=1.5)])),
                   "bad_ttl": await post("/control/snapshot", json=with_(limits=[LimitsCmd(tenant="K", factor=.5, ttl_s=0)])),
                   "bad_tenant": await post("/control/snapshot", json=with_(limits=[LimitsCmd(tenant="ZZ", factor=.5)])),
                   "bad_pin": await post("/control/snapshot", json=with_(pins={"K": "nope"})),
                   "bad_body": await post("/control/snapshot", json={"version": "x"}),
                   "no_token": await post("/control/snapshot", json=with_(), headers={H_CONTROL_TOKEN: "x"})}
            out["ok"] = await post("/control/snapshot", json=with_(
                limits=[LimitsCmd(tenant="K", factor=0.1, weight_factor=0.5, ttl_s=0.3)]))
            out["view"] = await c.get("/admin/tenants/K")
            out["ov"] = await c.get("/admin/overrides")
            await asyncio.sleep(0.35)
            out["view2"] = await c.get("/admin/tenants/K")
            return out
    o = run(go())
    for k in ("bad_factor", "bad_wf", "bad_ttl", "bad_tenant", "bad_pin", "bad_body"):
        assert o[k].status_code == 400 and o[k].json()["ok"] is False, k
    assert o["no_token"].status_code == 401
    assert o["ok"].status_code == 200 and o["ok"].json()["ok"]
    v = TenantAdminView(**o["view"].json())
    assert v.factor == 0.1 and v.weight_factor == 0.5 and v.project == "p1"
    assert (v.effective_quota.burst, v.effective_quota.concurrency) == (1, 1)
    assert v.effective_quota.rate_rps == pytest.approx(10) and v.quota.rate_rps == 100
    ov = OverridesView(**o["ov"].json())
    assert "limits:K" in ov.expires_at and ov.snapshot_version == 2 and not ov.fail_static
    v2 = TenantAdminView(**o["view2"].json())
    assert v2.factor == 1.0 and v2.effective_quota == v2.quota      # reverted by TTL


def test_block_and_overrides_ignored_outside_tenantguard_mode():
    gw, app, up = make([tenant("K"), tenant("A")], mode="industry")

    async def go():
        async with client(app) as c:
            await c.post("/control/block", json={"tenant": "K", "ttl_s": 30})
            r1 = await c.get("/api/light", headers=key("K"))
            await c.post("/admin/mode", json={"mode": "tenantguard"})
            r2 = await c.get("/api/light", headers=key("K"))
            await c.post("/control/block", json={"tenant": "K", "blocked": False})
            r3 = await c.get("/api/light", headers=key("K"))
            bad = await c.post("/admin/mode", json={"mode": "chaos"})
            unknown = await c.post("/control/block", json={"tenant": "ZZ", "ttl_s": 30})
            return r1, r2, r3, bad, unknown
    r1, r2, r3, bad, unknown = run(go())
    assert r1.status_code == 200 and r2.status_code == 429 and r2.json()["reasons"] == ["blocked"]
    assert int(r2.headers["Retry-After"]) == 30 and r3.status_code == 200
    assert bad.status_code == 422 and unknown.status_code == 400


def test_admin_views_never_show_keys():
    gw, app, up = make([tenant("A"), tenant("B", project="p2")], replicas={"p1": ["r1", "r2"], "p2": []})

    async def go():
        async with client(app) as c:
            return (await c.get("/admin/tenants")).json(), (await c.get("/admin/tenants/ZZ")).status_code
    lst, missing = run(go())
    assert [t["id"] for t in lst] == ["A", "B"] and missing == 404
    a = next(t for t in lst if t["id"] == "A")
    assert sorted(a["replicas"]) == ["r1", "r2"] and a["project"] == "p1"
    assert "api_key" not in json.dumps(lst) and "key_sha256" not in json.dumps(lst)


# ---------- costs ----------
def test_cost_seed_size_classes_and_express_lane():
    gw, app, up = make([tenant("A")])
    gw.costs.seed = {"GET /api/search": 3.831, "GET /api/export": 19.498}

    async def go():
        async with client(app) as c:
            s1 = await c.get("/api/search?n=1000", headers=key("A"))
            s2 = await c.get("/api/search?n=500000&cpu_ms=40", headers=key("A"))
            s3 = await c.get("/api/search?n=1000", headers=key("A"))
            return s1, s2, s3
    s1, s2, s3 = run(go())
    assert s1.headers["X-TenantGuard-Lane"] == "express"            # seeded 3.8 ms < express 5 ms
    assert up.seen[0].headers[H_CLASS] != up.seen[1].headers[H_CLASS]   # size bucket splits the class
    assert up.seen[0].headers[H_CLASS] == up.seen[2].headers[H_CLASS]
    assert gw.costs.measured[("A", up.seen[1].headers[H_CLASS])] == 40.0   # the big class learned its own cost


# ---------- telemetry ----------
def test_telemetry_ws_frames_latest_and_metrics():
    gw, app, up = make([tenant("A"), tenant("K", rate_rps=0.1, burst=1)], tick_ms=100, ticker=True)
    with TestClient(app, headers=TOKEN) as tc:
        frames = []
        with tc.websocket_connect("/internal/telemetry") as ws:
            for _ in range(3):
                assert tc.get("/api/light", headers=key("A")).status_code == 200
            tc.get("/api/light", headers=key("K"))
            tc.get("/api/light", headers=key("K"))
            for _ in range(6):
                frames.append(TelemetryFrame(**json.loads(ws.receive_text())))
        latest = tc.get("/internal/telemetry/latest")
        metrics = tc.get("/metrics").text
    assert latest.status_code == 200 and TelemetryFrame(**latest.json()).mode == "industry"
    cells = [c for f in frames for c in f.cells if c.tenant == "A"]
    assert sum(c.count for c in cells) == 3 and all(c.p95_ms >= c.p50_ms for c in cells)
    qa = [q for f in frames for q in f.queues if q.tenant == "A"]
    assert sum(q.admitted for q in qa) == 3 and sum(q.completed for q in qa) == 3
    qk = [q for f in frames for q in f.queues if q.tenant == "K"]
    assert sum(q.rejected_429 for q in qk) == 1 and sum(q.refused_cost_ms for q in qk) > 0
    assert {ls.server for ls in frames[0].limiter} == {"r1", "r2", "r3"}
    assert 'tenantguard_rejected_total{tenant="K",reason="rate"} 1' in metrics
    assert 'tenantguard_request_duration_ms_count{tenant="A"} 3' in metrics


def test_missing_or_garbage_cpu_header_is_counted_and_not_learned():
    gw, app, up = make([tenant("K")])

    async def go():
        async with client(app) as c:
            r1 = await c.get("/api/x?fail=1", headers=key("K"))
            r2 = await c.get("/api/x?cpu_ms=abc", headers=key("K"))
            r3 = await c.get("/api/x?cpu_ms=-5", headers=key("K"))
            return r1, r2, r3, (await c.get("/metrics")).text
    r1, r2, r3, m = run(go())
    assert (r1.status_code, r2.status_code, r3.status_code) == (500, 200, 200)
    assert gw.costs.measured == {}
    assert sum(n for (t, _), n in gw.telemetry.c_cpu_missing.items() if t == "K") == 3
    assert "tenantguard_cpu_header_missing_total{" in m and 'code="5xx"} 1' in m
