from __future__ import annotations

import json
import re

import pytest
from conftest import NAMED_DETAIL, SRV, T0, ev, scenario, snap, stats

from tenantguard_billing.bill import credit_pct, opaque_ref
from tenantguard_billing.main import Billing, handle_ws_message


def line(bill, kind):
    return [x for x in bill.lines if x.kind == kind]


# ---------------------------------------------------------------- calm prices frozen during incidents
def test_victim_pays_calm_reference_during_incident(billing):
    scenario(billing)
    a = billing.bill("A", T0, T0 + 180, T0 + 1000)
    # measured 100 + 300 + 100 handler ms; the incident minute is billed at the calm 1 ms/request
    assert a.requests == 300
    assert a.measured_cpu_ms == 500.0
    assert a.billed_cpu_ms == 300.0
    assert a.contention_waived_cpu_ms == 200.0
    rate = billing.cfg.rate_rs_per_ms("A")
    assert line(a, "compute")[0].amount_rs == pytest.approx(300.0 * rate)


def test_reference_frozen_while_incident_open(billing):
    scenario(billing)
    # the incident bucket would have moved the EWMA to 1.4 ms (3 ms/request); frozen, it stays 1.0
    assert billing.pricer.ref[("A", "c_light")] == pytest.approx(1.0)
    p = billing.pricer.closed[("A", T0 + 60)]
    assert not p.calm and not p.blamed and p.billed_handler_ms == 100.0


def test_culprit_pays_measured(billing):
    scenario(billing)
    k = billing.bill("K", T0, T0 + 180, T0 + 1000)
    assert k.billed_cpu_ms == k.measured_cpu_ms == 600.0
    assert k.contention_waived_cpu_ms == 0.0
    assert k.sla_credit_pct == 0.0 and not line(k, "sla_credit")
    assert SRV in k.incident_refs                       # its own incident: real id


def test_whale_cannot_hide_behind_a_shared_class_price(billing):
    # same class id for light and whale searches (gateway hashes query keys, not values)
    b = billing
    b.on_stats(stats("w0", T0 - 5, T0 - 10, {"A": {}, "K": {}}, {}))
    b.on_stats(stats("w0", T0 + 55, T0 - 10, {"A": {"requests": 100, "cpu": 100.0}, "K": {"requests": 10, "cpu": 5000.0}},
                     {("A", "s"): (100, 100.0), ("K", "s"): (10, 5000.0)}))
    b.tick(T0 + 1000)
    assert b.pricer.ref[("A", "s")] == pytest.approx(1.0)
    assert b.pricer.ref[("K", "s")] == pytest.approx(500.0)


def test_provisional_bucket_does_not_update_refs(billing):
    b = billing
    b.on_stats(stats("w0", T0 - 5, T0 - 10, {"A": {}}, {}))
    b.on_stats(stats("w0", T0 + 30, T0 - 10, {"A": {"requests": 10, "cpu": 10.0}}, {("A", "s"): (10, 10.0)}))
    b.tick(T0 + 40)                                   # bucket not finished: not closed
    assert not b.pricer.ref
    assert b.bill("A", T0, T0 + 60, T0 + 40).billed_cpu_ms == 10.0


# ---------------------------------------------------------------- credits
def test_credit_table(cfg):
    assert credit_pct(cfg, 0) == 0
    assert credit_pct(cfg, 1.9) == 0
    assert credit_pct(cfg, 2) == 2
    assert credit_pct(cfg, 10) == 4
    assert credit_pct(cfg, 59.9) == 4
    assert credit_pct(cfg, 60) == 10
    assert credit_pct(cfg, 10_000) == 25


