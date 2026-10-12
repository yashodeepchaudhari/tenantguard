from __future__ import annotations

import asyncio
import json
import time
from typing import List

import httpx
import pytest
from tenantguard_common.config import load_tenants
from tenantguard_common.contracts import H_CONTROL_TOKEN, CaseView, IncidentEvent

from tenantguard_handoff import wire
from tenantguard_handoff.app import create_app
from tenantguard_handoff.bridge import Bridge
from tenantguard_handoff.settings import HandoffSettings


class Web:
    """Fake Web app: records POST /cases; can be 'down' (503) or unreachable."""

    def __init__(self, mode: str = "up", delay_s: float = 0.0):
        self.mode, self.delay_s = mode, delay_s
        self.got: List[dict] = []

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.mode == "unreachable":
            raise httpx.ConnectError("refused", request=req)
        if self.mode == "down":
            return httpx.Response(503)
        body = json.loads(req.content)
        wire.Case.model_validate(body)
        self.got.append(body)
        return httpx.Response(200, json={"ok": True})


class Control:
    """Fake gateway + brain control endpoints."""

    def __init__(self, brain_status: int = 200):
        self.brain_status = brain_status
        self.got: List[httpx.Request] = []

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        self.got.append(req)
        if req.url.path == "/api/operator":
            return httpx.Response(self.brain_status, json={"ok": self.brain_status == 200})
        return httpx.Response(200, json={"ok": True, "detail": ""})


def make(tmp_path, web: Web, control: Control, **kw):
    s = HandoffSettings(web_url="http://web", brain_url="http://brain", gateway_url="http://gw",
                        state_path=str(tmp_path / "ids.json"), control_token="tok", **kw)
    b = Bridge(s, load_tenants(), control_client=httpx.AsyncClient(transport=httpx.MockTransport(control)),
               web_client=httpx.AsyncClient(transport=httpx.MockTransport(web)))
    b.outbox.backoff_start_s = b.outbox.backoff_max_s = 0.05
    return b


def incident(stage, ts, level=None, tenant="K", iid="inc-1-K-1", detail="d"):
    return json.loads(IncidentEvent(incident_id=iid, stage=stage, ts=ts, tenant=tenant, level=level,
                                    detail=detail).model_dump_json())


async def settle(b: Bridge, timeout: float = 2.0):
    end = time.monotonic() + timeout
    while b.outbox.pending and time.monotonic() < end:
        await asyncio.sleep(0.01)


def test_events_become_cases_pushed_to_web_latest_version_wins(tmp_path):
    web, ctl = Web(), Control()

    async def go():
        b = make(tmp_path, web, ctl)
        b.outbox.start()
        b.ingest({"type": "state", "ts": 1.0, "mode": "tenantguard", "tenants": [], "servers": [],
                  "utilization": 0, "fairness": 1, "status_sentence": "ok"})
        b.ingest(incident("detected", 1.0))
        b.ingest(incident("level_change", 2.0, level="throttle"))      # both before the outbox runs
        await settle(b)
        b.ingest({"type": "incident", "garbage": True})                   # bad message: ignored, no crash
        await b.stop()
        return b
    b = asyncio.run(go())
    assert b.mode == "tenantguard"
    assert [c["case_id"] for c in web.got] == ["INC-0001"]               # one POST: stale version superseded
    assert web.got[0]["status"] == "waiting_for_decision" and len(web.got[0]["pending_actions"]) == 1
    assert b.book.get("INC-0001").delivered_at is not None


def test_web_app_down_never_blocks_and_delivers_when_back(tmp_path):
    web, ctl = Web(mode="unreachable", delay_s=0.2), Control()

    async def go():
        b = make(tmp_path, web, ctl)
        b.outbox.start()
        t0 = time.perf_counter()
        for i in range(50):
            b.ingest(incident("level_change", float(i), level="throttle" if i % 2 else "deprioritize"))
        ingest_s = time.perf_counter() - t0
        await asyncio.sleep(0.3)
        assert web.got == [] and b.outbox.stats["failed"] >= 1
        assert b.book.get("INC-0001").delivered_at is None
        web.mode, web.delay_s = "up", 0.0
        await asyncio.sleep(0.2)
        await settle(b)
        await b.stop()
        return b, ingest_s
    b, ingest_s = asyncio.run(go())
    assert ingest_s < 0.5                                                  # 50 events, Web app down: no blocking
    assert len(web.got) == 1                                               # only the latest version is sent
    assert len(web.got[0]["history"]) == 50
    assert b.book.get("INC-0001").delivered_at is not None


