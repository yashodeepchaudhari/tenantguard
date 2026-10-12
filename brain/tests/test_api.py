"""HTTP + WS surface, with the background loops off and state injected directly."""
import time

from fastapi.testclient import TestClient

from tenantguard_common.contracts import IncidentEvent, StateSnapshot

from tenantguard_brain.main import create_app
from tenantguard_brain.sim import Simulator, whale_scenario

from .conftest import drive


def make_client(cfg):
    app = create_app(cfg, start_loops=False)
    brain = app.state.runtime.brain
    now = time.time()
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 20, 0), quota_mult={"K": 3}, start=now - 40)
    drive(brain, sim, now, routes_apply=False)
    return TestClient(app), brain


def test_state_and_incidents(cfg):
    client, brain = make_client(cfg)
    with client:
        r = client.get("/api/state")
        assert r.status_code == 200
        snap = StateSnapshot.model_validate(r.json())
        assert snap.mode == "tenantguard" and len(snap.tenants) == 12
        k = next(t for t in snap.tenants if t.tenant == "K")
        assert k.level == "throttle" and k.factor < 1
        evs = [IncidentEvent.model_validate(e) for e in client.get("/api/incidents?tenant=K").json()]
        evs = [e for e in evs if e.incident_id.startswith("inc-")]
        assert evs and evs[0].stage == "detected" and evs[0].tenant == "K"
        iid = evs[0].incident_id
        assert all(e.incident_id == iid for e in
                   map(IncidentEvent.model_validate, client.get(f"/api/incidents?incident_id={iid}").json()))
        assert client.get("/api/incidents?tenant=A").json() == []
        assert snap.noisy_thresholds["warn"] == cfg.ladder.warn
        assert client.get("/healthz").json()["ok"] is True
        assert "K" in client.get("/api/debug").json()["ladders"]


def test_ws_backfills_state_then_incidents(cfg):
    client, _ = make_client(cfg)
    with client, client.websocket_connect("/ws/events") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        second = ws.receive_json()
        assert second["type"] == "incident"


def test_cors_allows_ui_origin(cfg):
    client, _ = make_client(cfg)
    with client:
        r = client.get("/api/state", headers={"Origin": "http://127.0.0.1:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
