import pytest

from tenantguard_brain.score import (BaselineTracker, compute_scores, fair_shares, noisy_score, shares, slowdown,
                                     victim_impact)

W = {"A": 4.0, "B": 2.0, "C": 1.0, "K": 2.0}
SERVERS = {"A": ["s0", "s1"], "B": ["s1", "s2"], "C": ["s2", "s3"], "K": ["s0", "s3"]}
NB = {t: [o for o in SERVERS if o != t and set(SERVERS[o]) & set(SERVERS[t])] for t in SERVERS}
CAP = {s: 500.0 for s in ("s0", "s1", "s2", "s3")}
SLOTS = {s: 8.0 for s in CAP}


def score(cpu_by_server, slow, occupancy=None, active=None, min_vi=0.2):
    return compute_scores(weights=W, servers_of=SERVERS, active=active or list(W), cpu_by_server=cpu_by_server,
                          cpu_cap=CAP, occupancy=occupancy or {}, slots=SLOTS, slowdowns=slow, neighbours=NB,
                          min_victim_impact=min_vi, quarantine_threshold=4.0)


def calm_cpu():
    # everyone uses 20% of its weighted entitlement: a calm 30%-ish fleet
    return {"s0": {"A": 40, "K": 20}, "s1": {"A": 40, "B": 20}, "s2": {"B": 20, "C": 10}, "s3": {"C": 10, "K": 20}}


def test_shares_and_fair_shares():
    assert shares({"a": 1, "b": 3}) == {"a": 0.25, "b": 0.75}
    assert shares({"a": 0, "b": 0}) == {"a": 0.0, "b": 0.0}
    f = fair_shares({"a": 4, "b": 2, "c": 1}, ["a", "b", "c"])
    assert f["a"] == pytest.approx(4 / 7) and f["c"] == pytest.approx(1 / 7)
    # an inactive tenant gets the share it would have if it became active (never divides by 0)
    f = fair_shares({"a": 4, "b": 2}, ["a"])
    assert f["a"] == 1.0 and f["b"] == pytest.approx(2 / 6)


def test_slowdown_and_floor():
    assert slowdown(20, 10) == pytest.approx(1.0)
    assert slowdown(5, 10) == 0.0
    assert slowdown(None, 10) is None and slowdown(10, None) is None
    # sub-floor jitter on a 1 ms endpoint is not a 3x slowdown
    assert slowdown(3, 1, floor_ms=8) == 0.0


def test_victim_impact_mean_over_neighbours_and_clipped():
    slow = {"A": 1.0, "B": 0.5, "C": 9.0, "K": 0.0}
    vi, victims = victim_impact("K", slow, ["A", "C"])
    assert vi == 3.0                      # mean(1, 9) = 5 clipped to 3
    assert victims[0] == ("C", 9.0)
    vi, _ = victim_impact("K", slow, ["A", "B"])
    assert vi == pytest.approx(0.75)
    # culprit itself never counts, missing data (None) is skipped, not treated as fast
    vi, _ = victim_impact("A", {"A": 5.0, "B": None, "K": 0.4})
    assert vi == pytest.approx(0.4)
    assert victim_impact("A", {})[0] == 0.0


def test_noisy_score_formula():
    s, r = noisy_score(cpu_share=0.6, queue_share=0.2, fair_share=0.2, impact=0.5)
    assert r == pytest.approx(3.0) and s == pytest.approx(4.5)
    s, r = noisy_score(0.1, 0.5, 0.25, 0.0)          # the queue dimension can dominate
    assert r == pytest.approx(2.0)
    assert noisy_score(0.5, 0.5, 0.0, 1.0) == (0.0, 0.0)


def test_calm_fleet_nobody_flagged():
    sc = score(calm_cpu(), {t: 0.0 for t in W})
    assert all(not d.flagged for d in sc.values())
    assert all(d.usage_ratio < 1 for d in sc.values())


def test_whale_flagged_only_when_neighbours_hurt():
    cpu = calm_cpu()
    cpu["s0"]["K"] = 300
    cpu["s3"]["K"] = 300
    hurt = {"A": 1.5, "B": 0.0, "C": 1.2, "K": 0.0}
    sc = score(cpu, hurt)
    k = sc["K"]
    assert k.flagged and k.usage_ratio > 1
    assert k.score == pytest.approx(k.usage_ratio * (1 + k.victim_impact))
    assert k.norm == pytest.approx(min(1.0, k.score / 4.0))
    # an innocent low user with hurt neighbours is not flagged (it does not over-use)
    assert not sc["B"].flagged and not sc["A"].flagged


def test_legit_burst_with_no_victim_impact_is_not_flagged():
    cpu = calm_cpu()
    cpu["s0"]["K"] = 450                   # K bursts to 90% of s0 and s3 ...
    cpu["s3"]["K"] = 450
    sc = score(cpu, {t: 0.0 for t in W})   # ... but nobody is slower than baseline
    assert sc["K"].usage_ratio > 1.5
    assert not sc["K"].flagged
    # a tiny wobble below min_victim_impact is still "not hurt"
    sc = score(cpu, {"A": 0.1, "B": 0.0, "C": 0.1, "K": 0.0})
    assert not sc["K"].flagged


def test_queue_occupancy_dimension():
    sc = score(calm_cpu(), {"A": 1.0, "B": 0.0, "C": 1.0, "K": 0.0}, occupancy={"K": 12.0})
    assert sc["K"].queue_share > sc["K"].cpu_share
    assert sc["K"].dominant == "queue" and sc["K"].flagged


def test_baseline_ewma_is_time_based():
    b = BaselineTracker(half_life_s=60, warmup=4)
    assert b.get("A") is None and not b.ready("A")
    t = 0.0
    for v in (10, 12, 8, 10):
        t += 0.25
        b.update("A", v, t)
    assert b.get("A") == pytest.approx(10.0)          # warm-up = running mean
    assert b.ready("A")
    b.update("A", 20, t + 60)                          # one half-life later: halfway to the new value
    assert b.get("A") == pytest.approx(15.0)
    b2 = BaselineTracker(half_life_s=60, warmup=0)
    b2.update("A", 10, 0)
    for i in range(1, 241):                            # 60 s of 4 Hz samples at 20 ms
        b2.update("A", 20, i * 0.25)
    assert b2.get("A") == pytest.approx(15.0, abs=0.01)  # same half-life whatever the sample rate
    b.update("A", 0, t + 61)                           # a tick with no completions is ignored
    assert b.get("A") == pytest.approx(15.0)
