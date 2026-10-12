from __future__ import annotations

from collections import Counter, defaultdict

import pytest

from tenantguard_gateway.scheduler import Item, Scheduler


def run(s: Scheduler, n: int, refill):
    served = []
    for _ in range(n):
        it = s.pop()
        assert it is not None
        served.append(it)
        refill(it.tenant)
    return served


@pytest.mark.parametrize("costs", [{"A": 5, "B": 5, "C": 5}, {"A": 5, "B": 20, "C": 50}])
def test_drr_weights_4_2_1_within_5pct(costs):
    weights = {"A": 4, "B": 2, "C": 1}
    s = Scheduler(lambda t: weights[t], quantum_ms=5)
    for t in weights:
        for _ in range(50):
            s.push(Item(t, costs[t]))
    served = run(s, 10_000, lambda t: s.push(Item(t, costs[t])))   # keep every queue backlogged
    work = defaultdict(float)
    for it in served:
        work[it.tenant] += it.cost
    total = sum(work.values())
    for t, w in weights.items():
        expected = w / 7
        assert abs(work[t] / total - expected) <= 0.05 * expected, (t, work[t] / total, expected)


def test_weight_change_takes_effect():
    weights = {"A": 1.0, "B": 1.0}
    s = Scheduler(lambda t: weights[t])
    for t in weights:
        for _ in range(10):
            s.push(Item(t, 5))
    weights["A"] = 0.25                      # deprioritized (weight_factor 0.25)
    served = run(s, 2000, lambda t: s.push(Item(t, 5)))
    c = Counter(it.tenant for it in served)
    assert c["B"] / c["A"] == pytest.approx(4, rel=0.05)


def test_whale_cannot_starve_others():
    s = Scheduler(lambda t: 1.0, quantum_ms=5)
    for _ in range(1000):
        s.push(Item("W", 500))               # whale: 1000 queued requests of 500 ms each, all first
    for _ in range(100):
        s.push(Item("A", 5))
        s.push(Item("B", 5))
    first = [s.pop().tenant for _ in range(202)]
    assert first.count("A") == 100 and first.count("B") == 100
    assert first.count("W") <= 2


def test_fifo_mode_serves_in_arrival_order_so_whale_blocks():
    s = Scheduler(lambda t: 1.0, fifo=True)
    for _ in range(10):
        s.push(Item("W", 500))
    s.push(Item("A", 5))
    order = [s.pop().tenant for _ in range(11)]
    assert order == ["W"] * 10 + ["A"]


def test_cancelled_items_are_skipped_and_counted():
    s = Scheduler(lambda t: 1.0)
    a, b = Item("A", 5), Item("A", 5)
    s.push(a)
    s.push(b)
    assert s.depth("A") == 2
    assert s.cancel(a)
    assert s.depth("A") == 1
    assert s.pop() is b
    assert s.pop() is None and s.depth("A") == 0
    assert not s.cancel(b)                   # already dispatched


def test_ineligible_tenant_is_skipped_without_blocking_others():
    s = Scheduler(lambda t: 1.0)
    s.push(Item("A", 5))
    s.push(Item("B", 5))
    it = s.pop(lambda i: i.tenant != "A")
    assert it.tenant == "B"
    assert s.pop(lambda i: False) is None
    assert s.pop().tenant == "A"


def test_mode_switch_keeps_queued_work():
    s = Scheduler(lambda t: 1.0, fifo=True)
    for t in "ABAB":
        s.push(Item(t, 5))
    s.fifo = False
    assert sorted(s.pop().tenant for _ in range(4)) == list("AABB")
