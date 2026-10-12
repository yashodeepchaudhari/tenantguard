"""T20 review fixes: fail-closed partner auth, no duplicate decision requests, bounded memory/payloads,
per-message isolation on the brain stream."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from tenantguard_common.config import load_tenants
from tenantguard_common.contracts import IncidentEvent

from tenantguard_handoff import bridge as bridge_mod
from tenantguard_handoff import wire
from tenantguard_handoff.app import create_app
from tenantguard_handoff.cases import CaseBook
from tenantguard_handoff.settings import HandoffSettings

from test_bridge_api import Control, Web, incident, make  # noqa: E402

T0 = 1_791_800_000.0


def book(**kw) -> CaseBook:
    return CaseBook(HandoffSettings(**kw), load_tenants())


def ev(stage, ts, level=None, prev=None, iid="inc-1-K-1", tenant="K", detail="d"):
    return IncidentEvent(incident_id=iid, stage=stage, ts=ts, tenant=tenant, level=level, prev_level=prev,
                         detail=detail)


def holds(b: CaseBook, cid="INC-0001"):
    c = b.cases[cid]
    return [a for a in c.view.actions if c.kinds[a.action_id] == "hold"]


def blocks(b: CaseBook, cid="INC-0001"):
    c = b.cases[cid]
    return [a for a in c.view.actions if c.kinds[a.action_id] == "block"]


def decide(b: CaseBook, action_id: str, decision: str, now: float):
    return b.on_decision(wire.CaseUpdateDecision(case_id="INC-0001", type="decision", action_id=action_id,
                                                 decision=decision, by="Rahul"), now)


# ---------------------------------------------------------------- #1 fail closed
def _post_update(app, headers=None):
    body = {"case_id": "INC-0001", "type": "owner", "owner": "R", "time": "10:00:00"}

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as c:
            return [await c.post(p, json=body, headers=headers or {}) for p in ("/control/case-update",
                                                                               "/cases/update")]
    return asyncio.run(go())


@pytest.mark.parametrize("headers", [None, {"X-Handoff-Token": ""}, {"X-Handoff-Token": "anything"}])
def test_no_partner_token_configured_rejects_every_update(tmp_path, monkeypatch, headers):
    monkeypatch.delenv("HANDOFF_PARTNER_TOKEN", raising=False)
    ctl = Control()
    b = make(tmp_path, Web(), ctl)
    b.ingest(incident("detected", 1.0))
    b.ingest(incident("level_change", 2.0, level="quarantine"))
    for r in _post_update(create_app(b, start=False), headers):
        assert r.status_code == 503 and "partner token not configured" in r.json()["detail"]
    assert ctl.got == []                                   # nothing reached the gateway or the brain
    assert b.book.get("INC-0001").owner is None
    asyncio.run(b.stop())


def test_wrong_token_401_right_token_200(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_PARTNER_TOKEN", "p4ss")
    b = make(tmp_path, Web(), Control())
    b.ingest(incident("detected", 1.0))
    app = create_app(b, start=False)
    assert [r.status_code for r in _post_update(app, {"X-Handoff-Token": "nope"})] == [401, 401]
    assert [r.status_code for r in _post_update(app, {"X-Handoff-Token": "p4ss"})] == [200, 200]
    asyncio.run(b.stop())


# ---------------------------------------------------------------- #2 no duplicate decision requests
def test_approved_hold_is_not_requested_again_by_echoes_or_recovery_steps():
    b = book()
    b.on_event(ev("detected", T0, level="warn"), T0)
    b.on_event(ev("level_change", T0 + 2, level="throttle", prev="warn"), T0 + 2)
    assert [a.action_id for a in holds(b)] == ["A1"]
    _, cmds = decide(b, "A1", "approve", T0 + 3)
    assert cmds and holds(b)[0].state == "extended"
    # the brain's operator-extend echo, then recovery steps, all at the same level
    b.on_event(ev("level_change", T0 + 3.1, level="throttle", prev="throttle"), T0 + 3.1)
    for i in range(3):
        b.on_event(ev("level_change", T0 + 10 + i, level="throttle", prev="throttle"), T0 + 10 + i)
    assert [a.action_id for a in holds(b)] == ["A1"]
    assert not any(a.state == "pending" for a in b.get("INC-0001").actions)
    assert b.get("INC-0001").status == "in_progress"       # nothing waiting for a decision


def test_rejected_requests_are_not_re_asked_on_renewals_but_escalation_asks_again():
    b = book()
    b.on_event(ev("detected", T0, level="warn"), T0)
    b.on_event(ev("level_change", T0 + 1, level="quarantine", prev="warn"), T0 + 1)
    assert [a.action_id for a in holds(b)] == ["A1"] and [a.action_id for a in blocks(b)] == ["A2"]
    decide(b, "A1", "reject", T0 + 2)
    decide(b, "A2", "reject", T0 + 2)
    for i in range(4):                                    # quarantine renewals / recovery steps
        b.on_event(ev("level_change", T0 + 5 + i, level="quarantine", prev="quarantine"), T0 + 5 + i)
    assert len(holds(b)) == 1 and len(blocks(b)) == 1
    b.on_event(ev("level_change", T0 + 20, level="throttle", prev="quarantine"), T0 + 20)   # de-escalation
    assert len(holds(b)) == 1
    b.on_event(ev("level_change", T0 + 30, level="blocked", prev="throttle"), T0 + 30)      # escalation
    assert len(holds(b)) == 2 and holds(b)[-1].state == "pending"


def test_pending_hold_is_refreshed_not_duplicated():
    b = book(ttl_throttle_s=60)
    b.on_event(ev("detected", T0, level="warn"), T0)
    b.on_event(ev("level_change", T0 + 1, level="throttle", prev="warn"), T0 + 1)
    b.on_event(ev("level_change", T0 + 30, level="throttle", prev="throttle"), T0 + 30)
    (a1,) = holds(b)
    assert a1.state == "pending" and a1.deadline_at == T0 + 90


# ---------------------------------------------------------------- #3 bounded memory and payloads
def test_wire_history_capped_with_earlier_events_marker():
    b = book(history_wire_max=50, history_keep=80)
    b.on_event(ev("detected", T0, level="warn"), T0)
    for i in range(119):                                   # 1 + 119 = 120 history items in total
        b.on_event(ev("level_change", T0 + 1 + i, level="warn", prev="warn"), T0 + 1 + i)
    c = b.cases["INC-0001"]
    assert len(c.view.history) == 80 and c.dropped == 40   # memory bound
    w = b.to_wire("INC-0001")
    assert len(w.history) == 51
    assert w.history[0].event == "70 earlier events"
    assert w.history[-1].event == c.view.history[-1].event
    wire.Case.model_validate(w.model_dump())               # still the strict partner format


def test_closed_cases_evicted_after_retention_and_never_resurrected():
    b = book(retention_s=3600)
    b.on_event(ev("detected", T0, level="warn"), T0)
    b.on_event(ev("closed", T0 + 10, level="normal"), T0 + 10)
    b.on_event(ev("detected", T0, level="warn", iid="inc-2-C-1", tenant="C"), T0)      # still open
    assert b.evict(T0 + 10 + 3599) == []
    assert b.evict(T0 + 10 + 3600) == ["INC-0001"]
    assert "INC-0001" not in b.cases and "INC-0002" in b.cases
    # a WS reconnect backfills old events: the evicted incident must not come back
    assert b.on_event(ev("level_change", T0 + 5, level="throttle", prev="warn"), T0 + 4000) is None
    assert "INC-0001" not in b.cases


def test_id_map_is_capped_but_live_cases_keep_their_ids():
    b = book(retention_s=0, id_map_max=3)
    for i in range(6):
        iid = f"inc-{i}-K-1"
        b.on_event(ev("detected", T0 + i, level="warn", iid=iid), T0 + i)
        if i < 5:
            b.on_event(ev("closed", T0 + i + 0.5, level="normal", iid=iid), T0 + i + 0.5)
        b.evict(T0 + i + 1)
    assert len(b.by_incident) <= 3 and len(b.retired) <= 3
    assert b.by_incident["inc-5-K-1"] == "INC-0006"        # the live case kept its mapping
    assert b.next_seq == 7


def test_bridge_tick_evicts_forgets_delivery_and_persists(tmp_path):
    async def go():
        b = make(tmp_path, Web(), Control(), retention_s=0)
        b.outbox.start()
        b.ingest(incident("detected", 1.0))
        b.ingest(incident("closed", 2.0, level="normal"))
        for _ in range(200):
            if not b.outbox.pending:
                break
            await asyncio.sleep(0.01)
        b.tick(10_000_000_000.0)
        await b.stop()
        return b
    b = asyncio.run(go())
    assert b.book.cases == {} and "INC-0001" not in b.outbox.delivered
    saved = json.loads((tmp_path / "ids.json").read_text(encoding="utf-8"))
    assert saved["next_seq"] == 2


# ---------------------------------------------------------------- #5 per-message isolation, backoff
def test_poison_message_is_skipped_and_counted(tmp_path):
    b = make(tmp_path, Web(), Control())
    real = b.book.on_event

    def on_event(e, now):
        if e.detail == "poison":
            raise RuntimeError("boom")
        return real(e, now)

    b.book.on_event = on_event
    assert b.ingest_safe("{not json") is None
    assert b.ingest_safe(json.dumps(incident("detected", 1.0, detail="poison"))) is None
    assert b.ingest_safe(json.dumps(incident("detected", 2.0))) == "INC-0001"
    assert b.bad_messages == 2 and "RuntimeError" in b.last_bad
    assert b.health()["bad_messages"] == 2
    asyncio.run(b.stop())


def test_stream_survives_poison_and_backoff_only_resets_after_a_stable_connection(tmp_path, monkeypatch):
    frames = [json.dumps(incident("detected", 1.0)), json.dumps(incident("detected", 1.5, detail="poison")),
              "{garbage", json.dumps(incident("level_change", 2.0, level="throttle"))]
    connects = []

    class FakeWS:
        def __init__(self):
            self.frames = list(frames) if not connects[1:] else []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.frames:
                return self.frames.pop(0)
            raise StopAsyncIteration

    def fake_connect(url, **kw):
        connects.append(url)
        return FakeWS()

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(bridge_mod.websockets, "connect", fake_connect)
    monkeypatch.setattr(bridge_mod.asyncio, "sleep", fake_sleep)
    b = make(tmp_path, Web(), Control())
    real = b.book.on_event
    b.book.on_event = lambda e, now: (_ for _ in ()).throw(RuntimeError("boom")) if e.detail == "poison" \
        else real(e, now)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(b._consume_brain())
    v = b.book.get("INC-0001")
    assert v is not None and any("throttle" in h.event for h in v.history)   # frames after the poison applied
    assert b.bad_messages == 2
    # each connection was short-lived: delays keep doubling instead of resetting to 0.5 s
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_next_backoff():
    nb = bridge_mod.next_backoff
    assert nb(0.5, None, 100.0) == 1.0                     # never connected
    assert nb(4.0, 99.0, 100.0) == 8.0                     # connected 1 s: still growing
    assert nb(8.0, 0.0, 100.0) == bridge_mod.RECONNECT_START_S   # a stable connection resets it
    assert nb(10.0, None, 0.0) == bridge_mod.RECONNECT_MAX_S
