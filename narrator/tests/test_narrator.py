from __future__ import annotations

import json

from fastapi.testclient import TestClient
from tenantguard_common.config import load_tenants
from tenantguard_common.contracts import ReportView

from tenantguard_narrator import template
from tenantguard_narrator.app import create_app
from tenantguard_narrator.evidence import anonymise, hhmmss
from tenantguard_narrator.service import Narrator
from tenantguard_narrator.settings import NarratorSettings

TENANTS = load_tenants()
NAMES = {t.id: t.name for t in TENANTS}
T0 = 1_791_815_912.0
SID = "srv-1791815912-2"          # a hash split of project commerce (the brain's v2 event shapes)
HID = "srv-1791815912-1"          # a host-wide slowdown


def ev(stage, dt, detail="", **kw):
    return {"type": "incident", "incident_id": kw.pop("iid", SID), "stage": stage, "ts": T0 + dt,
            "detail": detail, **kw}


SPLIT_START = ("Project commerce: 119 ms/s of CPU that no tenant's requests explain, for 10 s, while its tenants "
               "are slowed. Hash split test: B, J are pinned to two replicas, half each.")
ROUND1 = {"project": "commerce", "round": 1, "halves": [["J"], ["B"]], "kept": ["B"],
          "unattributed_ms_per_s": {"tg-commerce-1": 36.07, "tg-commerce-2": 62.17}}


def split_case(n: Narrator, close: bool = True) -> None:
    n.ingest(ev("detected", 0, SPLIT_START, report={"project": "commerce", "suspects": ["B", "J"]}))
    n.ingest(ev("proving", 12.7, "Split round 1: kept B", report=ROUND1))
    n.ingest(ev("verdict", 12.7, "Hash split found B: the unexplained CPU of project commerce followed it.",
                tenant="B", suspects=[{"tenant": "B", "confidence": 1.0}],
                action="unexplained CPU of project commerce charged to B",
                report={"verdict": "costly_pattern", "project": "commerce", "rounds": [ROUND1]}))
    if close:
        n.ingest(ev("closed", 12.7, "Split test over: pins removed."))


def host_case(n: Narrator, close: bool = True) -> None:
    projects = ["commerce", "free", "health", "media", "mobility"]
    detail = "All 5 active projects are slow while each stays within its CPU allocation. Nobody is blamed."
    n.ingest(ev("detected", 0, detail, iid=HID, report={"projects": projects}))
    n.ingest(ev("verdict", 0, detail, iid=HID, action="nobody throttled",
                report={"verdict": "host_fault", "projects": projects}))
    if close:
        n.ingest(ev("closed", 40, "The host-wide slowdown is over.", iid=HID))


def pack_of(iid=SID, close=True):
    n = Narrator(TENANTS)
    (split_case if iid == SID else host_case)(n, close)
    p = n.book.pack(iid)
    return p, anonymise(p)


# ---------- evidence + template ----------
def test_split_pack_contents():
    p, _ = pack_of()
    assert p["kind"] == "split" and p["tenant"] == "B" and p["tenant_name"] == "BazaarKart"
    assert p["project"] == "commerce" and p["verdict"] == "costly_pattern"
    assert [s["stage"] for s in p["stages"]] == ["detected", "proving", "verdict", "closed"]
    assert p["rounds"] == [{"round": 1, "halves": [["J"], ["B"]], "kept": ["B"],
                            "unattributed_ms_per_s": {"tg-commerce-1": 36.07, "tg-commerce-2": 62.17}}]
    assert p["suspects"] == [{"tenant": "B", "name": "BazaarKart", "confidence_pct": 100}]
    assert p["affected"] == ["J"]                                      # B's project peers


def test_split_report_says_the_right_things():
    p, anon = pack_of()
    rep, _ = template.build(p, anon)
    assert rep.headline == "Hidden CPU in project commerce traced to BazaarKart (B) in 1 split round; closed after 12.7 s"
    assert rep.timeline[1].startswith(hhmmss(T0 + 12.7) + " Split round: Split round 1: kept B")
    assert "followed BazaarKart (B)'s half of project commerce in every round" in rep.cause
    assert ("Round 1: {J} vs {B}; unexplained CPU tg-commerce-1 36.07 ms/s, tg-commerce-2 62.17 ms/s; kept B"
            in rep.evidence_points)
    assert "Tenants in the same project: J" in rep.evidence_points
    assert "charged to B" in rep.action_taken
    assert rep.operator_recommendation.startswith("Review BazaarKart (B)'s background work")
    text = " ".join([rep.headline, *rep.timeline, rep.cause, *rep.evidence_points, rep.action_taken])
    assert "server" not in text.lower() and "KaamKaro" not in text


