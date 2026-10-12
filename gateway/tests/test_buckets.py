from __future__ import annotations

import pytest
from tenantguard_common.contracts import Quota

from tenantguard_gateway.admission import admit, ratelimit_headers
from tenantguard_gateway.buckets import CostBucket, TokenBucket

Q = Quota(rate_rps=10, burst=5, cost_ms_per_s=100, concurrency=2)


def test_token_bucket_burst_then_refill_rate():
    b = TokenBucket(rate=10, burst=5, now=0.0)
    for _ in range(5):
        assert b.available(0.0)
        b.take(0.0)
    assert not b.available(0.0)
    assert b.retry_after(0.0) == pytest.approx(0.1)        # 1 token at 10/s
    assert not b.available(0.05)
    assert b.available(0.1)
    assert b.level(100.0) == 5                              # capped at burst


def test_token_bucket_reconfigure_clamps_level():
    b = TokenBucket(rate=10, burst=5, now=0.0)
    b.configure(rate=1, burst=2, now=0.0)
    assert b.level(0.0) == 2
    b.take(0.0)
    b.take(0.0)
    assert b.retry_after(0.0) == pytest.approx(1.0)


def test_cost_bucket_overdraw_and_payback():
    c = CostBucket(rate=100, capacity=200, now=0.0)
    assert c.available(0.0)
    c.debit(350, 0.0)                          # one heavy request overdraws: level -150
    assert c.level(0.0) == pytest.approx(-150)
    assert not c.available(0.0)
    assert c.retry_after(0.0) == pytest.approx(1.5)   # debt / refill rate
    assert not c.available(1.49)
    assert c.available(1.5)
    assert c.level(100.0) == 200


def test_admit_consumes_token_only_when_admitted():
    t, c = TokenBucket(10, 5, 0.0), CostBucket(100, 200, 0.0)
    c.debit(300, 0.0)
    d = admit(t, c, 0, Q, 0.0)
    assert not d.ok and d.reasons == ["cost"]
    assert t.level(0.0) == 5                    # refused request did not burn a token
    assert d.retry_after_s == pytest.approx(1.0)
    assert d.retry_after_header == "1"


def test_admit_reports_max_wait_across_dimensions_and_rounds_up():
    t, c = TokenBucket(0.5, 1, 0.0), CostBucket(100, 200, 0.0)
    t.take(0.0)                                  # next token in 2.0 s
    c.debit(450, 0.0)                            # debt 250 ms -> 2.5 s
    d = admit(t, c, 0, Q, 0.0)
    assert set(d.reasons) == {"rate", "cost"}
    assert d.retry_after_s == pytest.approx(2.5)
    assert d.retry_after_header == "3"


def test_admit_concurrency_and_block():
    t, c = TokenBucket(10, 5, 0.0), CostBucket(100, 200, 0.0)
    assert admit(t, c, 1, Q, 0.0).ok
    d = admit(t, c, 2, Q, 0.0)
    assert not d.ok and d.reasons == ["concurrency"]
    d = admit(t, c, 0, Q, 0.0, block_remaining_s=12.2)
    assert d.reasons == ["blocked"] and d.retry_after_header == "13"


def test_ratelimit_headers_shape():
    t, c = TokenBucket(10, 5, 0.0), CostBucket(100, 200, 0.0)
    h = ratelimit_headers(t, c, 1, Q, 2.0, 0.0)
    assert h["RateLimit-Policy"] == '"rate";q=5;w=1, "cost";q=200;w=2;qu="cpu-ms", "concurrency";q=2'
    assert h["RateLimit"] == '"rate";r=5;t=0, "cost";r=200;t=0, "concurrency";r=1'
