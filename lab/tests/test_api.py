import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tenantguard_lab.app import create_app
from tenantguard_lab.settings import load_config

ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def client():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.headers.get("X-API-Key"), req.url.path))
        if req.url.path == "/api/light" and len([c for c in calls if c[0] == "tk_K_demo"]) % 2:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={})

    app = create_app(load_config(), autostart=False, transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        c.calls = calls
        yield c


def test_status_starts_idle(client):
    s = client.get("/lab/status").json()
    assert s == {"attacks": [], "calm_running": False, "run": None}


def test_wave2_attacks_are_501(client):
    for name in ["background_bomb", "blackbox_bomb", "cache_flood", "faulty_server", "honest_spike"]:
        r = client.post("/lab/attack", json={"attack": name, "tenant": "K"})
        assert r.status_code == 501, name
    assert client.post("/lab/run", json={"scenario": "blackbox_bomb", "mode": "off"}).status_code == 501


def test_attack_validation(client):
    assert client.post("/lab/attack", json={"attack": "whale", "tenant": "K", "intensity": 6}).status_code == 422
    assert client.post("/lab/attack", json={"attack": "whale", "tenant": "K", "intensity": 0.05}).status_code == 422
    assert client.post("/lab/attack", json={"attack": "nope", "tenant": "K"}).status_code == 422
    assert client.post("/lab/attack", json={"attack": "whale", "tenant": "Z"}).status_code == 404
    assert client.post("/lab/attack", json={"attack": "whale"}).status_code == 422
    assert client.delete("/lab/attack/atk-missing").status_code == 404


def test_attack_lifecycle_and_metrics(client):
    r = client.post("/lab/attack", json={"attack": "flood", "tenant": "K", "intensity": 1, "duration_s": 30})
    assert r.status_code == 200
    st = r.json()
    assert st["state"] == "running" and st["tenant"] == "K" and st["ends_at"] > st["started_at"]
    time.sleep(1.0)
    assert client.get("/lab/status").json()["attacks"][0]["state"] == "running"
    m = client.get("/lab/metrics", params={"window_s": 5}).json()
    k = m["tenants"]["K"]
    assert k["attack"]["count"] > 0 and k["r429"] > 0
    assert m["attacks"][st["attack_id"]]["counts"]["sent"] > 0
    stopped = client.delete(f"/lab/attack/{st['attack_id']}").json()
    assert stopped["state"] == "stopped"
    assert {c[0] for c in client.calls} == {"tk_K_demo"}      # only the attacker's key was used


def test_attack_ends_by_itself_as_done(client):
    st = client.post("/lab/attack", json={"attack": "hog", "tenant": "B", "duration_s": 0.5}).json()
    time.sleep(1.5)
    assert client.get("/lab/status").json()["attacks"][0]["state"] == "done"
    assert any(path == "/api/export" for _, path in client.calls)


def test_calm_start_stop_and_cors(client):
    assert client.post("/lab/calm/start", json={"rate_scale": 1.0}).json()["calm_running"] is True
    time.sleep(0.5)
    assert client.post("/lab/calm/stop").json()["calm_running"] is False
    r = client.options("/lab/attack", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert r.headers["access-control-allow-origin"] == ORIGIN
    r = client.options("/lab/attack", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_run_conflict_and_personas(client):
    r = client.post("/lab/run", json={"scenario": "whale", "mode": "off", "calm_s": 30, "attack_s": 30})
    assert r.status_code == 202 and r.json()["attacker"] == "K"
    assert client.get("/lab/status").json()["run"] == r.json()["run_id"]
    assert client.post("/lab/run", json={"scenario": "flood", "mode": "off"}).status_code == 409
    assert client.delete("/lab/run").json()["state"] == "cancelled"
    p = client.get("/lab/personas").json()
    assert len(p["personas"]) == 12 and {x["name"] for x in p["personas"]} >= {"KaamKaro", "Aarogya Health"}


def test_results_newest_first_and_filters(tmp_path):
    from tenantguard_common.contracts import PhaseP99, RunResult
    cfg = load_config()
    cfg.results_dir = tmp_path
    p = PhaseP99(baseline=1, attack=2, after=1)
    for i, (sc, mode) in enumerate([("whale", "off"), ("whale", "tenantguard"), ("flood", "off")]):
        r = RunResult(run_id=f"r{i}", scenario=sc, mode=mode, started_at=1000.0 + i, victim_p99_ms=p)
        (tmp_path / f"{i}-{sc}-{mode}.json").write_text(r.model_dump_json(), encoding="utf-8")
    (tmp_path / "junk.json").write_text("{not json", encoding="utf-8")
    with TestClient(create_app(cfg, autostart=False, transport=httpx.MockTransport(lambda r: httpx.Response(200)))) as c:
        assert [r["run_id"] for r in c.get("/lab/results").json()] == ["r2", "r1", "r0"]
        assert [r["run_id"] for r in c.get("/lab/results", params={"scenario": "whale"}).json()] == ["r1", "r0"]
        assert [r["run_id"] for r in c.get("/lab/results", params={"scenario": "whale", "mode": "off"}).json()] == ["r0"]
        assert c.get("/lab/results", params={"mode": "bogus"}).status_code == 422
