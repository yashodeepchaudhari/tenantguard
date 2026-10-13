from __future__ import annotations

from tenantguard_gateway.telemetry import Telemetry


def test_cpu_missing_does_not_wipe_other_cumulative_counters():
    t = Telemetry()
    t.retry("K")
    t.retry("K")
    t.cpu_missing("K", "w1")
    t.cpu_missing("A", "w0")
    assert t.c_retries == {"K": 2}
    assert t.c_cpu_missing == {("K", "w1"): 1, ("A", "w0"): 1}
    t.retry("K")
    assert t.c_retries["K"] == 3


def test_frame_resets_per_tick_stats_but_keeps_cumulative_counters():
    t = Telemetry()
    t.admitted("A")
    t.retry("A")
    t.done("A", "w0", 2.0, 3.0, 200, cpu_ms=1.0)
    f = t.frame("industry", 250, ["A"], depth=lambda _: 0, inflight=lambda _: 0, limiter=[])
    q = f.queues[0]
    assert (q.admitted, q.retries, q.completed) == (1, 1, 1)
    f2 = t.frame("industry", 250, ["A"], depth=lambda _: 0, inflight=lambda _: 0, limiter=[])
    assert (f2.queues[0].admitted, f2.queues[0].retries, f2.queues[0].completed) == (0, 0, 0)
    assert t.c_retries["A"] == 1 and t.c_admitted["A"] == 1 and t.c_cpu_ms["A"] == 1.0


def test_refused_and_queued_cost_are_reported():
    t = Telemetry()
    t.rejected("K", ["rate"], cost_ms=4.0)
    t.expired("K", 900.0, cost_ms=6.0)
    f = t.frame("tenantguard", 250, ["K"], depth=lambda _: 2, inflight=lambda _: 0, limiter=[],
                queued_cost=lambda _: 11.5)
    q = f.queues[0]
    assert (q.refused_cost_ms, q.queued_cost_ms) == (10.0, 11.5)
