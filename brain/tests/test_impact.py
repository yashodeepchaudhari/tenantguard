"""ImpactView (business impact) and the recovery wording of the status sentence."""
import pytest

from tenantguard_brain.core import Brain
from tenantguard_brain.impact import MONTH_S, ImpactMeter
from tenantguard_brain.sim import Simulator, whale_scenario

from .conftest import T0, drive


def test_meter_math():
    m = ImpactMeter(T0, {"A": 100.0, "B": 50.0}, {"A": 2_592_000.0, "B": 0.0})
    m.tick(1.0, ["A", "B"], {"A": 80.0, "B": 70.0})       # A within SLO (avoided), B over (suffered)
    m.tick(1.0, ["A"], {"A": None})                        # no data: no evidence either way
    v = m.view()
    assert v.slo_violation_avoided_s == 1.0 and v.slo_violation_suffered_s == 1.0
    assert v.revenue_protected_rs == pytest.approx(2_592_000.0 * 1.0 / MONTH_S)
    assert v.customers_shielded == ["A"] and v.innocents_penalised == []
    m.note_action("C", score=0.9, warn=1.5)
    m.note_action("K", score=3.0, warn=1.5)
    assert m.view().innocents_penalised == ["C"]


def test_whale_impact_no_innocents_and_victims_shielded(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 30), quota_mult={"K": 3}, start=T0)
    drive(brain, sim, T0 + sim.total_s)
    imp = brain.snapshot(sim.now).impact
    assert imp is not None and imp.since <= T0 + 1
    assert imp.innocents_penalised == []
    assert imp.slo_violation_avoided_s > 0 and imp.revenue_protected_rs > 0
    assert imp.customers_shielded and "K" not in imp.customers_shielded
    neighbours = set(brain.neighbours["K"])
    assert set(imp.customers_shielded) <= neighbours


def test_ladder_escalation_on_a_low_score_is_an_innocent(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 30), quota_mult={"K": 3}, start=T0)
    drive(brain, sim, T0 + 21, routes_apply=False)
    brain.scores["K"].score = 0.5                         # pretend K was not noisy when it got tightened
    from tenantguard_brain import ladder as lad
    tr = lad.Transition("escalate", "warn", "throttle", 1.0, 0.5, 1.0, 1.0, "test")
    brain._on_transition("K", tr, sim.now)
    assert brain.snapshot(sim.now).impact.innocents_penalised == ["K"]


def test_over_plan_verdict_below_warn_is_not_an_innocent(cfg):
    """A proven over_plan tenant is throttled by the verdict (force_throttle) even when its noisy score is
    below warn: that is a verdict, not an innocent penalised. Neither is the AIMD step that renews it."""
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 30), quota_mult={"K": 3}, start=T0)
    drive(brain, sim, T0 + 21, routes_apply=False)
    brain.scores["K"].score = 0.5                         # proven over plan, but not noisy right now
    assert brain.force_throttle("K", sim.now, "verdict over_plan for K")
    from tenantguard_brain import ladder as lad
    st = brain.ladders["K"]
    tr = lad.Transition("step", st.level, st.level, st.factor, st.factor * 0.5, 1.0, 1.0, "test")
    brain._on_transition("K", tr, sim.now)
    assert brain.snapshot(sim.now).impact.innocents_penalised == []


def test_status_sentence_says_recovering_not_zero_share(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 60), quota_mult={"K": 3}, start=T0)
    seen = []
    t = T0
    while t < T0 + 120:
        t += 1.0
        drive(brain, sim, t, routes_apply=False)
        st = brain.ladders["K"]
        if st.level != "normal":
            seen.append(brain.snapshot(sim.now).status_sentence)
    assert any("recovering" in s or "recovered" in s for s in seen)
    assert not any("0.0x its fair share" in s for s in seen)
