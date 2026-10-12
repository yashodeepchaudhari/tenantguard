from __future__ import annotations

import pytest

from tenantguard_gateway.limiter import GradientLimiter
from tenantguard_gateway.retry import RetryBudget, fingerprint


def feed(lim: GradientLimiter, upstream_ms: float, cpu_ms: float = 0.0, n: int = 10, inflight: int = 64) -> int:
    for _ in range(n):
        lim.observe(upstream_ms, cpu_ms)
    lim.note_inflight(inflight)
    return lim.update()


# ---------- gradient limiter ----------
def test_window_grows_to_max_when_latency_stays_at_baseline():
    lim = GradientLimiter(8, 2, 64)
    for _ in range(100):
        feed(lim, 5.0)
    assert lim.window == 64


def test_window_shrinks_when_worker_side_latency_rises_and_settles_near_gradient_fixed_point():
    lim = GradientLimiter(32, 2, 64)
    feed(lim, 5.0)                              # baseline minRTT = 5 ms
    for _ in range(60):
        w = feed(lim, 10.0)                     # contention doubles the excess latency: gradient 0.5
    # fixed point of w = w*g + sqrt(w) is 1/(1-g)^2 = 4 (minRTT drifts up slowly, so allow a little)
    assert 3 <= w <= 6


def test_own_cpu_is_not_congestion():
    lim = GradientLimiter(16, 2, 64)
    feed(lim, 5.0, 0.0)
    w = feed(lim, 305.0, 300.0)                 # a whale request: slow because expensive, excess still 5 ms
    assert w >= 16


def test_window_clamped_and_never_moves_without_samples():
    lim = GradientLimiter(8, 2, 64)
    assert lim.update() == 8
    feed(lim, 1.0)
    for _ in range(50):
        feed(lim, 1000.0)
    assert lim.window == 2


def test_no_growth_when_window_underused():
    lim = GradientLimiter(8, 2, 64)
    for _ in range(20):
        feed(lim, 5.0, inflight=2)              # never used half the window
    assert lim.window == 8


def test_min_rtt_decays_upward_to_rebaseline():
    lim = GradientLimiter(8, 2, 64)
    feed(lim, 2.0)
    for _ in range(1500):
        feed(lim, 8.0)
    assert lim.min_rtt == pytest.approx(8.0)    # a permanently slower host becomes the new baseline
    assert lim.last_gradient == pytest.approx(1.0)


# ---------- retry budget ----------
def test_plain_repeats_are_not_retries():
    rb = RetryBudget(0.1, 10, 3)
    fp = fingerprint("GET", "/api/light", "", b"")
    for i in range(100):
        v = rb.check("A", fp, None, i * 0.05)
        rb.succeeded("A", fp)
        assert not v.is_retry and v.allowed


def test_repeat_of_failed_request_is_a_retry_and_budget_caps_it():
    rb = RetryBudget(0.1, 10, 3)
    fp = fingerprint("GET", "/api/light", "", b"")
    other = [fingerprint("GET", "/api/read", f"key={i}", b"") for i in range(49)]
    for i, f in enumerate(other):               # 49 + the original = 50 first attempts -> budget = max(3, 5) = 5
        rb.check("K", f, None, i * 0.01)
    rb.check("K", fp, None, 0.5)
    rb.failed("K", fp, 0.5, retry_after_s=2.0)
    verdicts = [rb.check("K", fp, None, 0.6 + i * 0.01) for i in range(20)]
    assert all(v.is_retry for v in verdicts)
    assert sum(v.allowed for v in verdicts) == 5
    refused = [v for v in verdicts if not v.allowed]
    assert refused[0].retry_after_s >= 1.5      # still told to wait for the original Retry-After


def test_retry_budget_window_expires_and_other_tenants_unaffected():
    rb = RetryBudget(0.1, 10, 3)
    fp = fingerprint("POST", "/api/write", "", b"x")
    rb.check("K", fp, None, 0.0)
    rb.failed("K", fp, 0.0)
    assert [rb.check("K", fp, None, 1.0 + i * 0.1).allowed for i in range(5)] == [True, True, True, False, False]
    assert rb.check("A", fp, None, 2.0).is_retry is False      # per tenant
    assert rb.check("K", fp, None, 11.0).is_retry is False     # the failure is forgotten after window_s


def test_idempotency_key_repeat_is_a_retry_even_after_success():
    rb = RetryBudget(0.1, 10, 3)
    assert not rb.check("A", "fp1", "key-1", 0.0).is_retry
    assert rb.check("A", "fp2", "key-1", 1.0).is_retry
    assert not rb.check("A", "fp3", "key-2", 1.0).is_retry


def test_fingerprint_includes_query_and_body():
    a = fingerprint("GET", "/api/read", "key=1", b"")
    assert a != fingerprint("GET", "/api/read", "key=2", b"")
    assert fingerprint("POST", "/api/write", "", b"a") != fingerprint("POST", "/api/write", "", b"b")
