"""verdicts.py: the five verdicts, proof evaluation, action budget, repeat-offender memory (pure)."""
import pytest

from tenantguard_brain.verdicts import (COSTLY, HONEST, HOST, NO_BLAME, OVER, ActionBudget, Evidence, OffenderMemory,
                                        ProofMeasure, VerdictParams, classify, evaluate_proof, host_verdict)

P = VerdictParams()


def ev(**kw):
    base = dict(tenant="K", confidence=0.9, confirmed=True, R=0.8, harm_followed=True, paid_share=0.9)
    base.update(kw)
    return Evidence(**base)


def test_below_the_confidence_bar_is_mitigated_no_blame():
    v, why = classify(ev(confidence=0.4), P)
    assert v == NO_BLAME and "below" in why[0]


def test_not_confirmed_is_mitigated_no_blame():
    v, why = classify(ev(confirmed=False, harm_followed=False), P)
    assert v == NO_BLAME and "did not confirm" in why[0]


def test_over_plan_from_admission_refusals():
    v, why = classify(ev(rejected_share=0.4), P)
    assert v == OVER and any("refuses 40%" in w for w in why)


def test_costly_pattern_from_low_paid_share_or_ledger():
    assert classify(ev(paid_share=0.2), P)[0] == COSTLY
    assert classify(ev(blackbox_hint=0.8), P)[0] == COSTLY
    assert classify(ev(evict_hint=0.6), P)[0] == COSTLY


def test_honest_growth_when_paid_work_explains_the_load():
    v, why = classify(ev(), P)
    assert v == HONEST and any("paid work explains" in w for w in why)


def test_over_plan_wins_over_costly():
    assert classify(ev(rejected_share=0.5, paid_share=0.1), P)[0] == OVER


def test_repeat_offender_is_judged_without_a_confirmed_proof():
    v, why = classify(ev(confirmed=False, repeat=True, paid_share=0.1), P)
    assert v == COSTLY and "repeat offender" in why[0]


def test_host_verdict():
    assert host_verdict(3.0, P)[0] == HOST
    v, why = host_verdict(1.1, P)
    assert v == NO_BLAME and "undrained" in why


def m(pre=1.0, post=(0.1, 0.1), spare_pre=0.0, spare=(800.0, 800.0), ratio=(1.0, 1.0), fp=900.0):
    return ProofMeasure(pre, list(post), spare_pre, list(spare), list(ratio), fp)


def test_proof_confirmed_when_servers_recover_and_harm_follows():
    R, harm, ok, why = evaluate_proof(m(), P)
    assert R == pytest.approx(0.9) and harm and ok and "followed" in why


def test_proof_not_confirmed_when_servers_do_not_recover():
    R, harm, ok, _ = evaluate_proof(m(post=(0.8, 0.9)), P)
    assert R < P.recovery_R and harm and not ok


def test_proof_culprit_paused_mid_test():
    # its servers recover (it stopped), but nothing arrived on the spare
    R, harm, ok, why = evaluate_proof(m(spare=(5.0, 5.0)), P)
    assert R > P.recovery_R and not harm and not ok and "paused" in why


def test_harm_can_follow_by_latency_alone():
    _, harm, ok, _ = evaluate_proof(m(spare=(0.0, 0.0), ratio=(1.2, 2.5)), P)
    assert harm and ok


def test_nothing_to_prove():
    assert evaluate_proof(m(pre=0.0), P)[2] is False


def test_action_budget():
    b = ActionBudget(relocations_per_min=2, quarantine_every_s=30, max_drained=1)
    assert b.can_relocate(0)
    b.note_relocation(0)
    b.note_relocation(10)
    assert not b.can_relocate(30) and b.can_relocate(61)
    assert b.can_quarantine(0)
    b.note_quarantine(0)
    assert not b.can_quarantine(29) and b.can_quarantine(30)
    ok, _ = b.can_drain("w4", [0.3, 0.4])
    assert ok
    b.drained["w4"] = 0
    ok, why = b.can_drain("w5", [0.3])
    assert not ok and "at most 1" in why
    b.drained.clear()
    ok, why = b.can_drain("w5", [0.3, 0.8])
    assert not ok and "too busy" in why


def test_offender_memory():
    mem = OffenderMemory(count=3, window_s=600)
    for t in (0, 100, 200):
        mem.note_named("K", t)
    assert mem.is_repeat("K", 250) and not mem.is_repeat("J", 250)
    assert not mem.is_repeat("K", 650)          # the first naming fell out of the window
    mem.note_verdict("K", COSTLY)
    mem.note_verdict("K", NO_BLAME)             # no-blame verdicts don't overwrite what we learned
    assert mem.last_verdict["K"] == COSTLY


def test_params_from_yaml():
    p = VerdictParams.from_yaml({"confidence_to_prove": 0.7, "paid_share_costly": 0.4, "recovery_R": 0.6,
                                 "host_idle_ratio": 2.0,
                                 "verdicts": {"over_plan_rejected_share": 0.3, "repeat_count": 2}})
    assert (p.confidence_to_prove, p.paid_share_costly, p.recovery_R, p.host_idle_ratio) == (0.7, 0.4, 0.6, 2.0)
    assert p.over_plan_rejected_share == 0.3 and p.repeat_count == 2
    b = ActionBudget.from_yaml({"action_budget": {"relocations_per_min": 3, "max_drained": 2}})
    assert b.relocations_per_min == 3 and b.max_drained == 2
