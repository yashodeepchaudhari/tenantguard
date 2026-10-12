from __future__ import annotations

from tenantguard_common.contracts import BlockCmd, DrainCmd, LimitsCmd, RelocateCmd, RouteWeightsCmd

from tenantguard_gateway.overrides import Overrides


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_ttl_auto_revert_of_every_kind():
    clk = Clock()
    o = Overrides(clk)
    o.set_limits(LimitsCmd(tenant="K", factor=0.5, weight_factor=0.25, ttl_s=10))
    o.set_block(BlockCmd(tenant="K", ttl_s=5))
    o.set_route_weights(RouteWeightsCmd(weights={"A": {"w0": 3, "w1": 1}}, ttl_s=10))
    o.set_relocate(RelocateCmd(tenant="K", target="spare", ttl_s=10))
    o.set_drain(DrainCmd(server="w4", ttl_s=10))
    assert o.limits_for("K").factor == 0.5
    assert o.block_remaining("K") == 5
    assert o.weights_for("A") == {"w0": 0.75, "w1": 0.25}
    assert o.relocation_for("K", "abcd1234") == "spare"
    assert o.drained() == {"w4"}
    clk.t += 5
    assert o.block_remaining("K") is None            # getters never apply an expired override
    assert o.limits_for("K") is not None
    clk.t += 5
    assert o.limits_for("K") is None and o.weights_for("A") is None
    assert o.relocation_for("K") is None and o.drained() == set()
    gone = o.sweep()
    assert sorted(gone) == ["block:K", "drain:w4", "limits:K", "relocate:K", "route_weights:A"]
    v = o.view(2.0)
    assert not v.limits and not v.blocks and not v.expires_at and not v.drains


def test_latest_limits_replaces_and_restarts_ttl_and_1_1_clears():
    clk = Clock()
    o = Overrides(clk)
    o.set_limits(LimitsCmd(tenant="K", factor=0.5, weight_factor=1.0, ttl_s=10))
    clk.t += 8
    o.set_limits(LimitsCmd(tenant="K", factor=0.25, weight_factor=0.5, ttl_s=10))
    clk.t += 8
    cmd = o.limits_for("K")
    assert (cmd.factor, cmd.weight_factor) == (0.25, 0.5)
    o.set_limits(LimitsCmd(tenant="K", factor=1.0, weight_factor=1.0))
    assert o.limits_for("K") is None and "limits:K" not in o.expires


def test_class_relocation_and_explicit_clear():
    o = Overrides(Clock())
    o.set_relocate(RelocateCmd(tenant="K", cls="deadbeef", target="quarantine", ttl_s=30))
    assert o.relocation_for("K", "deadbeef") == "quarantine"
    assert o.relocation_for("K", "other") is None
    o.set_relocate(RelocateCmd(tenant="K", cls="deadbeef", target=None))
    assert o.relocation_for("K", "deadbeef") is None
    o.set_block(BlockCmd(tenant="K", ttl_s=30))
    o.set_block(BlockCmd(tenant="K", blocked=False))
    assert o.block_remaining("K") is None


def test_fail_static_uses_receive_time():
    clk = Clock()
    o = Overrides(clk)
    assert o.fail_static(2.0)                         # never heard from the brain
    o.heartbeat()
    clk.t += 1.5
    v = o.view(2.0)
    assert not v.fail_static and v.last_heartbeat_age_s == 1.5
    clk.t += 1.0
    assert o.view(2.0).fail_static
