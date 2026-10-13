"""Allocator: golden numbers from docs/v2/03_ALGORITHMS.md §3 and seeded-random properties."""
import random

import pytest

from tenantguard_brain.allocator import allocate, estimate_demand, water_fill
from tenantguard_common.contracts import ProjectSpec

USABLE = 5.175     # 5.75 cores x (1 - 0.10)
PROJECTS = {
    "health": ProjectSpec(id="health", name="H", tier="enterprise", floor_cpu=1.25, cap_cpu=3.0),
    "commerce": ProjectSpec(id="commerce", name="C", tier="enterprise", floor_cpu=1.25, cap_cpu=3.0),
    "mobility": ProjectSpec(id="mobility", name="M", tier="pro", floor_cpu=0.75, cap_cpu=2.0),
    "media": ProjectSpec(id="media", name="Me", tier="pro", floor_cpu=0.75, cap_cpu=2.0),
    "free": ProjectSpec(id="free", name="F", tier="free", floor_cpu=0.5, cap_cpu=1.0),
}
WEIGHTS = {"health": 4, "commerce": 4, "mobility": 2, "media": 2, "free": 1}


def totals(a):
    return {p: round(x.total, 3) for p, x in a.items()}


def test_golden_two_projects_idle():
    d = {"health": 3.0, "commerce": 3.0, "mobility": 2.0, "media": 0.1, "free": 0.1}
    assert totals(allocate(USABLE, PROJECTS, WEIGHTS, d)) == {
        "health": 1.94, "commerce": 1.94, "mobility": 1.095, "media": 0.1, "free": 0.1}


def test_golden_media_wakes():
    d = {"health": 3.0, "commerce": 3.0, "mobility": 2.0, "media": 2.0, "free": 0.1}
    assert totals(allocate(USABLE, PROJECTS, WEIGHTS, d)) == {
        "health": 1.608, "commerce": 1.608, "mobility": 0.929, "media": 0.929, "free": 0.1}


def test_industry_is_static_floor_and_off_is_cap():
    d = {p: 3.0 for p in PROJECTS}
    assert totals(allocate(USABLE, PROJECTS, WEIGHTS, d, "industry"))["health"] == 1.25
    assert totals(allocate(USABLE, PROJECTS, WEIGHTS, d, "off"))["health"] == 3.0


def test_water_fill_caps_and_resplits():
    out = water_fill(3.0, {"a": 0.5, "b": 5.0, "c": 5.0}, {"a": 1, "b": 1, "c": 2})
    assert out["a"] == pytest.approx(0.5)
    assert out["b"] == pytest.approx(2.5 / 3)
    assert out["c"] == pytest.approx(5.0 / 3)


def test_estimate_demand_is_clipped_ewma():
    assert estimate_demand(1.0, 3.0, 0.5, 10.0) == 2.0
    assert estimate_demand(1.0, 30.0, 0.5, 2.0) == 2.0
    assert estimate_demand(0.0, -5.0, 0.5, 2.0) == 0.0


def test_properties_random():
    rng = random.Random(7)
    for _ in range(500):
        d = {p: rng.choice([0.0, rng.uniform(0, 4)]) for p in PROJECTS}
        a = allocate(USABLE, PROJECTS, WEIGHTS, d)
        assert sum(x.total for x in a.values()) <= USABLE + 1e-6
        for p, s in PROJECTS.items():
            assert a[p].total >= min(d[p], s.floor_cpu) - 1e-9          # floors honoured
            assert a[p].total <= min(max(d[p], 0), s.cap_cpu) + 1e-9     # never above want or cap
        unmet = any(a[p].total < min(d[p], s.cap_cpu) - 1e-6 for p, s in PROJECTS.items())
        if unmet:                                                        # work-conserving
            assert sum(x.total for x in a.values()) == pytest.approx(USABLE, abs=1e-6)
