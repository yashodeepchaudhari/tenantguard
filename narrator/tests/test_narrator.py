from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import List

import pytest
from fastapi.testclient import TestClient
from tenantguard_common.config import load_tenants
from tenantguard_common.contracts import CustomerMessage, IncidentReport, ReportView

from tenantguard_narrator import grounding, template
from tenantguard_narrator.ai import SCHEMA, SYSTEM_PROMPT, AIWriter
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


def test_template_report_is_grounded_and_says_the_right_things():
    p, anon = pack_of(close=True)
    rep, cust = template.build(p, anon)
    assert rep.headline == "Servers w1, w3, w8 slowed down; KaamKaro (K) named as the cause; closed after 151 s"
    assert rep.timeline[0].startswith(hhmmss(T0) + " Detected: Slow servers: w1 16.9x")
    assert "named with 100% confidence" in rep.cause
    assert "w1 at 16.93x its peers' latency" in rep.evidence_points
    assert "RouteWeightsCmd 9 tenants ttl=5s" in rep.action_taken
    assert rep.operator_recommendation.startswith("No action needed")
    ok, why = grounding.check(rep, cust, p, anon, NAMES)
    assert ok, why                                     # the template itself always passes the checker


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
    assert v.evidence["actions"][0]["ttl_s"] == 60.0 and v.ai_status == "disabled" and v.source == "template"
    # live-run regression: a ladder incident is about usage, never "named from the pattern of slow servers"
    assert "pattern of slow servers" not in v.report.cause and "usage-share" in v.report.cause
    assert v.evidence["slow_servers"] == []
    assert "Chitra Studio (C) uses 75% of the CPU" in " ".join(v.report.evidence_points)


# ---------- grounding ----------
def _ai_ok(p):
    rep = IncidentReport(
        headline="KaamKaro (K) slowed servers w1, w3 and w8; neighbours were rescued",
        timeline=[f"{hhmmss(T0)} Detected: w1, w3 and w8 were about 17x slower than their peers",
                  f"{hhmmss(T0 + 0.5)} Rescued: 9 tenants moved to healthy servers",
                  f"{hhmmss(T0 + 1.0)} Named: KaamKaro (K) with 100% confidence"],
        cause="A tenant, KaamKaro (K), could not be routed around: all 3 of its servers were slow.",
        evidence_points=["w1 at 16.9x its peers", "Tenants A, B and E share its servers"],
        action_taken="Route weights moved 9 tenants away for 5 s, renewed while needed.",
        operator_recommendation="Review KaamKaro's request pattern before approving any block.")
    cust = CustomerMessage(message="Some of your requests were slower for a short while because another tenant "
                                   "used far more than its fair share. Your traffic was moved to healthy servers.")
    return rep, cust


def test_grounding_accepts_a_faithful_ai_report():
    p, anon = pack_of()
    rep, cust = _ai_ok(p)
    assert grounding.check(rep, cust, p, anon, NAMES) == (True, "")


@pytest.mark.parametrize("field,bad,reason", [
    ("cause", "JhatpatMart (J) caused it.", "tenant JhatpatMart"),
    ("cause", "Tenant L was involved.", "tenant id L"),
    ("headline", "w6 slowed down", "server w6"),
    ("headline", "latency rose by 37.5 ms", "number 37.5"),
    ("headline", "25x slower than peers", "number 25"),
    ("headline", "about 18x slower", None),
    ("headline", "at 09:99:99", "time"),
])
def test_grounding_rejects_unknown_tenants_servers_numbers_times(field, bad, reason):
    p, anon = pack_of()
    rep, cust = _ai_ok(p)
    rep = rep.model_copy(update={field: bad})
    ok, why = grounding.check(rep, cust, p, anon, NAMES)
    if reason is None:                                   # 18 is a rounding of 17.8 from the evidence: allowed
        assert ok, why
    else:
        assert not ok and reason in why, why


def test_grounding_rejects_customer_message_naming_anyone():
    p, anon = pack_of()
    rep, _ = _ai_ok(p)
    for msg in ("KaamKaro caused this.", "Tenant K caused this.", "Server w1 was slow."):
        ok, why = grounding.check(rep, CustomerMessage(message=msg), p, anon, NAMES)
        assert not ok and why.startswith("customer message"), msg


