"""health.py, rescue.py, decoder.py: pure logic."""
from itertools import combinations

import pytest

from tenantguard_common.config import load_tenants

from tenantguard_brain.decoder import DecoderParams, decode, logistic
from tenantguard_brain.health import HealthParams, HealthTracker
from tenantguard_brain.rescue import plan_rescue

BARCODES = {t.id: list(t.servers) for t in load_tenants()}
SERVERS = sorted({s for v in BARCODES.values() for s in v})
TICK = 0.25


# ------------------------------------------------------------------ layout
def test_ag23_any_two_tenants_share_at_most_one_server():
    for a, b in combinations(BARCODES, 2):
        assert len(set(BARCODES[a]) & set(BARCODES[b])) <= 1
    assert len(BARCODES) == 12 and len(SERVERS) == 9


# ------------------------------------------------------------------ health
def feed(h, levels, t0, secs, n=5):
    t = t0
    flips = []
    for _ in range(int(round(secs / TICK))):
        t += TICK
        flips += [(t, f) for f in h.observe({s: (v, n) for s, v in levels.items()}, t)]
    return t, flips


def test_peer_ratio_and_debounce():
    p = HealthParams()
    h = HealthTracker(SERVERS, p)
    calm = {s: 8.0 for s in SERVERS}
    t, flips = feed(h, calm, 0, 5)
    assert flips == [] and h.slow_servers() == []
    assert h.servers["w4"].peer_ratio == pytest.approx(1.0)
    bad = dict(calm, w4=40.0)
    # median of 8 samples: needs > half the window bad, then open_after_ticks more
    t, flips = feed(h, bad, t, 3)
    assert flips and flips[0][1] == ("w4", True)
    assert h.servers["w4"].peer_ratio == pytest.approx(5.0)
    assert h.slow_servers() == ["w4"]
    t, flips = feed(h, calm, t, 3)
    assert flips and flips[0][1] == ("w4", False) and h.slow_servers() == []


def test_ratio_alone_is_not_enough():
    h = HealthTracker(SERVERS, HealthParams())
    tiny = {s: 1.0 for s in SERVERS}
    tiny["w4"] = 3.0                         # 3x its peers but only +2 ms: below slow_abs_ms
    feed(h, tiny, 0, 5)
    assert h.slow_servers() == [] and h.servers["w4"].peer_ratio == pytest.approx(3.0)


def test_one_bad_tick_is_ignored():
    h = HealthTracker(SERVERS, HealthParams())
    calm = {s: 8.0 for s in SERVERS}
    t, _ = feed(h, calm, 0, 3)
    t, _ = feed(h, dict(calm, w4=200.0), t, TICK)
    t, flips = feed(h, calm, t, 3)
    assert flips == [] and h.slow_servers() == []


def test_fleet_wide_slowdown_accuses_nobody():
    h = HealthTracker(SERVERS, HealthParams())
    t, _ = feed(h, {s: 8.0 for s in SERVERS}, 0, 3)
    t, flips = feed(h, {s: 80.0 for s in SERVERS}, t, 5)
    assert flips == [] and h.slow_servers() == []


def test_canary_takes_over_when_traffic_moved_away():
    h = HealthTracker(SERVERS, HealthParams())
    calm = {s: 8.0 for s in SERVERS}
    t = 0.0
    for _ in range(40):                       # 10 s: traffic everywhere but w4, canaries everywhere
        t += TICK
        if int(t * 4) % 4 == 0:
            for s in SERVERS:
                h.observe_canary(s, 3.0 + (50.0 if s == "w4" else 0.0), t)
        h.observe({s: (v, 5) for s, v in calm.items() if s != "w4"}, t)
    w4 = h.servers["w4"]
    assert w4.source == "canary" and w4.slow and w4.peer_ratio > 10


# ------------------------------------------------------------------ rescue
CAP = {s: 500.0 for s in SERVERS}


def calm_load(per_tenant=30.0):
    load = {s: {} for s in SERVERS}
    for t, servers in BARCODES.items():
        for s in servers:
            load[s][t] = per_tenant / len(servers)
    return load


def test_rescue_moves_every_neighbour_off_the_slow_server():
    plan = plan_rescue(barcodes=BARCODES, slow=["w4"], load=calm_load(), capacity=CAP, guard=0.7)
    on_w4 = {t for t, v in BARCODES.items() if "w4" in v}
    assert set(plan.weights) == on_w4 == {"B", "E", "G", "L"}
    for t, w in plan.weights.items():
        assert w["w4"] == 0.0
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)
        assert set(w) == set(BARCODES[t])        # only its own barcode servers
    assert plan.stuck == [] and plan.capped == []


def test_rescue_respects_the_capacity_guard():
    load = calm_load()
    load["w4"]["B"] = 100.0                      # B is heavy on the slow server ...
    for s in ("w3", "w5"):                       # ... and its healthy servers are nearly full
        load[s]["X"] = 0.6 * 500
    plan = plan_rescue(barcodes=BARCODES, slow=["w4"], load=load, capacity=CAP, guard=0.7)
    assert "B" in plan.capped and 0 < plan.moved["B"] < 1
    assert plan.weights["B"]["w4"] > 0           # part of B stays: nowhere safe to put it
    assert all(u <= 0.7 + 1e-6 for s, u in plan.projected_util.items() if s in ("w3", "w5"))


