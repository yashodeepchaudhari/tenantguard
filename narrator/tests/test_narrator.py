from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from tenantguard_common.config import load_tenants
from tenantguard_common.contracts import ReportView

from tenantguard_narrator import template
from tenantguard_narrator.app import create_app
from tenantguard_narrator.evidence import EvidenceBook, anonymise, hhmmss
from tenantguard_narrator.service import Narrator
from tenantguard_narrator.settings import NarratorSettings

TENANTS = load_tenants()
NAMES = {t.id: t.name for t in TENANTS}
T0 = 1_791_815_912.0
SID = "srv-1791815912-1"


def ev(stage, dt, detail="", **kw):
    return {"type": "incident", "incident_id": kw.pop("iid", SID), "stage": stage, "ts": T0 + dt,
            "detail": detail, **kw}


def snap(slow):
    return {"type": "state", "ts": T0, "mode": "tenantguard", "tenants": [], "utilization": 0.4, "fairness": 0.9,
            "status_sentence": "x",
            "servers": [{"server": s, "p95_ms": 120.0, "peer_ratio": r, "cpu_pct": 90.0, "status": "slow"}
                        for s, r in slow.items()]}


DETECTED = "Slow servers: w1 16.9x its peers (123 ms vs 7 ms, by traffic); w3 17.3x its peers (126 ms vs 7 ms, by traffic); w8 17.1x its peers (124 ms vs 7 ms, by traffic)."
RESCUED = ("Moved 9 tenants off w1, w3, w8 onto their healthy servers, keeping every server under 70% CPU: "
           "A -> w0 47%, w2 53%; B -> w4 50%, w5 50%.")
NAMED = "Named KaamKaro (K) with 100% confidence: 3 of its 3 servers are slower than their peers (w1 16.9x, w3 17.8x, w8 17.1x); it cannot be routed around them."


def server_incident(n: Narrator, close: bool = False) -> None:
    n.ingest(ev("detected", 0, DETECTED))
    n.ingest(snap({"w1": 16.93, "w3": 17.31, "w8": 17.08}))
    n.ingest(ev("rescued", 0.5, RESCUED, action="RouteWeightsCmd 9 tenants ttl=5s"))
    n.ingest(ev("named", 1.0, NAMED, tenant="K", suspects=[{"tenant": "K", "confidence": 1.0}]))
    if close:
        n.ingest(ev("resolved", 150, "Route weights back to default: every server is back within its peers' latency.",
                    action="RouteWeightsCmd cleared for 9 tenants"))
        n.ingest(ev("closed", 151, "Server incident closed after 150 s: every server is back within its peers' latency."))


def pack_of(close=False):
    n = Narrator(TENANTS)
    server_incident(n, close)
    p = n.book.pack(SID)
    return p, anonymise(p)


# ---------- evidence + template ----------
def test_evidence_pack_contents():
    p, _ = pack_of()
    assert p["kind"] == "server" and p["tenant"] == "K" and p["tenant_name"] == "KaamKaro"
    assert [s["stage"] for s in p["stages"]] == ["detected", "rescued", "named"]
    assert p["suspects"] == [{"tenant": "K", "name": "KaamKaro", "confidence_pct": 100}]
    # only the detected servers: the rescue text also names healthy targets (w0, w2, w4, w5), which must not count
    assert {s["server"]: s["peer_ratio"] for s in p["slow_servers"]} == {"w1": 16.93, "w3": 17.31, "w8": 17.08}
    assert p["actions"] == [{"time": hhmmss(T0 + 0.5), "action": "RouteWeightsCmd 9 tenants ttl=5s", "ttl_s": 5.0}]
    assert p["affected"] == ["A", "B", "C", "D", "E", "F", "G", "H", "I"]


def test_template_report_says_the_right_things():
    p, anon = pack_of(close=True)
    rep, cust = template.build(p, anon)
    assert rep.headline == "Servers w1, w3, w8 slowed down; KaamKaro (K) named as the cause; closed after 151 s"
    assert rep.timeline[0].startswith(hhmmss(T0) + " Detected: Slow servers: w1 16.9x")
    assert "named with 100% confidence" in rep.cause
    assert "w1 at 16.93x its peers' latency" in rep.evidence_points
    assert "RouteWeightsCmd 9 tenants ttl=5s" in rep.action_taken
    assert rep.operator_recommendation.startswith("No action needed")
    # every server and tenant the report names comes from the evidence
    text = " ".join([rep.headline, *rep.timeline, rep.cause, *rep.evidence_points, rep.action_taken])
    assert "JhatpatMart" not in text and "w5 at" not in text


