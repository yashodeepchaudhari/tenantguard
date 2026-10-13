from __future__ import annotations

import time

import pytest

from app import fault as fault_mod
from app.blackbox import BlackBox
from conftest import hdr
from tenantguard_common import contracts


def wait_for(pred, timeout=15.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def bb():
    box = BlackBox(cpu_ms_per_doc=2.0, queue_max=100)
    assert wait_for(lambda: box.status()["running"])
    yield box
    box.close()


def test_blackbox_cpu_never_in_tenant_meters(client, worker):
    assert wait_for(lambda: worker.blackbox.status()["running"])
    s0 = client.get("/stats").json()
    r = client.post("/api/ingest", json={"docs": 150}, headers=hdr("K", cls="ingest"))
    assert r.status_code == 202 and r.json()["accepted"] == 150
    handler_ms = float(r.headers["X-TenantGuard-Cpu-Ms"])
    assert handler_ms < 5                                          # the handler only enqueues
    assert wait_for(lambda: worker.blackbox.status()["processed"] >= 150)
    s1 = client.get("/stats").json()
    bb_ms = s1["blackbox_cpu_ms"] - s0["blackbox_cpu_ms"]
    assert bb_ms >= 150 * 2.0 * 0.8                                # ~2 ms per doc, in the child
    tenant_ms = sum(t["handler_cpu_ms"] + t["bg_cpu_ms"] for t in s1["tenants"])
    k = next(t for t in s1["tenants"] if t["tenant"] == "K")
    assert k["handler_cpu_ms"] == pytest.approx(handler_ms, abs=0.01)
    assert k["bg_cpu_ms"] == 0.0
    assert tenant_ms < bb_ms / 10                                  # invisible to every tenant meter
    # ...but the process tree does see it: unattributed CPU = total - tenants.
    assert s1["cpu_total_ms"] - s0["cpu_total_ms"] >= bb_ms * 0.8
    assert [c for c in s1["classes"] if c["cls"] == "ingest"][0]["cpu_ms_total"] < 5


def test_queue_is_bounded_and_drops_are_counted(bb):
    first = bb.submit(80)
    second = bb.submit(80)          # the child has had at most a few ms to drain
    assert first == {"accepted": 80, "dropped": 0, "queued": 80}
    assert second["accepted"] + second["dropped"] == 80 and second["dropped"] >= 55
    assert second["queued"] <= 100
    st = bb.status()
    assert st["dropped"] == second["dropped"] and st["accepted"] == 80 + second["accepted"]
    assert wait_for(lambda: bb.status()["queued"] == 0)
    assert bb.submit(100)["dropped"] == 0                          # space frees up as the child drains


def test_child_is_restarted_after_it_dies(bb):
    bb.submit(20)
    assert wait_for(lambda: bb.status()["processed"] >= 20)
    cpu_before = bb.cpu_ms()
    pid = bb.status()["pid"]
    bb.kill_child()
    assert wait_for(lambda: bb.status()["restarts"] == 1 and bb.status()["running"])
    assert bb.status()["pid"] != pid
    assert bb.cpu_ms() >= cpu_before                               # cumulative across restarts
    bb.submit(20)
    assert wait_for(lambda: bb.status()["processed"] >= 40)        # the new child does the work


def test_ingest_endpoint_reports_drops(make_client):
    c = make_client(seed_rows=5, blackbox_queue_max=10)
    w = c.app.state.worker
    w.blackbox.close()
    w.blackbox = BlackBox(cpu_ms_per_doc=50.0, queue_max=10)
    try:
        r = c.post("/api/ingest", json={"docs": 25}, headers=hdr("K"))
        assert r.status_code == 202 and r.json()["accepted"] == 10 and r.json()["dropped"] == 15
        assert c.post("/api/ingest", json={"docs": 0}, headers=hdr("K")).status_code == 422
        assert c.get("/healthz").json()["blackbox"]["dropped"] == 15
    finally:
        w.blackbox.close()


def test_fault_req_comes_from_contracts():
    assert fault_mod.FaultReq is contracts.FaultReq
    import app.main as main_mod
    assert main_mod.FaultReq is contracts.FaultReq


def test_config_reads_yaml_worker_keys():
    from app.config import load_config
    from tenantguard_common.config import load_settings
    w = load_settings()["worker"]
    cfg = load_config()
    assert cfg.cache_mb == w["cache_mb"] and cfg.bg_threads == w["bg_threads"] and cfg.seed_rows == w["seed_rows"]
    assert cfg.cache_guaranteed_share == w["cache_guaranteed_share"]
    assert cfg.blackbox_cpu_ms_per_doc == w["blackbox"]["cpu_ms_per_doc"]
    assert cfg.blackbox_queue_max == w["blackbox"]["queue_max"]


def test_stats_count_blackbox_docs_per_tenant(client, worker):
    """v2 attribution: the brain splits black-box CPU by documents handed over per tenant."""
    assert client.post("/api/ingest", json={"docs": 30}, headers=hdr("K")).status_code == 202
    assert client.post("/api/ingest", json={"docs": 10}, headers=hdr("A")).status_code == 202
    docs = client.get("/stats").json()["docs_by_tenant"]
    assert docs == {"K": 30, "A": 10}


def test_tenant_list_comes_from_env(monkeypatch):
    from app.config import load_config
    monkeypatch.setenv("TENANTGUARD_TENANTS", "A, D")
    assert load_config(server="t").tenants == ["A", "D"]