def test_victim_credit_math(billing):
    scenario(billing)
    a = billing.bill("A", T0, T0 + 180, T0 + 1000)
    assert a.sla_p95_ms == 50.0
    assert a.sla_violation_s == pytest.approx(10.0)          # 20 ticks x 0.5 s over 50 ms while w1 was slow
    assert a.sla_credit_pct == 4.0
    usage = sum(x.amount_rs for x in a.lines if x.kind != "sla_credit")
    assert a.subtotal_rs == pytest.approx(usage, abs=1e-4)
    assert a.credits_rs == pytest.approx(-0.04 * a.subtotal_rs, abs=1e-4)
    assert a.total_rs == pytest.approx(a.subtotal_rs + a.credits_rs, abs=1e-4)
    assert line(a, "sla_credit")[0].evidence == {"violation_s": 10.0, "pct": 4.0}


def test_no_credit_without_an_incident_or_for_tenants_without_traffic(billing):
    b = billing
    for i in range(40):                                  # A slow, but no incident open: its own load
        b.on_snapshot(snap(T0 + i * 0.5, {"A": 500.0, "C": 0.0}))
    assert b.bill("A", T0, T0 + 60, T0 + 100).sla_violation_s == 0
    b.on_events([ev("srv-x", "detected", T0 + 100)])
    for i in range(40):                                  # incident open, C has no traffic (p95 0): not "violating"
        b.on_snapshot(snap(T0 + 100 + i * 0.5, {"C": 0.0}))
    assert b.bill("C", T0, T0 + 200, T0 + 200).sla_violation_s == 0


# ---------------------------------------------------------------- true-cost line
def test_unmetered_blackbox_attributed_to_named_culprit(billing):
    scenario(billing)
    k = billing.bill("K", T0, T0 + 180, T0 + 1000)
    [um] = line(k, "unmetered")
    # window named(+64)..closed(+110) = 46 s of the +55..+115 sample (20060 ms) minus calm baseline 1 ms/s
    expected = 20060.0 * 46 / 60 - 46 * 1.0
    assert um.cpu_ms == pytest.approx(expected, rel=1e-3)
    assert um.evidence["incident_id"] == SRV and um.evidence["confidence"] == 0.85
    assert um.evidence["servers"] == ["w1", "w3", "w8"]
    assert um.amount_rs == pytest.approx(expected * billing.cfg.rate_rs_per_ms("K"), rel=1e-3)
    assert not line(billing.bill("A", T0, T0 + 180, T0 + 1000), "unmetered")


def test_no_unmetered_line_below_confidence(cfg):
    b = Billing(cfg, started_at=T0 - 1)
    scenario(b)
    b.timeline.episodes[SRV].culprits["K"] = 0.4
    assert not line(b.bill("K", T0, T0 + 180, T0 + 1000), "unmetered")


# ---------------------------------------------------------------- why + anonymisation
CULPRIT_TOKENS = ["KaamKaro", SRV, "w1", "w3", "w8"]


def assert_anonymous(text: str):
    for tok in CULPRIT_TOKENS:
        assert tok not in text, tok
    assert not re.search(r"\bK\b", text), text


def test_why_sentence_for_victim(billing):
    scenario(billing)
    w = billing.why("A", T0, T0 + 180, T0 + 1000)
    assert w.slow and w.violation_s == pytest.approx(10.0) and w.credit_pct == 4.0
    [e] = w.episodes
    assert e.cause == "neighbor" and e.servers_affected == 1 and e.servers_total == 3
    assert e.mitigated_after_s == 1.5 and e.worst_p95_ms == 120.0
    assert w.sentence.startswith("Between ")
    assert "another tenant's workload slowed 1 of your 3 servers." in w.sentence
    assert "Your traffic was moved to healthy servers in 1.5 s." in w.sentence
    assert w.sentence.endswith("A 4% service credit has been applied.")
    assert re.search(r"Between \d\d:\d\d:\d\d and \d\d:\d\d:\d\d IST", w.sentence)


