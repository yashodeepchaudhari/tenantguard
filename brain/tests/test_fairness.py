import pytest

from tenantguard_brain.fairness import jain, normalized_throughput, peer_ratio


def test_jain_bounds():
    assert jain([5, 5, 5, 5]) == pytest.approx(1.0)
    assert jain([1, 0, 0, 0]) == pytest.approx(0.25)          # 1/n when one tenant gets everything
    assert jain([]) == 1.0 and jain([0, 0]) == 1.0
    assert 0.25 < jain([10, 1, 1, 1]) < 1.0


def test_normalized_throughput_uses_weights_and_skips_idle():
    x = normalized_throughput({"A": 40, "B": 20, "C": 10, "D": 0}, {"A": 4, "B": 2, "C": 1, "D": 1})
    assert x == {"A": 10, "B": 10, "C": 10}
    assert jain(x.values()) == pytest.approx(1.0)              # weighted-fair => 1.0


def test_peer_ratio():
    p95 = {"w0": 10, "w1": 10, "w2": 30, "w3": None}
    assert peer_ratio("w2", p95) == pytest.approx(3.0)
    assert peer_ratio("w0", p95) == pytest.approx(10 / 20)
    assert peer_ratio("w3", p95) == 1.0                        # unknown -> neutral