def test_tenant_on_all_slow_servers_is_stuck():
    plan = plan_rescue(barcodes=BARCODES, slow=list(BARCODES["K"]), load=calm_load(), capacity=CAP)
    assert plan.stuck == ["K"]
    assert "K" not in plan.weights
    assert len(plan.weights) == 9                # every neighbour of K is rescued


def test_no_room_at_all_leaves_the_tenant_alone():
    load = calm_load()
    for s in ("w3", "w5"):
        load[s]["X"] = 0.75 * 500                # already over the guard
    plan = plan_rescue(barcodes=BARCODES, slow=["w4"], load=load, capacity=CAP, guard=0.7)
    assert "B" not in plan.weights and plan.moved["B"] == 0.0 and "B" in plan.capped


def test_rescue_only_targets_servers_that_are_up():
    # 3-worker stack: only w3, w4, w5 run; w4 is slow
    plan = plan_rescue(barcodes=BARCODES, slow=["w4"], load=calm_load(), capacity=CAP, targets=["w3", "w5"])
    assert set(plan.weights) == {"B"}                           # E, G, L have no live alternative
    assert set(plan.weights["B"]) == {"w3", "w4", "w5"}
    assert plan.stuck == []


def test_no_load_data_splits_evenly():
    plan = plan_rescue(barcodes={"B": ["w3", "w4", "w5"]}, slow=["w4"], load={}, capacity=CAP)
    assert plan.weights["B"] == {"w3": 0.5, "w4": 0.0, "w5": 0.5}


# ------------------------------------------------------------------ decoder
def ratios_for(slow, bad=3.0):
    return {s: (bad if s in slow else 1.0) for s in SERVERS}


def stuck_for(slow):
    return {t: all(s in slow for s in v) for t, v in BARCODES.items()}


def test_logistic():
    assert logistic(0) == 0.5 and logistic(50) > 0.999 and logistic(-50) < 0.001


@pytest.mark.parametrize("culprit", sorted(BARCODES))
def test_every_single_culprit_is_named(culprit):
    slow = set(BARCODES[culprit])
    d = decode(BARCODES, ratios_for(slow), stuck=stuck_for(slow))
    assert [c.tenant for c in d.culprits] == [culprit]
    assert d.culprits[0].confidence > 0.9 and d.host_suspects == []


def test_all_66_culprit_pairs_are_named_exactly():
    pairs = list(combinations(sorted(BARCODES), 2))
    assert len(pairs) == 66
    for a, b in pairs:
        slow = set(BARCODES[a]) | set(BARCODES[b])
        d = decode(BARCODES, ratios_for(slow), stuck=stuck_for(slow))
        assert {c.tenant for c in d.culprits} == {a, b}, (a, b, d.rounds)
        assert d.host_suspects == [], (a, b)
        assert all(c.confidence > 0.9 for c in d.culprits), (a, b, d.culprits)


@pytest.mark.parametrize("server", SERVERS)
def test_single_slow_server_is_a_host_suspect_and_nobody_is_blamed(server):
    d = decode(BARCODES, ratios_for({server}), stuck=stuck_for({server}))
    assert d.culprits == [] and d.host_suspects == [server]
    assert max(d.top_confidence, key=d.top_confidence.get) == "host_fault"


def test_two_slow_servers_on_one_barcode_line_are_not_a_tenant():
    # w4 + w5 are both on B's barcode, but B's third server (w3) is fine: not B
    slow = {"w4", "w5"}
    d = decode(BARCODES, ratios_for(slow), stuck=stuck_for(slow))
    assert d.culprits == [] and d.host_suspects == ["w4", "w5"]


def test_nothing_slow_names_nothing():
    d = decode(BARCODES, ratios_for(set()))
    assert d.culprits == [] and d.host_suspects == []
    assert max(d.top_confidence, key=d.top_confidence.get) == "none"


def test_culprit_plus_unrelated_host_fault():
    slow = set(BARCODES["K"]) | {"w5"}           # K on w1, w3, w8; w5 broken independently
    d = decode(BARCODES, ratios_for(slow), stuck=stuck_for(slow))
    assert [c.tenant for c in d.culprits] == ["K"] and d.host_suspects == ["w5"]


def test_partial_signal_lowers_confidence():
    r = ratios_for(set(BARCODES["K"]))
    r["w8"] = 1.7                                  # one of K's servers only borderline slow
    d = decode(BARCODES, r)
    assert [c.tenant for c in d.culprits] == ["K"]
    full = decode(BARCODES, ratios_for(set(BARCODES["K"])))
    assert d.culprits[0].confidence < full.culprits[0].confidence


def test_ledger_hint_breaks_a_tie_only_where_servers_are_slow():
    slow = set(BARCODES["A"]) | set(BARCODES["D"])     # A and D share w0
    hints = {"J": 1.0}                                  # J is "suspicious" but its servers are mostly fine
    d = decode(BARCODES, ratios_for(slow), stuck=stuck_for(slow), ledger=hints)
    assert {c.tenant for c in d.culprits} == {"A", "D"}


def test_params_from_yaml_override():
    p = DecoderParams.from_yaml({"slow_peer_ratio": 1.8}, {"k": 4, "max_culprits": 1})
    assert p.slow_peer_ratio == 1.8 and p.k == 4.0 and p.max_culprits == 1