def test_victim_views_never_reveal_the_culprit(billing):
    scenario(billing)
    for tenant in ("A", "E"):
        why = billing.why(tenant, T0, T0 + 180, T0 + 1000)
        bill = billing.bill(tenant, T0, T0 + 180, T0 + 1000)
        assert_anonymous(why.model_dump_json())
        assert_anonymous(bill.model_dump_json())
    a = billing.bill("A", T0, T0 + 180, T0 + 1000)
    assert a.incident_refs == [opaque_ref(SRV)]
    # the brain's own text named the culprit: proof that we did not copy it
    assert "KaamKaro" in NAMED_DETAIL


def test_why_for_culprit_is_own_usage_without_credit(billing):
    scenario(billing)
    w = billing.why("K", T0, T0 + 180, T0 + 1000)
    assert [e.cause for e in w.episodes] == ["own_usage"]
    assert w.credit_pct == 0 and "credit" not in w.sentence.replace("No credit applies", "")


def test_why_host_fault_and_ladder_and_quiet(billing):
    b = billing
    b.on_events([ev("srv-h", "detected", T0), ev("srv-h", "rescued", T0 + 0.8),
                 ev("srv-h", "named", T0 + 3, report={"culprits": [], "host_suspects": ["w4"]}),
                 ev("srv-h", "closed", T0 + 20)])
    for i in range(20):
        b.on_snapshot(snap(T0 + i * 0.5, {"B": 200.0}, slow=["w4"]))      # B = w3,w4,w5
    w = b.why("B", T0, T0 + 30, T0 + 100)
    assert w.episodes[0].cause == "host_fault"
    assert "No customer was at fault." in w.sentence and "w4" not in w.sentence
    # usage-ladder incident about K: A shares w1 with K
    b.on_events([ev("inc-1800000200-K-9", "detected", T0 + 200, tenant="K", level="warn"),
                 ev("inc-1800000200-K-9", "level_change", T0 + 202.5, tenant="K", level="throttle"),
                 ev("inc-1800000200-K-9", "closed", T0 + 230, tenant="K", level="normal")])
    for i in range(20):
        b.on_snapshot(snap(T0 + 200 + i * 0.5, {"A": 90.0}))
    w = b.why("A", T0 + 190, T0 + 240, T0 + 300)
    assert w.episodes[0].cause == "neighbor" and w.episodes[0].servers_affected == 1
    assert "That workload was throttled after 2.5 s." in w.sentence
    assert_anonymous(w.model_dump_json())
    # nothing happened to G in a quiet window
    q = b.why("G", T0 + 500, T0 + 600, T0 + 700)
    assert not q.slow and q.sentence.startswith("No slowdown")


def test_ws_messages_feed_state(billing):
    s = snap(T0, {"A": 10.0})
    handle_ws_message(billing, s.model_dump_json())
    handle_ws_message(billing, ev("srv-z", "detected", T0).model_dump_json())
    handle_ws_message(billing, json.dumps({"type": "something_else"}))
    assert len(billing.latency.ticks) == 1 and "srv-z" in billing.timeline.episodes


def test_culprit_slow_in_its_own_incident_gets_no_credit(billing):
    scenario(billing)
    for i in range(20):  # K itself is slow (200 ms > its 80 ms SLA) during the incident it caused
        billing.on_snapshot(snap(T0 + 200 + i * 0.5, {"K": 200.0, "E": 200.0}, slow=["w1"]))
    billing.on_events([ev("srv-again", "detected", T0 + 200),
                       ev("srv-again", "named", T0 + 201, report={"culprits": [{"tenant": "K", "confidence": 0.9}],
                                                                  "host_suspects": []}),
                       ev("srv-again", "closed", T0 + 215)])
    k = billing.bill("K", T0 + 190, T0 + 240, T0 + 1000)
    e = billing.bill("E", T0 + 190, T0 + 240, T0 + 1000)
    assert k.sla_violation_s == 0 and k.sla_credit_pct == 0
    assert e.sla_violation_s == pytest.approx(10.0) and e.sla_credit_pct == 4.0