# ---------- AI writer with a stubbed SDK client ----------
class StubMessages:
    def __init__(self, outputs: List[dict], delay: float = 0.0):
        self.outputs, self.delay, self.calls = outputs, delay, []

    async def create(self, **kw):
        self.calls.append(kw)
        delay = self.delay[len(self.calls) - 1] if isinstance(self.delay, list) else self.delay
        if delay:
            await asyncio.sleep(delay)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(stop_reason=out.get("stop_reason", "end_turn"), model="claude-opus-5-5",
                               stop_details=SimpleNamespace(category=out.get("category")),
                               content=[SimpleNamespace(type="text", text=out.get("text", ""))],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


class StubClient:
    def __init__(self, outputs, delay=0.0):
        self.messages = StubMessages(outputs, delay)
        self.beta = SimpleNamespace(messages=self.messages)

    def with_options(self, **_):
        return self


def ai_json(rep: IncidentReport, cust: CustomerMessage) -> dict:
    return {"text": json.dumps({"report": rep.model_dump(), "customer_message": cust.model_dump()})}


def test_request_shape_cached_system_prompt_effort_schema_fallback():
    w = AIWriter(StubClient([]), "claude-opus-5-5", "low", 4096, 30)
    p, anon = pack_of()
    req = w.request(p, anon)
    assert req["model"] == "claude-opus-5-5"
    assert req["system"] == [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    assert req["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert SCHEMA["properties"]["report"]["additionalProperties"] is False
    assert req["messages"][0]["content"].startswith("EVIDENCE:\n{")
    assert w.request(p, anon) == req                     # deterministic: same pack, same bytes (cache-friendly)


def run_flow(outputs, delay=0.0, extra=None):
    async def go():
        client = StubClient(outputs, delay)
        n = Narrator(TENANTS, AIWriter(client, "claude-opus-5-5"))
        seen = []
        q = asyncio.Queue(maxsize=64)
        n.subscribers.add(q)
        server_incident(n)
        if extra:
            await asyncio.sleep(0.01)
            extra(n)
        await n.drain()
        while not q.empty():
            seen.append(ReportView.model_validate_json(q.get_nowait()))
        return n, seen, client
    return asyncio.run(go())


def test_template_first_then_grounded_ai_version():
    p, anon = pack_of()
    n, seen, client = run_flow([ai_json(*_ai_ok(p))])
    assert [(v.version, v.source, v.ai_status) for v in seen] == [(1, "template", "pending"), (2, "ai", "done")]
    assert seen[1].grounded and seen[1].model == "claude-opus-5-5"
    assert seen[1].report.headline.startswith("KaamKaro (K) slowed")
    assert len(client.messages.calls) == 1


def test_ungrounded_ai_output_is_rejected_and_template_kept():
    p, anon = pack_of()
    rep, cust = _ai_ok(p)
    n, seen, _ = run_flow([ai_json(rep.model_copy(update={"cause": "JhatpatMart (J) did it"}), cust)])
    last = seen[-1]
    assert last.source == "template" and last.ai_status == "rejected" and "JhatpatMart" in last.ai_detail
    assert n.stats["ai_rejected"] == 1


@pytest.mark.parametrize("out,detail", [
    ({"stop_reason": "refusal", "category": "cyber"}, "refusal (cyber)"),
    ({"text": "not json"}, "invalid output"),
    ({"stop_reason": "max_tokens", "text": "{"}, "truncated"),
])
def test_refusal_or_invalid_output_keeps_template(out, detail):
    n, seen, _ = run_flow([out])
    assert seen[-1].source == "template" and seen[-1].ai_status == "error" and detail in seen[-1].ai_detail


def test_api_error_keeps_template():
    import anthropic
    import httpx2
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    n, seen, _ = run_flow([err])
    assert seen[-1].source == "template" and seen[-1].ai_status == "error" and "connection" in seen[-1].ai_detail


def test_stale_ai_result_is_dropped_when_incident_changes():
    p, anon = pack_of()
    good = ai_json(*_ai_ok(p))
    # the first AI call is slow; meanwhile the incident closes, so a second (fresh) AI call runs
    # first call is slow (0.5 s), the second immediate: robust even on a loaded machine
    n, seen, client = run_flow([good, good], delay=[0.5, 0.0],
                               extra=lambda n: n.ingest(ev("closed", 151, "Server incident closed after 150 s.")))
    assert len(client.messages.calls) == 2
    ai_versions = [v for v in seen if v.source == "ai"]
    assert len(ai_versions) == 1 and ai_versions[0].trigger_stage == "closed"


def test_no_key_mode_is_template_only(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = NarratorSettings.from_dict({"narrator": {"model": "claude-opus-5-5"}})
    assert s.ai_enabled is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert NarratorSettings.from_dict({}).ai_enabled is True
    monkeypatch.setenv("NARRATOR_AI", "0")
    assert NarratorSettings.from_dict({}).ai_enabled is False


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
    assert first.incident_id == SID and first.source == "template"
    assert [r["incident_id"] for r in lst] == [SID]
    assert one.status_code == 200 and ReportView.model_validate(one.json()).ai_status == "disabled"
    assert missing.status_code == 404
    assert health["ai_enabled"] is False and health["reports"] == 1
