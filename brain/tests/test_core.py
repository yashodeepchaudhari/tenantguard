"""End-to-end brain logic (ladder + control loop + runtime wiring) on the simulator, with a virtual clock."""
import asyncio

from tenantguard_common.contracts import StateSnapshot, TenantUsage, WorkerStats

from tenantguard_brain.sim import Phase, whale_scenario

from .conftest import T0, Harness


def run(h: Harness, until: float, log=None) -> Harness:
    asyncio.run(h.run_until(until, log))
    return h


def whale(cfg, mode="tenantguard"):
    h = Harness(cfg, whale_scenario("K", 20, 40, 60), mode=mode)
    log = []
    run(h, T0 + h.sim.total_s, log)
    return h, log


def test_quiet_pool_raises_no_incident_and_keeps_one_warm_replica_each(cfg):
    h = run(Harness(cfg, [Phase("calm", 60)]), T0 + 60)
    assert h.events() == []
    assert all(s.limits == [] for s in h.snapshots)
    snap = h.rt.brain.snapshot(h.sim.now, h.rt.control.pool_view())
    assert snap.status_sentence.startswith("All 12 tenants healthy")
    assert {p: len(r) for p, r in h.sim.ready.items()} == {p: 1 for p in ("health", "commerce", "mobility", "media", "free")}
    assert snap.fairness > 0.5 and 0.1 < snap.utilization < 0.6


def test_whale_is_contained_inside_its_project_and_recovers(cfg):
    h, log = whale(cfg)
    events = h.events()
    assert {e.tenant for e in events} == {"K"}                            # no innocent gets an incident
    assert {c.tenant for s in h.snapshots for c in s.limits} == {"K"}     # ...or a limit
    detected = [e for e in events if e.stage == "detected"]
    assert len(detected) == 1 and 20 < detected[0].ts - T0 < 26
    levels = [(e.prev_level, e.level) for e in events if e.stage == "level_change"]
    assert levels[:3] == [("normal", "warn"), ("warn", "throttle"), ("throttle", "throttle")]
    assert [c.factor for s in h.snapshots for c in s.limits][:1] == [0.5]
    assert h.rt.brain.ladders["K"].level == "normal" and h.rt.brain.limits() == []
    assert [e.stage for e in events][-3:] == ["resolved", "level_change", "closed"]
    assert h.snapshots[-1].limits == []                                   # the clear reached the gateway


def test_other_projects_never_feel_the_whale(cfg):
    h, log = whale(cfg)
    brain = h.rt.brain
    others = [t for t, cfg_ in brain.tenants.items() if cfg_.project != "mobility"]
    assert all(lv[t][0] == "normal" for _, lv, _ in log for t in others)
    caps = {(r.project, r.nano_cpus) for s in h.snapshots for rs in s.replicas.values() for r in rs
            if r.project != "mobility" and r.state == "ready"}
    assert caps == {("health", 1_000_000_000), ("commerce", 1_000_000_000), ("media", 750_000_000),
                    ("free", 500_000_000)}                                # their floors never moved


def test_every_limit_carries_both_factors_and_a_ttl_and_every_event_explains_why(cfg):
    h, _ = whale(cfg)
    for s in h.snapshots:
        for c in s.limits:
            assert 0 < c.factor <= 1 and 0 < c.weight_factor <= 1 and c.ttl_s > 0 and c.incident_id
    for e in h.events():
        assert e.detail and len(e.detail) > 30
        if e.stage == "level_change" and e.level in ("throttle", "deprioritize", "quarantine"):
            assert "because" in e.detail and "LimitsCmd" in (e.action or "")


def test_no_actions_and_a_static_fleet_outside_tenantguard_mode(cfg):
    for mode in ("industry", "off"):
        h, _ = whale(cfg, mode=mode)
        assert all(s.limits == [] for s in h.snapshots)
        assert [e for e in h.events() if e.stage == "level_change"] == []
        assert {p: len(r) for p, r in h.sim.ready.items()}["mobility"] == 1   # no scaling, no lending


def test_industry_mode_caps_each_project_at_its_floor(cfg):
    h, _ = whale(cfg, mode="industry")
    mob = [r for r in h.snapshots[-1].replicas["mobility"] if r.state == "ready"]
    assert [r.nano_cpus for r in mob] == [750_000_000]


def test_stale_telemetry_holds_everything_and_says_so(cfg):
    h = run(Harness(cfg, whale_scenario("K", 20, 40, 60)), T0 + 30)
    brain, control = h.rt.brain, h.rt.control
    replicas = dict(control.fleet.replicas)
    level = brain.ladders["K"].level
    later = h.sim.now + 10
    asyncio.run(h.rt.control_once(later))                     # no frames for 10 s
    assert control.fleet.replicas.keys() == replicas.keys()
    assert brain.tick(later).events == [] and brain.ladders["K"].level == level
    assert "telemetry lost" in brain.snapshot(later).status_sentence
    assert brain.limits()                                     # the active limit keeps being published


def test_worker_restart_does_not_produce_negative_cpu(cfg):
    h = Harness(cfg, [Phase("calm", 5)])
    brain = h.rt.brain
    a = WorkerStats(server="tg-health-1", ts=T0, started_at=T0 - 100, cpu_total_ms=5000, mem_mb=80,
                    tenants=[TenantUsage(tenant="A", requests=10, handler_cpu_ms=900, bg_cpu_ms=0, db_ms=0)])
    b = a.model_copy(update={"ts": T0 + 1, "started_at": T0 + 0.5, "cpu_total_ms": 10,
                             "tenants": [TenantUsage(tenant="A", requests=1, handler_cpu_ms=3, bg_cpu_ms=0, db_ms=0)]})
    brain.ingest_stats(a, T0)
    brain.ingest_stats(b, T0 + 1)
    assert list(brain.stat_samples) == []                     # restart: no diff, never negative


def test_snapshot_matches_contract(cfg):
    h, _ = whale(cfg)
    snap = h.rt.brain.snapshot(h.sim.now, h.rt.control.pool_view())
    again = StateSnapshot.model_validate(snap.model_dump())
    assert len(again.tenants) == 12 and {t.project for t in again.tenants} == {
        "health", "commerce", "mobility", "media", "free"}
    assert again.pool is not None and len(again.pool.projects) == 5
    assert {s.project for s in again.servers} == {"health", "commerce", "mobility", "media", "free"}
