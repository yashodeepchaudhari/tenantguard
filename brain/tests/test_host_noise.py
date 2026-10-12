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
