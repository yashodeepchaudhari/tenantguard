"""The five Wave 2 attacks against a mock fleet: what each one sends, as whom, and how faulty_server cleans up."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tenantguard_lab.app import create_app
from tenantguard_lab.settings import load_config


@pytest.fixture
def lab(monkeypatch):
    """Lab on a mock transport that records every request. lab.refuse_fault = True makes workers answer 401."""
    monkeypatch.setenv("TENANTGUARD_ADMIN_TOKEN", "s3cret")
    cfg = load_config()
    reqs = []

    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(req)
        if req.url.path == "/admin/fault" and c.refuse_fault:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(202 if req.url.path == "/api/ingest" else 200, json={})

    app = create_app(cfg, autostart=False, transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        c.reqs, c.cfg, c.refuse_fault = reqs, cfg, False
        yield c


def run_for(lab, body, seconds=1.0):
    r = lab.post("/lab/attack", json={"intensity": 1, "duration_s": 30, **body})
    assert r.status_code == 200, r.text
    st = r.json()
    time.sleep(seconds)
    assert lab.delete(f"/lab/attack/{st['attack_id']}").json()["state"] == "stopped"
    return st


def sent(lab, path):
    return [r for r in lab.reqs if r.url.path == path]


def test_blackbox_bomb_sends_cheap_ingests_as_the_attacker(lab):
    run_for(lab, {"attack": "blackbox_bomb", "tenant": "K"})
    ingests = sent(lab, "/api/ingest")
    assert len(ingests) >= 3
    assert {r.method for r in ingests} == {"POST"}
    assert {json.loads(r.content)["docs"] for r in ingests} == {lab.cfg.attacks["blackbox_bomb"]["docs"]}
    assert {r.headers.get("X-API-Key") for r in lab.reqs} == {"tk_K_demo"}
    m = lab.get("/lab/metrics", params={"window_s": 5}).json()
    assert m["tenants"]["K"]["attack"]["count"] >= 3


def test_background_bomb_writes_with_heavy_indexing(lab):
    run_for(lab, {"attack": "background_bomb", "tenant": "K"})
    writes = sent(lab, "/api/write")
    assert len(writes) >= 3
    bodies = [json.loads(r.content) for r in writes]
    assert {b["index_rounds"] for b in bodies} == {lab.cfg.attacks["background_bomb"]["index_rounds"]}
    assert all(b["key"] and b["value"] for b in bodies)
    assert {r.headers.get("X-API-Key") for r in lab.reqs} == {"tk_K_demo"}


def test_cache_flood_reads_keys_that_never_repeat(lab):
    run_for(lab, {"attack": "cache_flood", "tenant": "K"})
    keys = [r.url.params["key"] for r in sent(lab, "/api/read")]
    assert len(keys) >= 20                               # 4 x K's 10 req/s
    assert all(k.startswith("flood") for k in keys)
    assert len(set(keys)) >= len(keys) - 1               # 10M keyspace: at most a rare repeat
    assert {r.headers.get("X-API-Key") for r in lab.reqs} == {"tk_K_demo"}


def test_honest_spike_is_the_tenants_own_mix(lab):
    run_for(lab, {"attack": "honest_spike", "tenant": "J"}, seconds=1.5)
    assert {r.headers.get("X-API-Key") for r in lab.reqs} == {"tk_J_demo"}
    paths = {r.url.path for r in lab.reqs}
    mix_paths = {lab.cfg.endpoints[k].path for k in lab.cfg.persona("J").mix}
    assert paths <= mix_paths and len(paths) >= 2       # several kinds, nothing outside J's usual traffic
    assert len(lab.reqs) >= 25                           # ~3 x J's 14 req/s


def test_faulty_server_injects_then_clears_the_fault(lab):
    st = lab.post("/lab/attack", json={"attack": "faulty_server", "server": "w4", "duration_s": 30}).json()
    assert st["state"] == "running" and st["server"] == "w4" and st["tenant"] is None
    time.sleep(0.5)
    w4 = httpx.URL(lab.cfg.workers["w4"])
    post = [r for r in lab.reqs if r.method == "POST"]
    assert len(post) == 1 and post[0].url.path == "/admin/fault" and post[0].url.port == w4.port
    assert post[0].headers["X-Admin-Token"] == "s3cret" and "X-API-Key" not in post[0].headers
    body = json.loads(post[0].content)
    assert body["latency_ms"] == 150 and body["cpu_burn_pct"] == 30 and body["duration_s"] >= 30  # TTL backstop
    assert not [r for r in lab.reqs if r.method == "DELETE"]          # still faulty while the attack runs
    assert lab.delete(f"/lab/attack/{st['attack_id']}").json()["state"] == "stopped"
    deletes = [r for r in lab.reqs if r.method == "DELETE"]
    assert len(deletes) == 1 and deletes[0].url.path == "/admin/fault" and deletes[0].url.port == w4.port
    assert deletes[0].headers["X-Admin-Token"] == "s3cret"
    assert len(lab.reqs) == 2                                          # no tenant traffic at all


def test_faulty_server_intensity_scales_the_fault(lab):
    st = lab.post("/lab/attack", json={"attack": "faulty_server", "server": "w2", "intensity": 2}).json()
    time.sleep(0.3)
    body = json.loads(sent(lab, "/admin/fault")[0].content)
    assert body["latency_ms"] == 300 and body["cpu_burn_pct"] == 60
    lab.delete(f"/lab/attack/{st['attack_id']}")


def test_faulty_server_refused_ends_at_once(lab):
    lab.refuse_fault = True
    lab.post("/lab/attack", json={"attack": "faulty_server", "server": "w4", "duration_s": 30})
    time.sleep(1.0)
    a = lab.get("/lab/status").json()["attacks"][0]
    assert a["state"] == "done"                          # not left "running" for 30 s with no fault in place


def test_wave2_validation(lab):
    assert lab.post("/lab/attack", json={"attack": "faulty_server"}).status_code == 422
    assert lab.post("/lab/attack", json={"attack": "faulty_server", "tenant": "K"}).status_code == 422
    assert lab.post("/lab/attack", json={"attack": "faulty_server", "server": "w99"}).status_code == 404
    for name in ["background_bomb", "blackbox_bomb", "cache_flood", "honest_spike"]:
        assert lab.post("/lab/attack", json={"attack": name}).status_code == 422, name
        assert lab.post("/lab/attack", json={"attack": name, "tenant": "Z"}).status_code == 404, name
    # tools/demo.py preflight probes with an unknown tenant AND server: shipped attacks must answer 4xx, not 501
    for name in ["background_bomb", "blackbox_bomb", "cache_flood", "faulty_server", "honest_spike"]:
        r = lab.post("/lab/attack", json={"attack": name, "tenant": "__probe__", "server": "__probe__"})
        assert r.status_code == 404, name
    assert lab.get("/lab/status").json()["attacks"] == []   # nothing was started
    assert lab.reqs == []


def test_runs_accept_wave2_and_faulty_server_blames_nobody(lab):
    r = lab.post("/lab/run", json={"scenario": "blackbox_bomb", "mode": "off", "calm_s": 30, "attack_s": 30})
    assert r.status_code == 202 and r.json()["attacker"] == "K"
    assert lab.delete("/lab/run").json()["state"] == "cancelled"
    r = lab.post("/lab/run", json={"scenario": "faulty_server", "server": "w4", "mode": "tenantguard",
                                   "calm_s": 30, "attack_s": 30})
    assert r.status_code == 202 and r.json()["attacker"] is None
    assert lab.delete("/lab/run").json()["state"] == "cancelled"
    assert lab.post("/lab/run", json={"scenario": "faulty_server", "mode": "off"}).status_code == 422
