import pytest

from tenantguard_brain.ladder import LadderParams, LadderState, Signal, reset, step

P = LadderParams()          # defaults == tenantguard.yaml brain.noisy
TICK = 0.25
HURT = Signal(score=6.0, flagged=True, unhurt=False)
HUGE = Signal(score=9.0, flagged=True, unhurt=False)
CALM = Signal(score=0.4, flagged=False, unhurt=True)
BAND = Signal(score=1.3, flagged=False, unhurt=False)     # between recover_below and warn, mildly hurt


def run(st, sig, t0, secs):
    """Step every 250 ms for `secs`; return (end time, transitions)."""
    out = []
    t = t0
    for _ in range(int(round(secs / TICK))):
        t += TICK
        tr = step(st, sig, t, P)
        if tr:
            out.append((t, tr))
    return t, out


def test_params_from_yaml():
    p = LadderParams.from_yaml({"warn": 1.5, "throttle": 2.0, "quarantine": 4.0, "recover_below": 1.0,
                                "hold_s": {"warn": 3}, "throttle_factor": 0.5, "recover_step": 0.2},
                               {"throttle": 30, "quarantine": 90})
    assert p.hold["warn"] == 3 and p.hold["recover"] == 10
    assert p.recover_step == 0.2 and p.ttl_for("throttle") == 30 and p.ttl_for("quarantine") == 90


def test_no_escalation_before_hold():
    st = LadderState()
    t, trs = run(st, HURT, 0, P.hold["warn"])        # first seen at 0.25 s: held only 1.75 s at 2.0 s
    assert trs == [] and st.level == "normal"
    t, trs = run(st, HURT, t, TICK)
    assert st.level == "warn" and trs[0][1].kind == "escalate"


def test_half_duty_flapping_never_escalates():
    st = LadderState()
    t = 0.0
    for _ in range(20):
        t, _ = run(st, HURT, t, 0.5)
        t, _ = run(st, CALM, t, 0.5)
    assert st.level == "normal"


def test_one_dip_does_not_restart_the_hold():
    st = LadderState()
    t, _ = run(st, HURT, 0, 1.0)
    t, _ = run(st, CALM, t, 0.25)            # one noisy tick below the threshold
    t, trs = run(st, HURT, t, 1.0)
    assert st.level == "warn"                # still escalated at 2.25 s, as without the dip
    assert trs[0][0] == pytest.approx(2.25)


def test_strict_hold_ratio_restarts_on_a_dip():
    p = LadderParams(hold_ratio=1.0)
    st = LadderState()
    t = 0.0
    for sig, secs in ((HURT, 1.0), (CALM, 0.25), (HURT, 1.0)):
        for _ in range(int(secs / TICK)):
            t += TICK
            step(st, sig, t, p)
    assert st.level == "normal"


def test_full_walk_up_the_ladder():
    st = LadderState()
    t, trs = run(st, HUGE, 0, 60)
    walk = [(tr.level, tr.factor, tr.weight_factor) for _, tr in trs]
    assert walk == [
        ("warn", 1.0, 1.0),
        ("throttle", 0.5, 1.0),
        ("throttle", 0.25, 1.0),          # AIMD multiplicative decrease, down to the floor
        ("deprioritize", 0.25, 0.25),
        ("quarantine", 0.1, 0.1),
    ]
    times = [tt for tt, _ in trs]
    assert times == pytest.approx([2.25, 4.25, 6.25, 10.25, 16.25])   # first seen at 0.25; hold_s 2, 2, 2, 4, 6
    assert st.acting and st.level == "quarantine"


def test_quarantine_needs_the_quarantine_threshold():
    st = LadderState()
    run(st, HURT, 0, 60)                 # score 6 >= quarantine 4
    assert st.level == "quarantine"
    st = LadderState()
    run(st, Signal(3.0, True, False), 0, 60)
    assert st.level == "deprioritize"    # stays at deprioritize while 2 <= score < 4


def test_ladder_recovers_after_the_attack_stops():
    st = LadderState()
    t, _ = run(st, HUGE, 0, 30)
    assert st.level == "quarantine"
    # nothing until calm fills hold_ratio of the hold_s.recover window (80% of 10 s = 8 s)
    t, trs = run(st, CALM, t, P.hold_ratio * P.hold["recover"] - TICK)
    assert trs == []
    t, trs = run(st, CALM, t, 30)
    kinds = [tr.kind for _, tr in trs]
    factors = [tr.factor for _, tr in trs if tr.kind in ("recover", "clear")]
    assert factors == pytest.approx([0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])   # +0.1 per recover tick
    gaps = [b[0] - a[0] for a, b in zip(trs, trs[1:]) if b[1].kind in ("recover", "clear")]
    assert all(g >= P.recover_tick_s - 1e-9 for g in gaps)
    assert "clear" in kinds and kinds[-1] == "resolve"
    assert st.level == "normal" and not st.acting
    assert [tr.level for _, tr in trs][:2] == ["deprioritize", "deprioritize"]


def test_hysteresis_band_holds_still():
    st = LadderState()
    t, _ = run(st, HURT, 0, 4.5)
    assert st.level == "throttle" and st.factor == 0.5
    t, trs = run(st, BAND, t, 60)
    assert trs == [] and st.factor == 0.5


def test_aimd_probe_then_decrease_again():
    st = LadderState()
    t, _ = run(st, HURT, 0, 6.5)
    assert st.factor == 0.25
    # contained: neighbours unhurt -> additive increase after the recover hold
    t, trs = run(st, Signal(3.0, False, True), t, 10.5)
    assert st.factor == pytest.approx(0.35)
    # hurting again -> multiplicative decrease after hold_s.throttle
    t, trs = run(st, HURT, t, 2.25)
    assert st.factor == 0.25 and trs[-1][1].kind == "step"


def test_legit_burst_never_leaves_normal():
    st = LadderState()
    t, trs = run(st, Signal(score=12.0, flagged=False, unhurt=True), 0, 300)
    assert trs == [] and st.level == "normal" and not st.acting


def test_plain_warning_clears_without_action():
    st = LadderState()
    t, _ = run(st, Signal(1.7, True, False), 0, 3)
    assert st.level == "warn" and not st.acting
    t, trs = run(st, CALM, t, P.hold["recover"] + 1)
    assert st.level == "normal" and trs[-1][1].kind == "resolve"


def test_reset():
    st = LadderState()
    run(st, HUGE, 0, 8.5)
    tr = reset(st, 100, "mode changed")
    assert tr.level == "normal" and tr.prev_level == "throttle" and st.factor == 1.0
    assert reset(st, 101, "again") is None
