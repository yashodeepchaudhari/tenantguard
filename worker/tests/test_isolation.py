from __future__ import annotations

import inspect

import pytest

from app.store import TenantStore
from app.tenancy import NoTenantError, tenant_var
from conftest import hdr


def test_tenant_b_cannot_read_tenant_a_written_key(client):
    r = client.post("/api/write", json={"key": "secret", "value": "A-private-data"}, headers=hdr("A"))
    assert r.status_code == 200
    assert client.get("/api/read", params={"key": "secret"}, headers=hdr("A")).json()["value"] == "A-private-data"
    # B asks for the same key: not in B's DB, not served from A's cache entry.
    r = client.get("/api/read", params={"key": "secret"}, headers=hdr("B"))
    assert r.status_code == 404
    assert "A-private-data" not in r.text


def test_same_key_resolves_to_each_tenants_own_row_db_and_cache(client):
    for _ in range(2):  # first pass from DB, second from cache
        a = client.get("/api/read", params={"key": "k0001"}, headers=hdr("A")).json()
        b = client.get("/api/read", params={"key": "k0001"}, headers=hdr("B")).json()
        assert a["value"].startswith("A|k0001|")
        assert b["value"].startswith("B|k0001|")
    assert a["source"] == b["source"] == "cache"


def test_one_sqlite_file_per_tenant(client, worker):
    client.post("/api/write", json={"key": "x", "value": "only-A"}, headers=hdr("A"))
    files = {p.name for p in worker.store.dir.glob("*.db")}
    assert {"A.db", "B.db", "L.db"} <= files
    import sqlite3
    b = sqlite3.connect(worker.store.dir / "B.db")
    assert b.execute("SELECT COUNT(*) FROM items WHERE value LIKE '%only-A%'").fetchone()[0] == 0


def test_repository_takes_tenant_only_from_context(worker):
    # No tenant parameter exists on the repository API...
    for name in ("get", "put", "page", "count", "add_terms", "find_term"):
        assert "tenant" not in inspect.signature(getattr(TenantStore, name)).parameters
    # ...and calling it outside a tenant context fails closed.
    with pytest.raises(NoTenantError):
        worker.store.get("k0001")
    with pytest.raises(NoTenantError):
        worker.cache.get("k0001")


def test_runtime_tenant_gets_own_seeded_db(client):
    r = client.get("/api/read", params={"key": "k0002"}, headers=hdr("Znew"))
    assert r.status_code == 200 and r.json()["value"].startswith("Znew|k0002|")


@pytest.mark.parametrize("bad", ["../A", "A/../../etc", "_canary", "x" * 40, "a b"])
def test_bad_tenant_ids_rejected(client, bad):
    r = client.get("/api/light", headers=hdr(bad))
    assert r.status_code == 400
    assert r.json()["error"] == "bad_tenant"


def test_missing_tenant_is_400_with_cpu_header(client):
    r = client.get("/api/light")
    assert r.status_code == 400
    assert r.json() == {"error": "missing_tenant", "tenant": None, "detail": "X-TenantGuard-Tenant header is required"}
    assert "X-TenantGuard-Cpu-Ms" in r.headers


def test_cache_eviction_respects_guaranteed_share(make_client):
    c = make_client(seed_rows=400, cache_mb=0.1, cache_guaranteed_share=0.25)
    w = c.app.state.worker
    for i in range(20):  # A warms ~13 KB, inside its 25 KB share
        c.get("/api/read", params={"key": f"k{i:04d}"}, headers=hdr("A"))
    for i in range(400):  # K floods far past capacity
        c.get("/api/read", params={"key": f"k{i:04d}"}, headers=hdr("K"))
    counters = w.cache.counters("A")
    assert counters["evictions_suffered"] == 0
    hits_before = counters["cache_hits"]
    for i in range(20):
        assert c.get("/api/read", params={"key": f"k{i:04d}"}, headers=hdr("A")).json()["source"] == "cache"
    assert w.cache.counters("A")["cache_hits"] == hits_before + 20
    assert w.cache.tenant_bytes("K") + w.cache.tenant_bytes("A") <= w.cache.capacity


def test_flooder_evicting_others_is_ledgered(make_client):
    c = make_client(seed_rows=400, cache_mb=0.1, cache_guaranteed_share=0.05)
    w = c.app.state.worker
    for i in range(60):  # A is far above its small share
        c.get("/api/read", params={"key": f"k{i:04d}"}, headers=hdr("A"))
    for i in range(300):
        c.get("/api/read", params={"key": f"k{i:04d}"}, headers=hdr("K"))
    assert w.cache.counters("K")["evictions_caused"] > 0
    assert w.cache.counters("A")["evictions_suffered"] == w.cache.counters("K")["evictions_caused"]
    assert w.cache.ledger[("K", "A")] > 0
    stats = c.get("/stats").json()
    a = next(t for t in stats["tenants"] if t["tenant"] == "A")
    assert a["evictions_suffered"] > 0


def test_tenant_var_is_clean_after_request(client):
    client.get("/api/light", headers=hdr("A"))
    assert tenant_var.get() is None
