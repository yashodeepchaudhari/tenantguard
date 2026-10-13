from __future__ import annotations

import time

from tenantguard_common.contracts import BlockCmd, LimitsCmd, Snapshot

from tenantguard_gateway.overrides import Overrides


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def snap(version=1, limits=(), pins=None, pin_ttl_s=30.0) -> Snapshot:
    return Snapshot(version=version, issued_at=time.time(), tenants=[], replicas={}, limits=list(limits),
                    pins=pins or {}, pin_ttl_s=pin_ttl_s)


def test_ttl_auto_revert_of_every_kind():
    clk = Clock()
    o = Overrides(clk)
    o.apply_snapshot(snap(limits=[LimitsCmd(tenant="K", factor=0.5, weight_factor=0.25, ttl_s=10)],
                          pins={"F": "tg-free-2"}, pin_ttl_s=20))
    o.set_block(BlockCmd(tenant="K", ttl_s=5))
    assert o.limits_for("K").factor == 0.5 and o.pin_for("F") == "tg-free-2" and o.block_remaining("K") == 5
    clk.t += 5
    assert o.block_remaining("K") is None            # getters never apply an expired override
    clk.t += 5
    assert o.limits_for("K") is None and o.pin_for("F") == "tg-free-2"
    assert sorted(o.sweep()) == ["block:K", "limits:K"]
    clk.t += 10
    assert o.pin_for("F") is None and o.sweep() == ["pin:F"]
    assert o.limits == {} and o.blocks == {} and o.pins == {}


def test_each_snapshot_replaces_limits_and_pins():
    clk = Clock()
    o = Overrides(clk)
    o.apply_snapshot(snap(1, [LimitsCmd(tenant="K", factor=0.5, ttl_s=60)], {"F": "r2"}))
    o.apply_snapshot(snap(2, [LimitsCmd(tenant="G", factor=0.25, ttl_s=60),
                              LimitsCmd(tenant="X", factor=1.0, weight_factor=1.0)]))
    assert o.limits_for("K") is None and o.pin_for("F") is None       # absent from the new snapshot = cleared
    assert o.limits_for("G").factor == 0.25 and o.limits_for("X") is None and o.snapshot_version == 2


def test_fail_static_follows_the_snapshot_clock():
    clk = Clock()
    o = Overrides(clk)
    assert o.fail_static(2.0)                         # no snapshot yet
    o.apply_snapshot(snap())
    assert not o.fail_static(2.0)
    clk.t += 2.5
    v = o.view(2.0)
    assert v.fail_static and v.last_snapshot_age_s == 2.5


def test_unblock_and_tenant_expiry():
    clk = Clock()
    o = Overrides(clk)
    o.set_block(BlockCmd(tenant="K", ttl_s=30))
    o.apply_snapshot(snap(limits=[LimitsCmd(tenant="K", factor=0.5, ttl_s=60)]))
    assert o.tenant_expiry("K") == 1060.0
    o.set_block(BlockCmd(tenant="K", blocked=False))
    assert o.block_remaining("K") is None and o.tenant_expiry("K") == 1060.0
