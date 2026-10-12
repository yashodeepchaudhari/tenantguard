"""POST /api/operator: human decisions the ladder obeys (release / extend / resume)."""
import time

from fastapi.testclient import TestClient

from tenantguard_common.contracts import H_CONTROL_TOKEN, OperatorCmd

from tenantguard_brain.core import Brain
from tenantguard_brain.main import create_app
from tenantguard_brain.sim import Simulator, whale_scenario

from .conftest import T0, drive

TOK = {H_CONTROL_TOKEN: "dev"}


def throttled_k(cfg, until=T0 + 40):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 200, 0), quota_mult={"K": 3}, start=T0)
    drive(brain, sim, until, routes_apply=False)          # the ladder alone, K is throttled by +30 s
    assert brain.ladders["K"].acting
    return brain, sim


def k_events(events):
    return [e for e in events if e.incident_id.startswith("inc-")]


def test_release_clears_and_suppresses_re_escalation(cfg):
    brain, sim = throttled_k(cfg)
    ev = brain.operator(OperatorCmd(tenant="K", decision="release", until_s=20, by="asha", case_id="c-1"), sim.now)
    assert ev[0].level == "normal" and "Operator hold by asha" in ev[0].detail and "c-1" in ev[0].detail
    assert brain.ladders["K"].level == "normal" and not brain.ladders["K"].acting
    events, cmds = drive(brain, sim, T0 + 55, routes_apply=False)      # attack still on, hold lasts 20 s
    assert cmds[0][1].factor == 1.0 and cmds[0][1].weight_factor == 1.0   # the clear is sent at once
    held = [(t, c) for t, c in cmds if t < T0 + 60]
    assert all(c.factor == 1.0 for _, c in held)                         # no re-escalation during the hold
    notes = [e for e in k_events(events) if "escalation suppressed" in e.detail]
    assert len(notes) == 1                                               # noted once, not every tick
    # after the hold the ladder resumes and, the attack still running, escalates again with a fresh hold
    events, cmds = drive(brain, sim, T0 + 80, routes_apply=False)
    ended = [e for e in k_events(events) if "ended" in e.detail]
    assert ended and brain.ladders["K"].level != "normal"
    first_up = next(e for e in k_events(events) if e.prev_level == "normal" and e.level == "warn")
    assert first_up.ts - ended[0].ts >= cfg.ladder.hold["warn"] - 1e-6   # needed a full hold again


def test_extend_holds_the_level_and_keeps_renewing(cfg):
    brain, sim = throttled_k(cfg)
    k = brain.ladders["K"]
    level, factor = k.level, k.factor
    brain.operator(OperatorCmd(tenant="K", decision="extend", until_s=40, by="ravi"), sim.now)
    sim.phases = whale_scenario("K", 20, 10, 200)        # the attack stops at +30 s: calm would relax K
    events, cmds = drive(brain, sim, T0 + 79, routes_apply=False)
    assert (k.level, k.factor) == (level, factor)       # held, no AIMD recovery
    assert cmds and all(c.factor == factor for _, c in cmds)          # renewals only
    events, cmds = drive(brain, sim, T0 + 120, routes_apply=False)
    assert any("ended" in e.detail for e in k_events(events))
    assert k.factor > factor                              # the ladder resumed and relaxed


def test_resume_cancels_the_hold(cfg):
    brain, sim = throttled_k(cfg)
    brain.operator(OperatorCmd(tenant="K", decision="release", until_s=600, by="asha"), sim.now)
    assert "K" in brain.holds
    ev = brain.operator(OperatorCmd(tenant="K", decision="resume", by="asha"), sim.now)
    assert "K" not in brain.holds and "back in charge" in ev[0].detail
    drive(brain, sim, T0 + 60, routes_apply=False)
    assert brain.ladders["K"].acting                      # the attack continues: re-escalated


def make_client(cfg):
    app = create_app(cfg, start_loops=False)
    brain = app.state.runtime.brain
    now = time.time()
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 20, 0), quota_mult={"K": 3}, start=now - 40)
    drive(brain, sim, now, routes_apply=False)
    return TestClient(app), brain


def test_operator_endpoint_validation(cfg):
    client, brain = make_client(cfg)
    body = {"tenant": "K", "decision": "release", "until_s": 60, "by": "asha"}
    with client:
        assert client.post("/api/operator", json=body).status_code == 401
        assert client.post("/api/operator", json=body, headers={H_CONTROL_TOKEN: "nope"}).status_code == 401
        for bad in (0, -5, cfg.ttl_max_s + 1):
            r = client.post("/api/operator", json={**body, "until_s": bad}, headers=TOK)
            assert r.status_code == 400 and r.json()["ok"] is False
        r = client.post("/api/operator", content=b'{"tenant":"K","decision":"release","until_s":Infinity,"by":"a"}',
                        headers={**TOK, "content-type": "application/json"})
        assert r.status_code in (400, 422)                # never accepted
        assert client.post("/api/operator", json={**body, "tenant": "ZZ"}, headers=TOK).status_code == 404
        assert client.post("/api/operator", json={**body, "decision": "nuke"}, headers=TOK).status_code == 422
        r = client.post("/api/operator", json=body, headers=TOK)
        assert r.status_code == 200 and r.json() == {"ok": True, "detail": "release K by asha for 60 s"}
        assert "K" in brain.holds and brain.ladders["K"].level == "normal"
        evs = client.get("/api/incidents?tenant=K").json()
        assert any("Operator hold by asha" in e["detail"] for e in evs)
