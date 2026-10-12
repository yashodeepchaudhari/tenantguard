from __future__ import annotations

import random
from collections import Counter

import pytest
from tenantguard_common.contracts import Quota

from tenantguard_gateway.router import candidates, choose
from tenantguard_gateway.tenants import body_bucket, request_class, scale_quota

KNOWN = ["w0", "w1", "w2", "spare"]


def test_default_equal_weights_random_spread():
    rng = random.Random(1)
    c = Counter(choose(candidates(["w0", "w1", "w2"], KNOWN), rng=rng) for _ in range(9000))
    for s in ("w0", "w1", "w2"):
        assert c[s] == pytest.approx(3000, rel=0.06)


def test_route_weights_drain_relocate():
    cands = candidates(["w0", "w1", "w2"], KNOWN, weights={"w0": 0.0, "w1": 0.5, "spare": 0.5})
    assert dict(cands) == {"w1": 0.5, "spare": 0.5}
    assert candidates(["w0", "w1", "w2"], KNOWN, drained={"w1"}) == [("w0", 1.0), ("w2", 1.0)]
    assert candidates(["w0", "w1", "w2"], KNOWN, relocation="spare") == [("spare", 1.0)]
    # never black-hole: everything drained -> fall back to the barcode
    assert len(candidates(["w0", "w1", "w2"], KNOWN, drained={"w0", "w1", "w2"})) == 3


def test_choose_respects_capacity():
    assert choose([("w0", 1), ("w1", 1)], has_capacity=lambda s: s == "w1") == "w1"
    assert choose([("w0", 1)], has_capacity=lambda s: False) is None


def test_request_class_is_stable_and_order_insensitive():
    a = request_class("get", "/api/search", ["n", "q"], 0)
    assert a == request_class("GET", "/api/search", ["q", "n"], 0)
    assert len(a) == 8 and int(a, 16) >= 0
    assert a != request_class("GET", "/api/search", ["n"], 0)
    assert a != request_class("POST", "/api/search", ["n", "q"], 0)
    assert request_class("POST", "/api/write", [], 10) != request_class("POST", "/api/write", [], 5000)
    assert [body_bucket(n) for n in (0, 1, 1024, 1025, 300_000)] == ["0", "1k", "1k", "16k", "big"]


def test_scale_quota_int_rounding():
    q = Quota(rate_rps=50, burst=100, cost_ms_per_s=300, concurrency=8)
    s = scale_quota(q, 0.1)
    assert (s.burst, s.concurrency) == (10, 1)
    assert s.rate_rps == pytest.approx(5) and s.cost_ms_per_s == pytest.approx(30)
    assert scale_quota(q, 0.01).concurrency == 1