def test_host_case_blames_nobody():
    p, anon = pack_of(HID)
    rep, cust = template.build(p, anon)
    assert p["kind"] == "host" and p["tenant"] is None and p["verdict"] == "host_fault"
    assert rep.headline == "5 active projects slowed down together; no tenant blamed; closed after 40 s"
    assert "outside the projects" in rep.cause
    assert "Projects slowed: commerce, free, health, media, mobility" in rep.evidence_points
    assert rep.operator_recommendation.startswith("No action needed: the slowdown is over")
    assert "infrastructure" in cust.message and "limited the cause" not in cust.message


def test_customer_message_is_anonymised():
    p, anon = pack_of()
    msg = template.customer_message(anon).message
    text = json.dumps(anon) + msg
    for name in NAMES.values():
        assert name not in text
    assert "commerce" not in text and "(B)" not in text and "tenant B" not in text
    assert "another tenant sharing your capacity ran heavy background work" in msg
    assert "limited the cause automatically" in msg and "back to normal" in msg
    assert "server" not in msg


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
    assert v.evidence["kind"] == "tenant" and v.evidence["project"] == "free"
    assert v.evidence["actions"][0]["ttl_s"] == 60.0 and v.version == 1 and v.trigger_stage == "closed"
    assert "usage-share" in v.report.cause and "project free" in v.report.cause
    assert "Chitra Studio (C) uses 75% of the CPU" in " ".join(v.report.evidence_points)
    assert "Tenants in the same project: F, I" in v.report.evidence_points


def test_replayed_events_do_not_republish():
    n = Narrator(TENANTS)
    split_case(n)
    v1 = n.reports[SID].version
    split_case(n)                                          # WS reconnect backfill: same events again
    assert n.reports[SID].version == v1


# ---------- API ----------
def test_api_and_ws():
    n = Narrator(TENANTS)
    app = create_app(n, NarratorSettings(), follow_brain=False)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/reports") as ws:
            split_case(n, close=False)
            first = ReportView.model_validate_json(ws.receive_text())
        lst = c.get("/reports").json()
        one = c.get(f"/reports/{SID}")
        missing = c.get("/reports/nope")
        health = c.get("/healthz").json()
    assert first.incident_id == SID and first.version == 1
    assert [r["incident_id"] for r in lst] == [SID]
    assert one.status_code == 200 and ReportView.model_validate(one.json()).trigger_stage == "verdict"
    assert missing.status_code == 404
    assert health["reports"] == 1 and health["reports_published"] == 1


def test_versions_increase_only_when_the_evidence_changes():
    n = Narrator(TENANTS)
    split_case(n, close=False)
    assert n.reports[SID].version == 1 and n.reports[SID].trigger_stage == "verdict"
    n.ingest(ev("closed", 13, "Split test over: pins removed."))
    v = n.reports[SID]
    assert v.version == 2 and v.trigger_stage == "closed" and v.created_at <= v.updated_at
    assert v.report.headline.endswith("closed after 13 s")


def test_report_store_is_bounded():
    n = Narrator(TENANTS, max_reports=2)
    for i in range(4):
        iid = f"srv-{i}"
        n.ingest(ev("detected", i, SPLIT_START, iid=iid, report={"project": "commerce"}))
        n.ingest(ev("verdict", i + 0.5, "found B", iid=iid, tenant="B", suspects=[{"tenant": "B", "confidence": 1.0}]))
    assert len(n.reports) == 2


def test_bad_messages_are_ignored():
    n = Narrator(TENANTS)
    assert n.ingest({"type": "incident", "garbage": True}) is None
    assert n.ingest("nope") is None
    assert n.ingest({"type": "state"}) is None
    assert n.reports == {}
