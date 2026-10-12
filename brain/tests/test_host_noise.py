"""Host calibration (T12 host run 1b failed: 78 server incidents + 19 ladder moves in 270 s of calm).

The simulator's noise="host" mode reproduces what 0.5-CPU containers do at calm on the Docker host:
1-3 s CPU-quota stalls of a whole server (~every 30 s per server, x3-8) and 5% of cells with a tail spike.
A quiet run must open nothing and move nothing; real trouble must still be found and proved.
"""
import collections

import pytest

from tenantguard_brain.core import Brain
from tenantguard_brain.prover import DockerProver
from tenantguard_brain.sim import Phase, SimDocker, Simulator, whale_scenario

from .conftest import T0, drive, drive_jobs


def counts(events):
    return collections.Counter((e.incident_id[:3], e.stage) for e in events)


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("calm_scale", [1.0, 1.6])          # ~25% and ~36% fleet CPU (the host's calm)
def test_noisy_calm_opens_nothing_and_moves_nothing(cfg, seed, calm_scale):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, [Phase("calm", 600)], start=T0, noise="host", seed=seed, calm_scale=calm_scale)
    events, limits = drive(brain, sim, T0 + 600)
    c = counts(events)
    assert c[("srv", "detected")] == 0, [e.detail for e in events if e.stage == "detected"][:3]
    assert c[("inc", "level_change")] == 0, [e.detail for e in events if e.stage == "level_change"][:3]
    assert limits == [] and sim.route_log == []
    assert brain.snapshot(sim.now).impact.innocents_penalised == []


def test_whale_is_still_found_and_proved_under_host_noise(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 60, 40, 30), quota_mult={"K": 3}, start=T0, noise="host",
                    seed=2, calm_scale=1.6)
    brain.s3.prover = DockerProver(SimDocker(sim), cfg.prover, cfg.verdicts, container="codeastra-spare-1")
    events, _ = drive_jobs(brain, sim, T0 + sim.total_s, prover=brain.s3.prover)
    named = [e for e in events if e.stage == "named" and e.suspects]
    assert named and named[0].suspects[0].tenant == "K" and named[0].ts - (T0 + 60) < 12
    verdicts = [e for e in events if e.stage == "verdict" and e.tenant]
    # paid work, but at this load K's calm + whale also exceeds its plan: either verdict is right; never a blame
    # on anyone else
    assert verdicts and verdicts[0].tenant == "K" and verdicts[0].verdict in ("honest_growth", "over_plan")
    assert {s.tenant for e in events for s in e.suspects} == {"K"}
    assert {e.tenant for e in events if e.tenant} == {"K"}
    assert brain.snapshot(sim.now).impact.innocents_penalised == []


def test_host_fault_is_still_found_under_host_noise(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, [Phase("calm", 200)], start=T0, noise="host", seed=3, calm_scale=1.6)
    drive(brain, sim, T0 + 60)
    sim.set_fault("w4", 40.0)
    events, limits = drive_jobs(brain, sim, T0 + 100)
    v = [e for e in events if e.stage == "verdict"]
    assert v and v[0].verdict == "host_fault" and "w4" in v[0].detail
    assert [e for e in events if e.stage == "detected"][0].ts - (T0 + 60) < 12
    assert limits == []


def test_a_fast_server_is_never_slow_whatever_the_ratio():
    """Host calm3: 'w4 4.8x its peers (17 ms vs 4 ms)' opened an incident. No SLA is at risk at 17 ms."""
    from tenantguard_common.config import load_tenants
    from tenantguard_brain.health import HealthParams, HealthTracker
    tenants = load_tenants()
    barcodes = {t.id: list(t.servers) for t in tenants}
    sla = {t.id: t.sla_p95_ms for t in tenants}
    servers = sorted({s for v in barcodes.values() for s in v})

    def run(level_w4, base=4.0, secs=20):
        h = HealthTracker(servers, HealthParams(), barcodes, sla)
        t = 0.0
        for _ in range(int(secs * 4)):
            t += 0.25
            h.observe([(tt, s, (level_w4 if s == "w4" else base), 3) for tt, srv in barcodes.items() for s in srv], t)
        return h
    h = run(17.0)
    assert h.servers["w4"].peer_ratio > 4 and h.slow_servers() == []          # 4.25x but only 17 ms: not slow
    assert h.floor_ms["w4"] >= 25.0
    h = run(60.0, base=10.0)
    assert h.slow_servers() == ["w4"]                                        # 6x and 60 ms: slow


