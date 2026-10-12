from __future__ import annotations

import threading
import time

from conftest import hdr


def test_export_rejected_when_estimate_exceeds_max_request_s(make_client):
    c = make_client(seed_rows=5, max_request_s=1.0)
    r = c.get("/api/export", params={"rows": 100_000, "chunk": 1, "pause_ms": 1000}, headers=hdr("A"))
    assert r.status_code == 422 and r.json()["error"] == "request_too_slow"
    r = c.get("/api/export", params={"rows": 5, "chunk": 1, "pause_ms": 100}, headers=hdr("A"))  # 0.5 s estimate
    assert r.status_code == 200


def test_bg_backlog_caps_per_tenant_and_total(make_client):
    c = make_client(seed_rows=5, bg_threads=1, bg_pending_per_tenant=3, bg_pending_total=5)
    w = c.app.state.worker
    gate = threading.Event()
    real_add_terms = w.store.add_terms

    def slow_add_terms(key, terms):
        gate.wait(10)
        real_add_terms(key, terms)

    w.store.add_terms = slow_add_terms
    try:
        codes_a = [c.post("/api/write", json={"key": f"a{i}", "value": "v", "index_rounds": 0},
                          headers=hdr("A")).status_code for i in range(4)]
        assert codes_a == [200, 200, 200, 503]
        r = c.post("/api/write", json={"key": "a-rejected", "value": "v"}, headers=hdr("A"))
        assert r.json()["error"] == "bg_backlog"
        # a 503 wrote nothing
        assert c.get("/api/read", params={"key": "a-rejected"}, headers=hdr("A")).status_code == 404
        # another tenant still has room until the worker-wide cap (5)
        codes_b = [c.post("/api/write", json={"value": "v", "index_rounds": 0}, headers=hdr("B")).status_code
                   for _ in range(3)]
        assert codes_b == [200, 200, 503]
        assert c.get("/healthz").json()["bg_pending_by_tenant"] == {"A": 3, "B": 2}
    finally:
        gate.set()
    assert w.jobs.drain(10)
    assert c.get("/healthz").json()["bg_pending_by_tenant"] == {}


def test_worker_wide_memory_cap(make_client):
    # 120 MB limit - 80 MB baseline = 40 MB for all tenants together; each tenant may hold 32.
    c = make_client(seed_rows=5, mem_limit_mb=120, mem_baseline_mb=80)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("A", c.get("/api/memory", params={"mb": 30, "hold_ms": 1500},
                                                                   headers=hdr("A")).status_code))
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if next(x for x in c.get("/stats").json()["tenants"] if x["tenant"] == "A")["mem_mb"] >= 30:
            break
        time.sleep(0.02)
    r = c.get("/api/memory", params={"mb": 20, "hold_ms": 1}, headers=hdr("B"))   # B is within its own 32 MB
    t.join()
    assert out["A"] == 200
    assert r.status_code == 507 and r.json()["error"] == "worker_memory"
    assert c.get("/api/memory", params={"mb": 20, "hold_ms": 1}, headers=hdr("B")).status_code == 200


def test_unhandled_error_is_500_json_with_cpu_header(client):
    w = client.app.state.worker

    @client.app.get("/api/boom")
    @w.metered
    def boom() -> dict:
        sum(range(50_000))
        raise RuntimeError("kaboom")

    r = client.get("/api/boom", headers=hdr("A"))
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error", "tenant": "A", "detail": "unhandled worker error"}
    assert float(r.headers["X-TenantGuard-Cpu-Ms"]) > 0          # the work done before the crash is reported
    a = next(t for t in client.get("/stats").json()["tenants"] if t["tenant"] == "A")
    assert a["requests"] == 1 and a["handler_cpu_ms"] == float(r.headers["X-TenantGuard-Cpu-Ms"])


def test_close_waits_for_running_jobs_and_store_rejects_after(make_client):
    c = make_client(seed_rows=5)
    w = c.app.state.worker
    for i in range(20):
        c.post("/api/write", json={"value": f"doc {i}", "index_rounds": 500}, headers=hdr("A"))
    w.jobs.shutdown(timeout=10)
    assert w.jobs._running == 0
    w.store.close()
    from app.store import StoreClosedError
    from app.tenancy import tenant_var
    import pytest
    token = tenant_var.set("A")
    try:
        with pytest.raises(StoreClosedError):
            w.store.get("k0001")
    finally:
        tenant_var.reset(token)