def test_case_update_api_owner_decision_404_400_and_control_token(tmp_path):
    web, ctl = Web(), Control(brain_status=404)                            # brain has no /api/operator yet

    async def go():
        b = make(tmp_path, web, ctl, ttl_quarantine_s=120, block_s=300)
        app = create_app(b, start=False)
        b.ingest(incident("detected", 1.0))
        b.ingest(incident("level_change", 2.0, level="quarantine"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as c:
            out = {
                "owner": await c.post("/control/case-update", json={"case_id": "INC-0001", "type": "owner",
                                                                     "owner": "Rahul", "time": "15:03:30"}),
                "view": await c.get("/cases/INC-0001"),
                "approve_block": await c.post("/cases/update", json={"case_id": "INC-0001", "type": "decision",
                                                                    "action_id": "A2", "decision": "approve",
                                                                    "by": "Rahul"}),
                "extend_hold": await c.post("/control/case-update", json={
                    "case_id": "INC-0001", "type": "decision", "action_id": "A1", "decision": "extend", "by": "Rahul"}),
                "again": await c.post("/control/case-update", json={
                    "case_id": "INC-0001", "type": "decision", "action_id": "A2", "decision": "approve", "by": "x"}),
                "unknown": await c.post("/control/case-update", json={
                    "case_id": "INC-0777", "type": "decision", "action_id": "A1", "decision": "approve", "by": "x"}),
                "unknown_get": await c.get("/cases/INC-0777"),
                "bad": await c.post("/control/case-update", json={"case_id": "INC-0001", "type": "decision",
                                                                  "action_id": "A1", "decision": "maybe", "by": "x"}),
                "notjson": await c.post("/control/case-update", content=b"{nope",
                                        headers={"content-type": "application/json"}),
                "list": await c.get("/cases"),
                "wire": await c.get("/cases/INC-0001/wire"),
            }
        await b.stop()
        return out
    o = asyncio.run(go())
    assert o["owner"].status_code == 200 and o["view"].json()["owner"] == "Rahul"
    assert o["view"].json()["owner_since_ist"] == "15:03:30"
    a = o["approve_block"].json()
    assert o["approve_block"].status_code == 200 and a["commands"][0]["status"] == 200
    gw_req = next(r for r in ctl.got if r.url.path == "/control/block")
    assert gw_req.url.host == "gw" and gw_req.headers[H_CONTROL_TOKEN] == "tok"
    assert json.loads(gw_req.content) == {"incident_id": "inc-1-K-1", "ttl_s": 300.0, "tenant": "K", "blocked": True}
    e = o["extend_hold"].json()
    assert e["commands"][0]["path"] == "/api/operator" and e["commands"][0]["status"] == 404
    assert o["again"].status_code == 409
    assert o["unknown"].status_code == 404 and o["unknown_get"].status_code == 404
    assert o["bad"].status_code == 400 and o["notjson"].status_code == 400
    view = CaseView.model_validate(o["list"].json()[0])
    events = [h.event for h in view.history]
    assert any(ev.startswith("TenantGuard applied /control/block") for ev in events)
    assert any("Best-effort: brain not ready" in ev for ev in events)
    assert wire.Case.model_validate(o["wire"].json()).case_id == "INC-0001"


def test_partner_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_PARTNER_TOKEN", "p4ss")
    web, ctl = Web(), Control()

    async def go():
        b = make(tmp_path, web, ctl)
        app = create_app(b, start=False)
        b.ingest(incident("detected", 1.0))
        body = {"case_id": "INC-0001", "type": "owner", "owner": "R", "time": "10:00:00"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as c:
            r1 = await c.post("/control/case-update", json=body)
            r2 = await c.post("/control/case-update", json=body, headers={"X-Handoff-Token": "p4ss"})
        await b.stop()
        return r1, r2
    r1, r2 = asyncio.run(go())
    assert r1.status_code == 401 and r2.status_code == 200


def test_case_ids_survive_a_restart(tmp_path):
    async def run_once(iid):
        b = make(tmp_path, Web(), Control())
        cid = b.ingest(incident("detected", 1.0, iid=iid))
        await b.stop()
        return cid
    assert asyncio.run(run_once("inc-1-K-1")) == "INC-0001"
    assert asyncio.run(run_once("inc-2-C-1")) == "INC-0002"            # numbering continues after restart
    assert asyncio.run(run_once("inc-1-K-1")) == "INC-0001"            # same incident keeps its case id
