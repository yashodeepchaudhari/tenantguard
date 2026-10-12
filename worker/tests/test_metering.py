from __future__ import annotations

import time

import pytest

from app.cpuclock import thread_cpu
from app.tenancy import tenant_var
from conftest import hdr
from tenantguard_common.contracts import WorkerStats


def usage(client, tenant):
    s = WorkerStats(**client.get("/stats").json())
    return next(t for t in s.tenants if t.tenant == tenant)


def test_background_job_charged_to_the_writing_tenant(client, worker):
    before_a, before_b = usage(client, "A"), usage(client, "B")
    r = client.post("/api/write", json={"key": "doc", "value": "hello indexed world", "index_rounds": 3000},
                    headers=hdr("A"))
    assert r.status_code == 200
    assert worker.jobs.drain(10)
    a, b = usage(client, "A"), usage(client, "B")
    assert a.bg_cpu_ms - before_a.bg_cpu_ms > 1.0          # 3000 rounds is several ms of CPU
    assert b.bg_cpu_ms == before_b.bg_cpu_ms == 0.0
    # Background CPU is not in the handler header / handler meter.
    assert float(r.headers["X-TenantGuard-Cpu-Ms"]) < a.bg_cpu_ms
    # The job wrote into A's DB (index terms), proving it ran in A's context.
    token = tenant_var.set("A")
    try:
        assert worker.store.find_term("indexed") == ["doc"]
    finally:
        tenant_var.reset(token)
    token = tenant_var.set("B")
    try:
        assert worker.store.find_term("indexed") == []
    finally:
        tenant_var.reset(token)


def test_concurrent_bg_jobs_split_by_tenant(client, worker):
    for i in range(6):
        client.post("/api/write", json={"value": f"alpha {i}", "index_rounds": 2000}, headers=hdr("A"))
        client.post("/api/write", json={"value": f"beta {i}", "index_rounds": 0}, headers=hdr("K"))
    assert worker.jobs.drain(10)
    a, k = usage(client, "A"), usage(client, "K")
    assert a.bg_cpu_ms > 5 * k.bg_cpu_ms


def test_meters_add_up(client):
    sent = {"A": [], "K": []}
    plan = [("A", "get", "/api/light", {}, None), ("A", "get", "/api/read", {"params": {"key": "k0003"}}, None),
            ("K", "get", "/api/search", {"params": {"n": 50_000}}, None),
            ("K", "post", "/api/write", {"json": {"value": "x" * 2000, "index_rounds": 0}}, None),
            ("A", "get", "/api/export", {"params": {"pause_ms": 0}}, None)]
    for rnd in range(3):
        for tenant, method, path, kw, _ in plan:
            r = getattr(client, method)(path, headers=hdr(tenant, cls=path.split("/")[-1][:8]), **kw)
            assert r.status_code == 200, r.text
            sent[tenant].append((path, float(r.headers["X-TenantGuard-Cpu-Ms"]), len(r.request.content or b"")))
    s = WorkerStats(**client.get("/stats").json())
    for tenant, reqs in sent.items():
        u = next(t for t in s.tenants if t.tenant == tenant)
        assert u.requests == len(reqs)
        # header values are rounded to 0.001 ms
        assert u.handler_cpu_ms == pytest.approx(sum(c for _, c, _ in reqs), abs=0.002 * len(reqs))
        assert u.bytes_in == sum(b for _, _, b in reqs)
        classes = [c for c in s.classes if c.tenant == tenant]
        assert sum(c.count for c in classes) == u.requests
        assert sum(c.cpu_ms_total for c in classes) == pytest.approx(u.handler_cpu_ms, abs=0.01)
        assert u.db_ms > 0
    k = next(t for t in s.tenants if t.tenant == "K")
    search = next(c for c in s.classes if c.tenant == "K" and c.cls == "search")
    assert search.count == 3 and search.cpu_ms_total > 3 * 5  # 50k docs is >5 ms each
    # Attributed CPU can never exceed what the process actually used.
    total_attr = sum(t.handler_cpu_ms + t.bg_cpu_ms for t in s.tenants)
    assert total_attr <= s.cpu_total_ms
    assert s.started_at <= s.ts and s.cpu_limit_cores > 0 and s.mem_mb > 10


def test_cpu_header_tracks_real_work(client):
    small = float(client.get("/api/search", params={"n": 1000}, headers=hdr("A")).headers["X-TenantGuard-Cpu-Ms"])
    big = float(client.get("/api/search", params={"n": 200_000}, headers=hdr("A")).headers["X-TenantGuard-Cpu-Ms"])
    assert big > 20 * small
    # A sleep is not CPU: a slow export is mostly wall time.
    t0 = time.perf_counter()
    r = client.get("/api/export", params={"pause_ms": 50, "rows": 50, "chunk": 10}, headers=hdr("A"))
    wall = (time.perf_counter() - t0) * 1000
    assert wall > 250 and float(r.headers["X-TenantGuard-Cpu-Ms"]) < wall / 3


def test_thread_cpu_clock_is_fine_grained():
    vals = []
    for _ in range(20):
        t0 = thread_cpu()
        sum(range(20_000))
        vals.append((thread_cpu() - t0) * 1000)
    assert 0 < sorted(vals)[10] < 10  # sub-tick resolution, not 0 / 15.6 ms steps


def test_class_defaults_to_none(client):
    client.get("/api/light", headers=hdr("C"))
    s = client.get("/stats").json()
    assert {"tenant": "C", "cls": "none", "count": 1} == {k: v for k, v in s["classes"][0].items() if k != "cpu_ms_total"}


def test_cpu_limit_from_env(make_client, monkeypatch):
    monkeypatch.setenv("CPU_LIMIT_CORES", "1.25")
    c = make_client(seed_rows=5)
    assert c.get("/stats").json()["cpu_limit_cores"] == 1.25
