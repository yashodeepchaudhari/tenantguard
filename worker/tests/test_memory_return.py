"""Memory must go back to the OS after load (found on the Docker host: workers stuck near their cgroup limit).

The RSS test is the real check on Linux (glibc arenas), which is what the containers run; on other
platforms it still runs as a sanity check.
"""
from __future__ import annotations

import sys
import threading

import psutil
import pytest

from app import malloc
from app.meter import OTHER_CLASS, Meters
from conftest import hdr

ADMIN = {"X-Admin-Token": "dev"}
LINUX = sys.platform.startswith("linux")


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / (1024 * 1024)


def test_rss_returns_near_baseline_after_heavy_load(make_client):
    c = make_client(seed_rows=50)
    for _ in range(3):                                    # warm up imports, thread pool, sqlite
        c.get("/api/search", params={"n": 20_000}, headers=hdr("K"))
        c.get("/api/memory", params={"mb": 1, "hold_ms": 1}, headers=hdr("K"))
    c.post("/admin/memory/trim", headers=ADMIN)
    base = rss_mb()

    errors = []

    def hold(tenant):
        for _ in range(4):
            r = c.get("/api/memory", params={"mb": 16, "hold_ms": 150}, headers=hdr(tenant))
            if r.status_code != 200:
                errors.append(r.status_code)

    def search():
        for i in range(6):
            r = c.get("/api/search", params={"n": 200_000, "q": f"whale-{i}"}, headers=hdr("K"))
            if r.status_code != 200:
                errors.append(r.status_code)

    threads = [threading.Thread(target=hold, args=(t,)) for t in "ABCD"] + \
              [threading.Thread(target=search) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rep = c.post("/admin/memory/trim", headers=ADMIN).json()
    grown = rss_mb() - base
    # 4 tenants x 16 MB were held at once (64 MB) and 24 heavy searches ran; afterwards almost nothing may stay.
    assert grown < 20, f"RSS stayed {grown:.1f} MB above baseline ({rep})"


def test_memory_hold_is_released_immediately(make_client):
    c = make_client(seed_rows=5)
    c.get("/api/memory", params={"mb": 1, "hold_ms": 1}, headers=hdr("A"))
    base = rss_mb()
    assert c.get("/api/memory", params={"mb": 30, "hold_ms": 50}, headers=hdr("A")).status_code == 200
    assert rss_mb() - base < 10                          # no trim needed: the buffer was an mmap


def test_class_meter_is_bounded_per_tenant():
    m = Meters(max_classes_per_tenant=5)
    for i in range(50):
        m.record_request("K", f"c{i}", 1.0, 0)
    m.record_request("A", "c0", 1.0, 0)
    _, classes = m.snapshot()
    k = {c.cls: c for c in classes if c.tenant == "K"}
    assert len(k) == 6 and OTHER_CLASS in k              # 5 real classes + "other"
    assert sum(c.count for c in k.values()) == 50         # nothing is lost, only folded
    assert k[OTHER_CLASS].count == 45 and k[OTHER_CLASS].cpu_ms_total == 45.0
    assert any(c.tenant == "A" and c.cls == "c0" for c in classes)   # the cap is per tenant


def test_admin_memory_endpoints(client):
    assert client.get("/admin/memory").status_code == 401
    assert client.post("/admin/memory/trim").status_code == 401
    rep = client.get("/admin/memory", headers=ADMIN).json()
    assert rep["rss_mb"] > 10 and rep["mem_limit_mb"] > 0 and "cache_mb" in rep
    before = rep["trims"]
    t = client.post("/admin/memory/trim", headers=ADMIN).json()
    assert t["trims"] == before + 1 and "rss_before_mb" in t
    if LINUX:
        assert rep["allocator"]["tuned"] is True


def test_allocator_tuning_platform_behaviour():
    if LINUX:
        assert malloc.tune() is True and malloc.info() is not None
    else:
        assert malloc.tune() is False and malloc.info() is None and malloc.trim() is False


def test_sqlite_page_cache_and_wal_are_bounded(worker):
    from app.tenancy import tenant_var
    token = tenant_var.set("A")
    try:
        with worker.store._tx() as conn:
            assert conn.execute("PRAGMA cache_size").fetchone()[0] == -1024
            assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 4194304
    finally:
        tenant_var.reset(token)


@pytest.mark.skipif(not LINUX, reason="glibc behaviour")
def test_trimmer_thread_runs(make_client):
    c = make_client(seed_rows=5, memory_trim_s=0.05)
    import time
    w = c.app.state.worker
    deadline = time.monotonic() + 3
    while w.trimmer.trims == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert w.trimmer.trims > 0