def test_two_slow_neighbours_do_not_flag_an_over_user():
    """Host calm3: G at 1.3x its share with '2 of 9 neighbours slower' went to warn."""
    from tenantguard_brain.score import compute_scores
    w = {"G": 1.0, "A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0}
    servers = {"G": ["s0"], "A": ["s0"], "B": ["s0"], "C": ["s0"], "D": ["s0"]}
    nb = {t: [o for o in w if o != t] for t in w}
    common = dict(weights=w, servers_of=servers, active=list(w), cpu_by_server={"s0": {"G": 400.0, "A": 25.0}},
                  cpu_cap={"s0": 1000.0}, occupancy={}, slots={"s0": 8.0}, neighbours=nb,
                  min_victim_impact=0.2, quarantine_threshold=4.0, over_use=1.25, min_hurt=3, hurt_level=0.1)
    two = compute_scores(slowdowns={"A": 1.5, "B": 1.5, "C": 0.0, "D": 0.0}, **common)
    assert two["G"].usage_ratio >= 1.25 and two["G"].victim_impact >= 0.2 and not two["G"].flagged
    three = compute_scores(slowdowns={"A": 1.5, "B": 1.5, "C": 1.0, "D": 0.0}, **common)
    assert three["G"].flagged


def test_a_server_a_hot_tenant_runs_on_is_not_drained_as_a_faulty_host(cfg):
    """Host run step 2b: while K ramped, the decoder named 'host suspect w1' and drained it twice."""
    from tenantguard_brain.sim import HEAVY
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 20, rps=6), quota_mult={"K": 3}, start=T0)
    sim.apply_relocate("K", HEAVY, "w1", 600)          # K's heavy work lands on w1 only: w1 slow, w3/w8 fine
    events, _ = drive_jobs(brain, sim, T0 + sim.total_s)
    drains = [c for _, p, c in sim.control_log if p.endswith("/drain")]
    assert drains == []
    assert not any("Host suspect w1" in e.detail for e in events if e.stage == "named")
    held = [e for e in events if e.stage == "named" and "w1 is not treated as a faulty host" in e.detail]
    assert len(held) == 1 and "while K runs hot there" in held[0].detail
    assert held[0].report["held_hosts"] == {"w1": ["K"]} and held[0].suspects == []


def test_a_real_host_fault_with_no_hot_tenant_is_still_drained(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, [Phase("calm", 120)], start=T0)
    drive(brain, sim, T0 + 20)
    sim.set_fault("w4", 40.0)
    events, _ = drive_jobs(brain, sim, T0 + 50)
    assert [e.verdict for e in events if e.stage == "verdict"] == ["host_fault"]


def test_a_deferred_drain_is_said_once(cfg):
    """Host run step 2b: 62 'Drain test for w1 deferred: peers are too busy (95% >= 75%)' events, one per second."""
    brain = Brain(cfg)
    brain.s3.budget.drain_peer_busy = 0.0               # peers always "too busy"
    sim = Simulator(cfg.tenants, [Phase("calm", 120)], start=T0)
    drive(brain, sim, T0 + 20)
    sim.set_fault("w4", 40.0)
    events, _ = drive(brain, sim, T0 + 60)
    deferred = [e for e in events if e.stage == "proving" and "deferred" in e.detail]
    assert len(deferred) == 1 and "w4" in deferred[0].detail


def test_a_server_below_its_floor_enters_the_decoder_as_normal(cfg):
    """Host step 3: during the w4 incident, 'Host suspect w0 3.8x (24 ms vs 6 ms)' was named: under the floor."""
    brain = Brain(cfg)
    h = brain.s3.health
    h.servers["w0"].level_ms, h.servers["w0"].peer_ratio = 24.0, 3.8      # 3.8x but 24 ms (< 25 ms floor)
    h.servers["w4"].level_ms, h.servers["w4"].peer_ratio = 60.0, 6.0      # really slow
    r = brain.s3.decoder_ratios()
    assert r["w0"] == 1.0 and r["w4"] == 6.0
    assert "spare" not in r and "quarantine" not in r                      # pool servers are never decoded