def test_customer_message_is_anonymised():
    p, anon = pack_of(close=True)
    text = json.dumps(anon) + template.customer_message(anon).message
    for tid, name in NAMES.items():
        assert name not in text
    assert "(K)" not in text and "w1" not in text and "tenant K" not in text
    msg = template.customer_message(anon).message
    assert "another tenant" in msg and "moved to healthy servers" in msg and "back to normal" in msg


def test_tenant_ladder_incident_template():
    n = Narrator(TENANTS)
    iid = "inc-1791815504-C-2"
    n.ingest(ev("detected", 0, "Possible noisy neighbour: Chitra Studio (C) uses 75% of the CPU", iid=iid, tenant="C",
                level="warn"))
    n.ingest(ev("level_change", 3, "Escalated warn -> throttle", iid=iid, tenant="C", level="throttle",
                prev_level="warn", action="LimitsCmd factor=0.50 weight_factor=1.00 ttl=60s"))
    n.ingest(ev("closed", 30, "Incident closed after 30 s (worst level: throttle).", iid=iid, tenant="C"))
    v = n.reports[iid]
    assert v.report.headline == "Chitra Studio (C) used more than its fair share (peak level: throttle); closed after 30 s"
    assert v.evidence["actions"][0]["ttl_s"] == 60.0 and v.version == 1 and v.trigger_stage == "closed"
    # live-run regression: a ladder incident is about usage, never "named from the pattern of slow servers"
    assert "pattern of slow servers" not in v.report.cause and "usage-share" in v.report.cause
    assert v.evidence["slow_servers"] == []
    assert "Chitra Studio (C) uses 75% of the CPU" in " ".join(v.report.evidence_points)


def test_replayed_events_do_not_republish():
    n = Narrator(TENANTS)
    server_incident(n)
    v1 = n.reports[SID].version
    server_incident(n)                                     # WS reconnect backfill: same events again
    assert n.reports[SID].version == v1


# ---------- API ----------
def test_api_and_ws():
    n = Narrator(TENANTS)
    app = create_app(n, NarratorSettings(), follow_brain=False)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/reports") as ws:
            server_incident(n)
            first = ReportView.model_validate_json(ws.receive_text())
        lst = c.get("/reports").json()
        one = c.get(f"/reports/{SID}")
        missing = c.get("/reports/nope")
        health = c.get("/healthz").json()
    assert first.incident_id == SID and first.version == 1
    assert [r["incident_id"] for r in lst] == [SID]
    assert one.status_code == 200 and ReportView.model_validate(one.json()).trigger_stage == "named"
    assert missing.status_code == 404
    assert health["reports"] == 1 and health["reports_published"] == 1


def test_versions_increase_only_when_the_evidence_changes():
    n = Narrator(TENANTS)
    server_incident(n)
    assert n.reports[SID].version == 1 and n.reports[SID].trigger_stage == "named"
    n.ingest(ev("resolved", 150, "Route weights back to default.", action="RouteWeightsCmd cleared for 9 tenants"))
    n.ingest(ev("closed", 151, "Server incident closed after 150 s."))
    v = n.reports[SID]
    assert v.version == 3 and v.trigger_stage == "closed" and v.created_at <= v.updated_at
    assert v.report.headline.endswith("closed after 151 s")


def test_report_store_is_bounded():
    n = Narrator(TENANTS, max_reports=2)
    for i in range(4):
        iid = f"srv-{i}"
        n.ingest(ev("detected", i, DETECTED, iid=iid))
        n.ingest(ev("named", i + 0.5, NAMED, iid=iid, tenant="K", suspects=[{"tenant": "K", "confidence": 1.0}]))
    assert len(n.reports) == 2


def test_bad_messages_are_ignored():
    n = Narrator(TENANTS)
    assert n.ingest({"type": "incident", "garbage": True}) is None
    assert n.ingest("nope") is None
    assert n.ingest({"type": "state"}) is None
    assert n.reports == {}
