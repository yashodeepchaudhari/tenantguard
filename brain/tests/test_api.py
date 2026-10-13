"""HTTP + WS surface: admin API (pool, projects, tenants), gateway control (snapshot, wake), UI reads."""
import asyncio
import json

from fastapi.testclient import TestClient

from tenantguard_common.contracts import H_CONTROL_TOKEN, IncidentEvent, PoolView, Snapshot, StateSnapshot

from tenantguard_brain.main import create_app
from tenantguard_brain.sim import whale_scenario

from .conftest import T0, Harness

TOK = {H_CONTROL_TOKEN: "dev"}


def make_client(cfg, until=T0 + 30):
    h = Harness(cfg, whale_scenario("K", 20, 40, 60))
    asyncio.run(h.run_until(until))
    return TestClient(create_app(cfg, start_loops=False, runtime=h.rt)), h


def test_state_pool_and_incidents(cfg):
    client, h = make_client(cfg)
    with client:
        snap = StateSnapshot.model_validate(client.get("/api/state").json())
        assert snap.mode == "tenantguard" and len(snap.tenants) == 12 and snap.pool is not None
        assert next(t for t in snap.tenants if t.tenant == "K").level == "throttle"
        pool = PoolView.model_validate(client.get("/api/pool").json())
        assert {p.id for p in pool.projects} == {"health", "commerce", "mobility", "media", "free"}
        evs = [IncidentEvent.model_validate(e) for e in client.get("/api/incidents?tenant=K").json()]
        assert evs and evs[0].stage == "detected" and evs[0].tenant == "K"
        assert client.get("/api/incidents?tenant=A").json() == []
        assert client.get("/healthz").json()["ok"] is True
        assert "K" in client.get("/api/debug").json()["ladders"]


def test_admin_needs_the_token_and_validates(cfg):
    client, h = make_client(cfg, until=T0 + 2)
    with client:
        assert client.get("/admin/registry").status_code == 401
        assert client.post("/control/wake", json={"project": "free"}).status_code == 401
        reg = client.get("/admin/registry", headers=TOK).json()
        assert len(reg["projects"]) == 5 and all("key_sha256" not in t for t in reg["tenants"])
        too_big = {"id": "big", "name": "Big", "tier": "pro", "floor_cpu": 1.0, "cap_cpu": 2.0}
        r = client.post("/admin/projects", json=too_big, headers=TOK)
        assert r.status_code == 422 and "sum of floors" in r.json()["detail"]
        assert client.post("/admin/projects/nope/tenants", json={"id": "Z", "name": "z"}, headers=TOK).status_code == 404
        assert client.post("/admin/projects/media/tenants", json={"id": "A", "name": "dup"}, headers=TOK).status_code == 409
        assert client.delete("/admin/projects/free", headers=TOK).status_code == 409   # still has tenants


def test_admin_creates_a_project_and_a_tenant_and_persists(cfg):
    client, h = make_client(cfg, until=T0 + 2)
    with client:
        r = client.put("/admin/pool", json={"cpu_total": 8.0, "mem_total_mb": 4096}, headers=TOK)
        assert r.status_code == 200 and r.json()["usable_cpu"] == 7.2
        new = {"id": "games", "name": "Games", "tier": "free", "floor_cpu": 0.5, "cap_cpu": 1.0, "warm_min": 0}
        assert client.post("/admin/projects", json=new, headers=TOK).status_code == 201
        r = client.post("/admin/projects/games/tenants", json={"id": "Q", "name": "Quiz"}, headers=TOK)
        assert r.status_code == 201
        created = r.json()
        assert created["project"] == "games" and created["api_key"].startswith("tk_")
    saved = json.loads(cfg.control.registry_path.read_text(encoding="utf-8"))
    assert "games" in saved["projects"] and saved["tenants"]["Q"]["project"] == "games"
    assert created["api_key"] not in cfg.control.registry_path.read_text(encoding="utf-8")


def test_snapshot_and_wake_for_the_gateway(cfg):
    client, h = make_client(cfg)
    with client:
        snap = Snapshot.model_validate(client.get("/snapshot", headers=TOK).json())
        assert snap.version >= 1 and len(snap.tenants) == 12 and any(c.tenant == "K" for c in snap.limits)
        assert all(len(t.key_sha256) == 64 for t in snap.tenants)
        r = client.post("/control/wake", json={"project": "free"}, headers=TOK)
        assert r.status_code == 200 and r.json()["ok"]
        assert client.post("/control/wake", json={"project": "nope"}, headers=TOK).status_code == 404


def test_ws_backfills_state_then_incidents(cfg):
    client, _ = make_client(cfg)
    with client, client.websocket_connect("/ws/events") as ws:
        assert ws.receive_json()["type"] == "state"
        assert ws.receive_json()["type"] == "incident"


def test_cors_allows_ui_origin(cfg):
    client, _ = make_client(cfg, until=T0 + 2)
    with client:
        r = client.get("/api/state", headers={"Origin": "http://127.0.0.1:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
