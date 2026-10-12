from __future__ import annotations

import statistics
import time

from conftest import hdr

ADMIN = {"X-Admin-Token": "dev"}


def light_ms(client, n=15, path="/api/light", tenant="A"):
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = client.get(path, headers=hdr(tenant))
        assert r.status_code == 200
        xs.append((time.perf_counter() - t0) * 1000)
    return statistics.median(xs)


def test_fault_adds_latency_and_clears(client):
    base = light_ms(client)
    r = client.post("/admin/fault", json={"latency_ms": 80}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["fault"]["latency_ms"] == 80
    slow = light_ms(client, n=5)
    assert slow >= base + 75
    assert light_ms(client, n=3, path="/canary") >= 75   # the brain's probes see the bad host too
    assert client.get("/stats").json()["fault"]["latency_ms"] == 80
    assert client.delete("/admin/fault", headers=ADMIN).status_code == 200
    assert light_ms(client) < base + 40
    assert client.get("/stats").json()["fault"] is None


def test_fault_latency_is_not_charged_as_cpu(client):
    client.post("/admin/fault", json={"latency_ms": 100}, headers=ADMIN)
    r = client.get("/api/light", headers=hdr("A"))
    client.delete("/admin/fault", headers=ADMIN)
    assert float(r.headers["X-TenantGuard-Cpu-Ms"]) < 20


def test_fault_cpu_burn_shows_in_process_cpu_not_tenants(client):
    s0 = client.get("/stats").json()
    client.post("/admin/fault", json={"cpu_burn_pct": 80}, headers=ADMIN)
    time.sleep(0.5)
    client.delete("/admin/fault", headers=ADMIN)
    s1 = client.get("/stats").json()
    assert s1["cpu_total_ms"] - s0["cpu_total_ms"] > 200
    assert sum(t["handler_cpu_ms"] + t["bg_cpu_ms"] for t in s1["tenants"]) < 50


def test_fault_duration_auto_clears(client):
    client.post("/admin/fault", json={"latency_ms": 50, "duration_s": 0.2}, headers=ADMIN)
    time.sleep(0.3)
    assert client.get("/stats").json()["fault"] is None


def test_fault_requires_admin_token(client):
    assert client.post("/admin/fault", json={"latency_ms": 10}).status_code == 401
    assert client.post("/admin/fault", json={"latency_ms": 10}, headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.delete("/admin/fault").status_code == 401
    assert client.post("/admin/fault", json={"latency_ms": -1}, headers=ADMIN).status_code == 422
    assert client.get("/stats").json()["fault"] is None


def test_body_limit_413(client):
    big = "x" * (1_048_576 + 10)
    r = client.post("/api/write", content=big, headers={**hdr("A"), "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
    ok = client.post("/api/write", json={"value": "y" * 500_000, "index_rounds": 0}, headers=hdr("A"))
    assert ok.status_code == 200


def test_body_limit_without_content_length(client):
    def chunks():
        for _ in range(20):
            yield b"z" * 100_000
    r = client.post("/api/write", content=chunks(), headers=hdr("A"))
    assert r.status_code == 413


def test_memory_within_budget_then_507(client, worker):
    r = client.get("/api/memory", params={"mb": 8, "hold_ms": 10}, headers=hdr("A"))
    assert r.status_code == 200 and r.json()["mb"] == 8
    r = client.get("/api/memory", params={"mb": 40, "hold_ms": 10}, headers=hdr("A"))
    assert r.status_code == 507 and r.json()["error"] == "tenant_memory_budget" and r.json()["tenant"] == "A"


def test_memory_budget_is_per_tenant_and_gauged(make_client):
    import threading
    c = make_client(seed_rows=5)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", c.get("/api/memory", params={"mb": 30, "hold_ms": 1500},
                                                                   headers=hdr("A"))))
    t.start()

    def gauge_a() -> float:
        return next(x for x in c.get("/stats").json()["tenants"] if x["tenant"] == "A")["mem_mb"]

    deadline = time.monotonic() + 5
    gauge = gauge_a()
    while gauge < 30 and time.monotonic() < deadline:
        time.sleep(0.02)
        gauge = gauge_a()
    second_a = c.get("/api/memory", params={"mb": 5, "hold_ms": 1}, headers=hdr("A")).status_code
    b = c.get("/api/memory", params={"mb": 30, "hold_ms": 1}, headers=hdr("B")).status_code
    t.join()
    assert gauge >= 30
    assert second_a == 507       # A is at its own budget...
    assert b == 200              # ...which does not touch B's
    assert out["a"].status_code == 200
    assert next(x for x in c.get("/stats").json()["tenants"] if x["tenant"] == "A")["mem_mb"] < 1


def test_endpoints_smoke(client):
    h = hdr("A")
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/api/read", params={"key": "nope"}, headers=h).status_code == 404
    assert client.get("/api/search", params={"n": 0}, headers=h).status_code == 422
    assert len(client.get("/api/search", params={"n": 5000}, headers=h).json()["hits"]) == 10
    assert client.get("/api/export", params={"pause_ms": 0}, headers=h).json()["rows"] == 50
    can = client.get("/canary").json()
    assert can["ok"] and [p["kind"] for p in can["probes"]] == ["cpu", "cache", "db"]
    assert client.get("/canary/db").status_code == 200
    assert client.get("/canary/bogus").status_code == 404
    # canaries are not attributed to any tenant
    assert {t["tenant"] for t in client.get("/stats").json()["tenants"] if t["requests"]} == {"A"}


def test_cors_preflight(client):
    r = client.options("/api/light", headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET",
                                              "Access-Control-Request-Headers": "X-TenantGuard-Tenant"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
