"""POST /api/operator: human decisions the ladder obeys (release / extend / resume)."""
import asyncio

from fastapi.testclient import TestClient

from tenantguard_common.contracts import H_CONTROL_TOKEN, OperatorCmd

from tenantguard_brain.main import create_app
from tenantguard_brain.sim import whale_scenario

from .conftest import T0, Harness

TOK = {H_CONTROL_TOKEN: "dev"}


def throttled_k(cfg, attack_s=200, until=T0 + 30):
    h = Harness(cfg, whale_scenario("K", 20, attack_s, 200))
    asyncio.run(h.run_until(until))          # K is throttled by +30 s
    assert h.rt.brain.ladders["K"].acting
    return h


def limits_k(h, since):
    return [next((c for c in s.limits if c.tenant == "K"), None) for s in h.snapshots[since:]]


def test_release_clears_and_suppresses_re_escalation(cfg):
    h = throttled_k(cfg)
    brain = h.rt.brain
    ev = brain.operator(OperatorCmd(tenant="K", decision="release", until_s=20, by="asha", case_id="c-1"), h.sim.now)
    assert ev[0].level == "normal" and "Operator hold by asha" in ev[0].detail and "c-1" in ev[0].detail
    assert brain.ladders["K"].level == "normal" and brain.limits() == []
    n = len(h.snapshots)
    asyncio.run(h.run_until(T0 + 45))                       # attack still on, hold lasts 20 s
    assert all(c is None for c in limits_k(h, n))           # no re-escalation during the hold
    notes = [e for e in brain.book.log if "escalation suppressed" in e.detail]
    assert len(notes) == 1                                  # noted once, not every tick
    asyncio.run(h.run_until(T0 + 60))
    ended = [e for e in brain.book.log if "ended" in e.detail]
    assert ended and brain.ladders["K"].level != "normal"   # the ladder resumed and escalated again


def test_extend_holds_the_level(cfg):
    h = throttled_k(cfg, attack_s=10)                       # the attack stops at +30 s: calm would relax K
    k = h.rt.brain.ladders["K"]
    level, factor = k.level, k.factor
    h.rt.brain.operator(OperatorCmd(tenant="K", decision="extend", until_s=40, by="ravi"), h.sim.now)
    asyncio.run(h.run_until(T0 + 69))
    assert (k.level, k.factor) == (level, factor)          # held, no AIMD recovery
    assert h.snapshots[-1].limits[0].factor == factor      # and still published
    asyncio.run(h.run_until(T0 + 100))
    assert any("ended" in e.detail for e in h.rt.brain.book.log) and k.factor > factor


def test_resume_cancels_the_hold(cfg):
    h = throttled_k(cfg)
    brain = h.rt.brain
    brain.operator(OperatorCmd(tenant="K", decision="release", until_s=600, by="asha"), h.sim.now)
    ev = brain.operator(OperatorCmd(tenant="K", decision="resume", by="asha"), h.sim.now)
    assert "K" not in brain.holds and "back in charge" in ev[0].detail
    asyncio.run(h.run_until(T0 + 45))
    assert brain.ladders["K"].acting                        # the attack continues: re-escalated


def test_operator_endpoint_validation(cfg):
    h = throttled_k(cfg)
    client = TestClient(create_app(cfg, start_loops=False, runtime=h.rt))
    brain = h.rt.brain
    body = {"tenant": "K", "decision": "release", "until_s": 60, "by": "asha"}
    with client:
        assert client.post("/api/operator", json=body).status_code == 401
        assert client.post("/api/operator", json=body, headers={H_CONTROL_TOKEN: "nope"}).status_code == 401
        for bad in (0, -5, cfg.ttl_max_s + 1):
            r = client.post("/api/operator", json={**body, "until_s": bad}, headers=TOK)
            assert r.status_code == 400 and r.json()["ok"] is False
        assert client.post("/api/operator", json={**body, "tenant": "ZZ"}, headers=TOK).status_code == 404
        assert client.post("/api/operator", json={**body, "decision": "nuke"}, headers=TOK).status_code == 422
        r = client.post("/api/operator", json=body, headers=TOK)
        assert r.status_code == 200 and r.json() == {"ok": True, "detail": "release K by asha for 60 s"}
        assert "K" in brain.holds and brain.ladders["K"].level == "normal"
        assert any("Operator hold by asha" in e["detail"] for e in client.get("/api/incidents?tenant=K").json())
