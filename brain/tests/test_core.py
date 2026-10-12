"""End-to-end brain logic on the simulator, with a virtual clock (no I/O)."""
from tenantguard_common.contracts import QueueStat, StateSnapshot, TelemetryFrame, TenantUsage, WorkerStats

from tenantguard_brain.core import Brain
from tenantguard_brain.sim import Phase, Simulator, whale_scenario

from .conftest import T0, drive


def whale_run(cfg, mode="tenantguard", **kw):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 60), quota_mult={"K": 3}, start=T0, mode=mode)
    log = []
    events, commands = drive(brain, sim, T0 + sim.total_s, log=log, **kw)
    return brain, sim, events, commands, log


def test_quiet_fleet_raises_no_incident(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, [Phase("calm", 120)], start=T0)
    events, commands = drive(brain, sim, T0 + 120)
    assert events == [] and commands == []
    snap = brain.snapshot(sim.now)
    assert snap.status_sentence.startswith("All 12 tenants healthy")
    assert snap.fairness > 0.5 and 0.1 < snap.utilization < 0.6


def test_whale_is_detected_throttled_and_recovers(cfg):
    brain, sim, events, commands, log = whale_run(cfg)
    # only K is ever touched: no innocent tenant gets an incident or a command
    assert {e.tenant for e in events} == {"K"}
    assert {c.tenant for _, c in commands} == {"K"}
    # detected during the attack (starts at +20 s), within a few seconds
    detected = [e for e in events if e.stage == "detected"]
    assert len(detected) == 1 and 20 < detected[0].ts - T0 < 26
    # the ladder walk: warn -> throttle (0.5) -> throttle (0.25)
    levels = [(e.prev_level, e.level) for e in events if e.stage == "level_change"]
    assert levels[:3] == [("normal", "warn"), ("warn", "throttle"), ("throttle", "throttle")]
    factors = [c.factor for _, c in commands]
    assert factors[:2] == [0.5, 0.25]
    # recovery after the attack stops (+60 s): back to full plan and incident closed
    assert brain.ladders["K"].level == "normal" and brain.ladders["K"].factor == 1.0
    stages = [e.stage for e in events]
    assert stages[-3:] == ["resolved", "level_change", "closed"]
    assert commands[-1][1].factor == 1.0 and commands[-1][1].weight_factor == 1.0
    assert sim.overrides == {}
    # contained before the attack ended: K is not escalating forever
    k_at_end_of_attack = [lv["K"] for t, lv in log if t - T0 <= 60][-1]
    assert k_at_end_of_attack[0] == "throttle"


def test_every_command_carries_both_factors_and_a_ttl(cfg):
    _, _, _, commands, _ = whale_run(cfg)
    for _, c in commands:
        d = c.model_dump()
        assert "factor" in d and "weight_factor" in d and c.ttl_s > 0
        assert 0 < c.factor <= 1 and 0 < c.weight_factor <= 1
    assert all(c.incident_id for _, c in commands)


def test_every_event_explains_why(cfg):
    _, _, events, _, _ = whale_run(cfg)
    for e in events:
        assert e.tenant == "K" and e.detail
        if e.stage == "level_change":
            assert e.level and e.prev_level
            assert "because" in e.detail
        if e.action and e.action.startswith("LimitsCmd factor=0"):
            assert "fair share" in e.detail and "neighbours" in e.detail


def test_actions_are_renewed_while_needed(cfg):
    brain, _, _, commands, _ = whale_run(cfg)
    # K stays throttled ~35 s with TTL 60 s: at least one renewal of an unchanged factor (every ttl/4)
    same = [(a, b) for (_, a), (_, b) in zip(commands, commands[1:]) if a.factor == b.factor]
    assert same


def test_failed_sends_are_retried(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 15, 0), quota_mult={"K": 3}, start=T0)
    _, commands = drive(brain, sim, T0 + sim.total_s, apply=False, sent_ok=False)
    times = [t for t, c in commands if c.factor == 0.5]
    assert len(times) >= 2 and all(b - a >= 1.0 - 1e-6 for a, b in zip(times, times[1:]))


def test_no_actions_outside_tenantguard_mode(cfg):
    brain, sim, events, commands, _ = whale_run(cfg, mode="industry")
    assert events == [] and commands == []
    assert brain.scores                                     # still scoring and observing
    assert "industry" in brain.snapshot(sim.now).status_sentence


def test_leaving_tenantguard_mode_resets_and_clears(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 30), quota_mult={"K": 3}, start=T0)
    drive(brain, sim, T0 + 30)
    assert brain.ladders["K"].acting
    sim.mode = "off"
    events, commands = drive(brain, sim, T0 + 32)
    assert brain.ladders["K"].level == "normal"
    assert any(e.stage == "closed" and "mode" in e.detail for e in events)
    assert commands and commands[0][1].factor == 1.0 and commands[0][1].weight_factor == 1.0


def test_ticks_without_completions_are_skipped(cfg):
    brain = Brain(cfg)
    now = T0
    def frame(p95, completed):
        return TelemetryFrame(ts=now, mode="tenantguard", queues=[
            QueueStat(tenant="A", depth=0, wait_p95_ms=0, p95_ms=p95, completed=completed, admitted=completed,
                      rejected_429=0, expired_503=0, inflight=0)])
    for _ in range(20):
        now += 0.25
        brain.ingest_frame(frame(10.0, 5), now)
    base = brain.baseline.get("A")
    for _ in range(20):
        now += 0.25
        brain.ingest_frame(frame(0.0, 0), now)               # idle ticks: p95 0 must not read as "fast"
    assert brain.baseline.get("A") == base
    brain.tick(now)
    assert brain.snapshot(now).tenants[0].p95_ms == 0.0      # no completions in the window -> no p95


def test_worker_restart_does_not_produce_negative_cpu(cfg):
    brain = Brain(cfg)
    def ws(ts, started, cpu):
        return WorkerStats(server="w1", ts=ts, started_at=started, cpu_total_ms=cpu, mem_mb=1,
                           tenants=[TenantUsage(tenant="K", requests=1, handler_cpu_ms=cpu, bg_cpu_ms=0, db_ms=0)])
    brain.ingest_stats(ws(T0, T0 - 100, 5000), T0)
    brain.ingest_stats(ws(T0 + 1, T0 - 100, 5300), T0 + 1)
    brain.ingest_stats(ws(T0 + 2, T0 + 1.5, 20), T0 + 2)     # restarted: counters reset
    brain.ingest_stats(ws(T0 + 3, T0 + 1.5, 120), T0 + 3)
    assert [round(s.tenant_cpu_ms["K"]) for s in brain.stat_samples] == [300, 100]


def test_snapshot_matches_contract(cfg):
    brain, sim, _, _, _ = whale_run(cfg)
    snap = brain.snapshot(sim.now)
    StateSnapshot.model_validate(snap.model_dump())
    assert len(snap.tenants) == 12 and {s.server for s in snap.servers} >= set(cfg.fleet_servers)
    assert abs(sum(t.cpu_share for t in snap.tenants) - 1.0) < 0.01
    assert all(0 <= t.noisy_norm <= 1 for t in snap.tenants)
    assert snap.brain_ok


def test_stale_telemetry_holds_and_says_so(cfg):
    brain, sim, _, _, _ = whale_run(cfg)
    snap = brain.snapshot(sim.now + 30)
    assert not snap.brain_ok and "lost" in snap.status_sentence
