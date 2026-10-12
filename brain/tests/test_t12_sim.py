"""Prove -> verdict -> action, end to end on the simulator with the DockerProver on a fake Docker."""
from tenantguard_brain.core import Brain
from tenantguard_brain.prover import DockerProver
from tenantguard_brain.sim import HEAVY, SimDocker, Simulator, whale_scenario

from .conftest import T0, drive_jobs


def make(cfg, *, qm=3.0, **scenario):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", **scenario), quota_mult={"K": qm}, start=T0)
    docker = SimDocker(sim)
    brain.s3.prover = DockerProver(docker, cfg.prover, cfg.verdicts, container="codeastra-spare-1")
    return brain, sim, docker


def run(brain, sim, until=None):
    return drive_jobs(brain, sim, until or T0 + sim.total_s, prover=brain.s3.prover)


def verdicts(events):
    return [e for e in events if e.stage == "verdict"]


def relocations(sim):
    return [(c.tenant, c.cls, c.target) for _, p, c in sim.control_log if p.endswith("/relocate")]


def test_honest_growth_stays_on_the_spare_as_a_burst_lane(cfg):
    brain, sim, docker = make(cfg, calm_s=20, attack_s=40, after_s=30)
    events, _ = run(brain, sim)
    v = verdicts(events)
    assert len(v) == 1 and v[0].verdict == "honest_growth" and v[0].tenant == "K"
    assert "proved by relocation" in v[0].detail and "burst lane" in v[0].action
    proving = [e for e in events if e.stage == "proving"]
    assert proving[0].tenant == "K" and proving[0].ts < v[0].ts
    assert relocations(sim) == [("K", None, "spare")]                  # it stays there (TTL)
    assert docker.calls and docker.calls[0][1] > 1.0                    # spare sized to 1.25x its footprint
    assert len(docker.calls) == 1                                       # and NOT restored: it is the burst lane
    assert not [e for e in events if e.incident_id.startswith("inc-") and e.action and "factor=0" in e.action]


def test_costly_pattern_sends_the_guilty_class_to_the_heavy_lane(cfg):
    brain, sim, docker = make(cfg, calm_s=20, attack_s=40, after_s=30, blackbox=True)
    events, _ = run(brain, sim)
    v = verdicts(events)
    assert v[0].verdict == "costly_pattern"
    assert "black-box" in v[0].detail and "paid) CPU explains only" in v[0].detail
    r = relocations(sim)
    assert ("K", None, "spare") in r                                    # the test ...
    assert ("K", HEAVY, "quarantine") in r                              # ... the guilty class to the heavy lane
    assert r[-1] == ("K", None, None)                                   # ... the rest goes home
    assert docker.calls[-1][1] == 1.0                                   # spare restored to its size before


def test_culprit_pausing_mid_test_is_mitigated_no_blame(cfg):
    # detected (~7 s), then it stops during the proof's settle time: its servers recover, the spare stays calm
    brain, sim, docker = make(cfg, calm_s=20, attack_s=8, after_s=40)
    events, _ = run(brain, sim)
    v = verdicts(events)
    assert [x.verdict for x in v] == ["mitigated_no_blame"]
    assert "harm did not follow" in v[0].detail
    assert relocations(sim) == [("K", None, "spare"), ("K", None, None)]    # moved back home
    assert docker.calls[-1][1] == 1.0                                       # spare restored


def test_over_plan_tightens_limits_and_sends_it_home(cfg, monkeypatch):
    brain, sim, docker = make(cfg, calm_s=20, attack_s=40, after_s=30)
    real = brain.verdict_inputs

    def refused(t, now):                       # admission is already refusing most of K's requests
        out = real(t, now)
        if t == "K":
            out["rejected_share"] = 0.6
        return out
    monkeypatch.setattr(brain, "verdict_inputs", refused)
    events, cmds = run(brain, sim, T0 + 40)
    v = verdicts(events)
    assert v[0].verdict == "over_plan" and "refuses 60%" in v[0].detail
    assert relocations(sim)[-1] == ("K", None, None)
    k_limits = [c for _, c in cmds if c.tenant == "K"]
    assert k_limits and k_limits[0].factor == cfg.ladder.throttle_factor and k_limits[0].weight_factor == 1.0
    assert brain.ladders["K"].level == "throttle"


def test_below_the_confidence_bar_is_not_proved(cfg, monkeypatch):
    cfg.verdicts.confidence_to_prove = 1.01               # nobody can reach it
    brain, sim, docker = make(cfg, calm_s=20, attack_s=40, after_s=10)
    events, _ = run(brain, sim, T0 + 40)
    v = verdicts(events)
    assert v and v[0].verdict == "mitigated_no_blame" and "below" in v[0].detail
    assert relocations(sim) == [] and docker.calls == []


def test_repeat_offender_gets_the_last_verdict_without_reproving(cfg):
    brain, sim, docker = make(cfg, calm_s=20, attack_s=40, after_s=10)
    for _ in range(cfg.verdicts.repeat_count):
        brain.s3.memory.note_named("K", T0)
    brain.s3.memory.note_verdict("K", "costly_pattern")
    events, _ = run(brain, sim, T0 + 40)
    v = verdicts(events)
    assert v[0].verdict == "costly_pattern" and "repeat offender" in v[0].detail
    assert not [e for e in events if e.stage == "proving"] and docker.calls == []
    assert ("K", HEAVY, "quarantine") in relocations(sim) or ("K", None, "quarantine") in relocations(sim)


def test_relocation_budget_defers_the_proof(cfg):
    brain, sim, docker = make(cfg, calm_s=20, attack_s=40, after_s=10)
    brain.s3.budget.relocations_per_min = 0
    events, _ = run(brain, sim, T0 + 40)
    notes = [e for e in events if e.stage == "proving" and "deferred" in e.detail]
    assert len(notes) == 1 and "budget" in notes[0].detail             # said once, not every tick
    assert relocations(sim) == [] and not verdicts(events)
