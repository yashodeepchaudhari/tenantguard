from __future__ import annotations

from collections import Counter

from tenantguard_common.contracts import Quota

from tenantguard_gateway.router import choose, rank
from tenantguard_gateway.tenants import body_bucket, request_class, scale_quota, size_bucket

TENANTS = [f"t{i}" for i in range(1000)]


def test_rendezvous_spreads_tenants_evenly():
    reps = ["r1", "r2", "r3", "r4"]
    c = Counter(rank(t, reps)[0] for t in TENANTS)
    assert all(200 <= c[r] <= 300 for r in reps), c


def test_adding_a_replica_moves_about_one_in_n_plus_one_and_only_to_it():
    before = {t: rank(t, ["r1", "r2", "r3"])[0] for t in TENANTS}
    after = {t: rank(t, ["r1", "r2", "r3", "r4"])[0] for t in TENANTS}
    moved = [t for t in TENANTS if before[t] != after[t]]
    assert 0.15 <= len(moved) / len(TENANTS) <= 0.35                # about 1/4
    assert all(after[t] == "r4" for t in moved)                     # nobody moves between old replicas


def test_removing_a_replica_moves_only_its_tenants():
    before = {t: rank(t, ["r1", "r2", "r3"])[0] for t in TENANTS}
    after = {t: rank(t, ["r1", "r3"])[0] for t in TENANTS}
    assert all(before[t] == after[t] for t in TENANTS if before[t] != "r2")


def test_choose_pin_then_rendezvous_with_spill():
    first, second = rank("A", ["r1", "r2"])
    assert choose("A", ["r1", "r2"], lambda r: True) == first
    assert choose("A", ["r1", "r2"], lambda r: r != first) == second      # spill when the first is full
    assert choose("A", ["r1", "r2"], lambda r: False) is None
    assert choose("A", ["r1", "r2"], lambda r: True, pin=second) == second
    assert choose("A", ["r1", "r2"], lambda r: r != second, pin=second) is None   # pinned: wait, don't spill
    assert choose("A", ["r1"], lambda r: True, pin="gone") == "r1"               # stale pin ignored
    assert choose("A", [], lambda r: True) is None


def test_request_class_and_size_bucket():
    assert body_bucket(0) == "0" and body_bucket(1024) == "1k" and body_bucket(10**6) == "big"
    assert size_bucket({"n": "1000"}, ["n"]) == "n=2^9" and size_bucket({"n": "500000"}, ["n"]) == "n=2^18"
    assert size_bucket({"n": "abc", "q": "5"}, ["n", "limit"]) == "" and size_bucket({"n": "0"}, ["n"]) == ""
    a = request_class("GET", "/api/search", ["n"], 0, "n=2^9")
    assert a == request_class("get", "/api/search", ["n", "n"], 0, "n=2^9") and len(a) == 8
    assert a != request_class("GET", "/api/search", ["n"], 0, "n=2^18")


def test_scale_quota():
    q = Quota(rate_rps=10, burst=5, concurrency=3)
    assert scale_quota(q, 1.0) is q
    assert scale_quota(q, 0.1) == Quota(rate_rps=1.0, burst=1, concurrency=1)
