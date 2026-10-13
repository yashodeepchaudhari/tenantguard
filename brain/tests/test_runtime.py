"""Runtime pieces without a network: snapshot push, start-up adoption, registry seeding, the audit log."""
import asyncio
import json

import httpx

from tenantguard_common.contracts import H_CONTROL_TOKEN, LimitsCmd, OverridesView, ReplicaView

from tenantguard_brain.runtime import Runtime, load_registry
from tenantguard_brain.sim import Phase

from .conftest import T0, Harness, SimBackend


def test_snapshot_push_carries_the_token_and_refusals_are_reported(cfg):
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.path, req.headers.get(H_CONTROL_TOKEN)))
        return httpx.Response(400 if len(seen) > 1 else 200, json={"ok": len(seen) == 1})

    rt = Runtime(cfg, backend=SimBackend(None))
    rt.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(rt.push_snapshot())
    assert rt.gateway_ok and rt.snapshots_sent == 1
    asyncio.run(rt.push_snapshot())
    assert rt.gateway_ok is False and rt.snapshots_sent == 1
    assert seen == [("/control/snapshot", cfg.control_token)] * 2
    assert rt.last_snapshot.version == 2


def test_registry_is_seeded_once_then_loaded(cfg):
    reg = load_registry(cfg)
    assert cfg.control.registry_path.exists() and len(reg.tenants) == 12
    cfg.seed_tenants = cfg.seed_tenants[:1]                   # a later start ignores the seed
    assert len(load_registry(cfg).tenants) == 12


def test_adopt_leftover_overrides(cfg):
    rt = Runtime(cfg, backend=SimBackend(None))
    brain = rt.brain
    ov = OverridesView(limits={
        "K": LimitsCmd(tenant="K", factor=0.25, weight_factor=0.25, incident_id="inc-old"),
        "ZZ": LimitsCmd(tenant="ZZ", factor=0.5),                     # not a tenant we know: left to expire
    })
    events = brain.adopt_overrides(ov, T0)
    k = brain.ladders["K"]
    assert k.level == "deprioritize" and k.factor == 0.25 and k.weight_factor == 0.25
    assert events and events[0].tenant == "K" and "Adopted" in events[0].detail
    assert [c.tenant for c in brain.limits()] == ["K"]               # published from the next snapshot on
    brain.ladders["A"].factor, brain.ladders["A"].level = 0.5, "throttle"
    brain.adopt_overrides(OverridesView(limits={"A": LimitsCmd(tenant="A", factor=0.1, weight_factor=0.1)}), T0)
    assert brain.ladders["A"].factor == 0.5                          # a tenant this run manages is not overwritten


def test_adopted_override_recovers_when_calm(cfg):
    h = Harness(cfg, [Phase("calm", 60)])
    h.rt.brain.adopt_overrides(OverridesView(limits={"K": LimitsCmd(tenant="K", factor=0.5)}), T0)
    asyncio.run(h.run_until(T0 + 60))
    assert h.rt.brain.ladders["K"].level == "normal" and h.snapshots[-1].limits == []


def test_replicas_found_at_start_must_pass_a_health_check(cfg):
    class Found(SimBackend):
        async def list_replicas(self):
            return [ReplicaView(id="tg-media-7", project="media", url="http://tg-media-7", state="ready",
                                tenants=["H", "L"], mem_mb=160)]

    rt = Runtime(cfg, backend=Found(None))
    rt.control.adopt(asyncio.run(rt.backend.list_replicas()), T0)
    assert rt.control.fleet.replicas["tg-media-7"].state == "starting"


def test_incidents_and_replica_actions_are_audited(cfg):
    h = Harness(cfg, [Phase("calm", 5)])
    asyncio.run(h.run_until(T0 + 3))
    rows = [json.loads(x) for x in cfg.control.events_path.read_text(encoding="utf-8").splitlines()]
    assert {r["kind"] for r in rows} == {"replica"}
    assert {(r["action"], r["ok"]) for r in rows} == {("start", True)} and len(rows) == 5
