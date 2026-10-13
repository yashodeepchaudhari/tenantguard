from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from tenantguard_common.config import load_tenants
from tenantguard_common.contracts import BlockCmd, CaseView, IncidentEvent, OperatorCmd

from tenantguard_handoff import wire
from tenantguard_handoff.cases import CaseBook, DecisionError
from tenantguard_handoff.settings import HandoffSettings
from tenantguard_handoff.timefmt import from_ist, ist

CONV = Path(__file__).resolve().parents[3] / "docs" / "convergence"
T0 = 1_791_800_000.0          # a fixed epoch; IST display is derived from it


def book(**kw) -> CaseBook:
    return CaseBook(HandoffSettings(**kw), load_tenants())


def ev(stage, tenant="K", ts=T0, level=None, prev=None, detail="", iid="inc-1-K-1", verdict=None):
    return IncidentEvent(incident_id=iid, stage=stage, ts=ts, tenant=tenant, level=level, prev_level=prev,
                         detail=detail, verdict=verdict)


# ---------- wire: every sample file round-trips ----------
@pytest.mark.parametrize("f", sorted((CONV / "cases").glob("*.json")), ids=lambda p: p.name)
def test_sample_cases_round_trip(f):
    raw = json.loads(f.read_text(encoding="utf-8"))
    case = wire.Case.model_validate(raw)
    assert case.model_dump() == raw


def test_sample_case_updates_round_trip_and_cover_all_decisions():
    examples = json.loads((CONV / "case-update-examples.json").read_text(encoding="utf-8"))["examples"]
    seen = set()
    for ex in examples:
        upd = wire.parse_case_update(ex["body"])
        assert upd.model_dump() == ex["body"]
        seen.add(getattr(upd, "decision", "owner"))
    assert seen == {"owner", "approve", "reject", "extend", "release", "resolve"}


@pytest.mark.parametrize("body", [
    {"case_id": "INC-0104", "type": "decision", "action_id": "A1", "decision": "maybe", "by": "x"},
    {"case_id": "INC-0104", "type": "owner", "owner": "Rahul", "time": "25:00:00"},
    {"case_id": "INC-104", "type": "owner", "owner": "Rahul", "time": "10:00:00"},
    {"case_id": "INC-0104", "type": "owner", "owner": "Rahul", "time": "10:00:00", "extra": 1},
])
def test_bad_updates_rejected(body):
    with pytest.raises(ValidationError):
        wire.parse_case_update(body)


def test_ist_round_trip_and_midnight_wrap():
    assert ist(0) == "05:30:00"
    t = T0 + 37
    assert from_ist(ist(t), t) == pytest.approx(int(t))
    ref = from_ist("23:59:50", T0)
    assert from_ist("00:00:10", ref) == pytest.approx(ref + 20)      # just after midnight = next day


# ---------- event -> case ----------
def test_detected_creates_case_with_neighbours_and_evidence_link():
    b = book(ui_url="http://10.0.0.5:5173")
    cid = b.on_event(ev("detected", detail="K uses 3.1x its fair CPU share"), T0)
    v = b.get(cid)
    assert cid == "INC-0001" and v.incident_id == "inc-1-K-1" and v.tenant == "K"
    assert v.context.evidence_link == "http://10.0.0.5:5173/case/inc-1-K-1"
    # v2: K's neighbours are the other tenants of its project (mobility: E, G, K)
    assert v.context.affected == ["E", "G"]
    assert v.severity == "low" and v.status == "in_progress" and v.context.verdict == "over_plan"
    assert "KaamKaro" in v.title or "(K)" in v.title
    assert v.history[0].event.startswith("Detected")


def test_level_changes_create_pending_actions_with_ttl_deadlines():
    b = book(ttl_throttle_s=60, ttl_quarantine_s=120, decision_window_s=120)
    b.on_event(ev("detected"), T0)
    cid = b.on_event(ev("level_change", level="throttle", prev="warn", ts=T0 + 2), T0 + 2)
    v = b.get(cid)
    assert v.severity == "medium" and v.status == "waiting_for_decision"
    (a1,) = v.actions
    assert a1.action_id == "A1" and a1.state == "pending" and a1.deadline_at == T0 + 62
    assert a1.deadline_ist == ist(T0 + 62)
    b.on_event(ev("level_change", level="quarantine", prev="deprioritize", ts=T0 + 9), T0 + 9)
    assert v.severity == "high"
    assert [a.action_id for a in v.actions] == ["A1", "A2"]
    assert v.actions[0].deadline_at == T0 + 9 + 120              # hold re-armed with the quarantine TTL
    assert "blocking" in v.actions[1].text and v.actions[1].deadline_at == T0 + 9 + 120


def test_replayed_events_are_ignored_and_recovery_closes_actions():
    b = book()
    b.on_event(ev("detected"), T0)
    b.on_event(ev("level_change", level="throttle", ts=T0 + 1), T0 + 1)
    n = len(b.get("INC-0001").history)
    assert b.on_event(ev("level_change", level="throttle", ts=T0 + 1), T0 + 5) is None
    assert len(b.get("INC-0001").history) == n
    b.on_event(ev("level_change", level="normal", prev="throttle", ts=T0 + 30), T0 + 30)
    assert b.get("INC-0001").actions[0].state == "expired"
    b.on_event(ev("resolved", ts=T0 + 31, detail="K back to full plan"), T0 + 31)
    assert b.get("INC-0001").status == "resolved"
    b.on_event(ev("closed", ts=T0 + 40), T0 + 40)
    assert b.get("INC-0001").status == "closed"


def test_verdict_from_brain_and_second_incident_gets_next_id():
    b = book()
    b.on_event(ev("detected"), T0)
    b.on_event(ev("verdict", verdict="costly_pattern", ts=T0 + 3), T0 + 3)
    assert b.get("INC-0001").context.verdict == "costly_pattern"
    assert b.on_event(ev("detected", tenant="C", iid="inc-9-C-2"), T0) == "INC-0002"


def test_deadline_expiry_applies_safe_default():
    b = book(ttl_throttle_s=60)
    b.on_event(ev("detected"), T0)
    b.on_event(ev("level_change", level="throttle", ts=T0 + 1), T0 + 1)
    assert b.expire(T0 + 60) == []
    assert b.expire(T0 + 61) == ["INC-0001"]
    v = b.get("INC-0001")
    assert v.actions[0].state == "expired" and v.status == "in_progress"
    assert "safe default" in v.history[-1].event


def test_to_wire_is_strict_partner_format():
    b = book()
    b.on_event(ev("detected", detail="x"), T0)
    b.on_event(ev("level_change", level="quarantine", ts=T0 + 1), T0 + 1)
    w = b.to_wire("INC-0001")
    raw = w.model_dump()
    assert wire.Case.model_validate(raw) == w
    assert [p["action_id"] for p in raw["pending_actions"]] == ["A1", "A2"]
    assert all(len(h["time"]) == 8 for h in raw["history"])
    assert "deadline_at" not in json.dumps(raw) and "time_ist" not in json.dumps(raw)


# ---------- decision -> control ----------
def _quarantined(**kw):
    b = book(ttl_quarantine_s=120, block_s=300, extend_s=120, ttl_max_s=3600, **kw)
    b.on_event(ev("detected"), T0)
    b.on_event(ev("level_change", level="quarantine", ts=T0 + 1), T0 + 1)
    return b


def D(action, decision, by="Rahul", cid="INC-0001"):
    return wire.CaseUpdateDecision(case_id=cid, action_id=action, decision=decision, by=by)


def test_hold_extend_and_release_map_to_operator_cmd():
    b = _quarantined()
    _, cmds = b.on_decision(D("A1", "extend"), T0 + 5)
    (c,) = cmds
    assert c.target == "brain" and c.path == "/api/operator"
    assert c.body == OperatorCmd(tenant="K", decision="extend", until_s=120, by="Rahul", case_id="INC-0001")
    assert b.get("INC-0001").actions[0].state == "extended"
    _, cmds = b.on_decision(D("A1", "release", by="Priya"), T0 + 6)
    assert cmds[0].body.decision == "release" and cmds[0].body.by == "Priya"
    assert b.get("INC-0001").actions[0].state == "released"
    with pytest.raises(DecisionError) as e:
        b.on_decision(D("A1", "extend"), T0 + 7)
    assert e.value.status == 409


def test_block_approve_is_ttl_bound_and_release_unblocks():
    b = _quarantined()
    b.s.block_s = 99999                                   # never longer than ttl_max_s
    _, cmds = b.on_decision(D("A2", "approve"), T0 + 5)
    (c,) = cmds
    assert c.target == "gateway" and c.path == "/control/block"
    assert c.body == BlockCmd(tenant="K", blocked=True, ttl_s=3600, incident_id="inc-1-K-1")
    _, cmds = b.on_decision(D("A2", "release"), T0 + 6)
    assert cmds[0].body.blocked is False


def test_reject_sends_nothing_and_resolve_clears_bridge_controls():
    b = _quarantined()
    _, cmds = b.on_decision(D("A1", "reject"), T0 + 2)
    assert cmds == [] and b.get("INC-0001").actions[0].state == "rejected"
    b.on_decision(D("A2", "approve"), T0 + 3)
    _, cmds = b.on_decision(D("A2", "resolve"), T0 + 4)
    assert [(c.target, getattr(c.body, "blocked", None)) for c in cmds] == [("gateway", False)]
    v = b.get("INC-0001")
    assert v.status == "resolved" and v.actions[1].state == "resolved"


def test_unknown_case_or_action_is_404_and_owner_is_recorded():
    b = _quarantined()
    with pytest.raises(DecisionError) as e:
        b.on_decision(D("A1", "approve", cid="INC-9999"), T0)
    assert e.value.status == 404
    with pytest.raises(DecisionError) as e:
        b.on_decision(D("A7", "approve"), T0)
    assert e.value.status == 404
    with pytest.raises(DecisionError):
        b.on_owner(wire.CaseUpdateOwner(case_id="INC-9999", owner="Rahul", time="10:00:00"), T0)
    b.on_owner(wire.CaseUpdateOwner(case_id="INC-0001", owner="Priya", time=ist(T0 + 3)), T0 + 3)
    b.on_owner(wire.CaseUpdateOwner(case_id="INC-0001", owner="Rahul", time=ist(T0 + 9)), T0 + 9)
    v = b.get("INC-0001")
    assert v.owner == "Rahul" and v.owner_since_at == pytest.approx(int(T0 + 9)) and v.owner_since_ist == ist(T0 + 9)
    assert v.history[-1].event == "Owner: Rahul acknowledged (handed over from Priya)"
    CaseView.model_validate(v.model_dump())


def test_hash_split_case_adopts_its_culprit_and_closes():
    from tenantguard_common.contracts import Suspect
    b = book()
    sid = "srv-1791815503-2"
    start = ev("detected", tenant=None, iid=sid, detail="Project commerce: 119 ms/s of CPU that no tenant's requests "
                                                         "explain. Hash split test: B, J are pinned.")
    start.report = {"project": "commerce", "suspects": ["B", "J"]}
    cid = b.on_event(start, T0)
    v = b.get(cid)
    assert v.tenant is None and v.severity == "medium" and v.actions == []
    assert v.title == "Hidden CPU in project commerce: hash split test"
    rnd = ev("proving", tenant=None, iid=sid, ts=T0 + 12, detail="Split round 1: kept B")
    rnd.report = {"project": "commerce", "round": 1, "kept": ["B"]}
    b.on_event(rnd, T0 + 12)
    found = ev("verdict", tenant="B", iid=sid, ts=T0 + 12.5, detail="Hash split found B")
    found.suspects = [Suspect(tenant="B", confidence=1.0)]
    found.report = {"verdict": "costly_pattern", "project": "commerce"}
    b.on_event(found, T0 + 12.5)
    assert v.tenant == "B" and v.title == "BazaarKart (B) found behind project commerce's hidden CPU (100% confidence)"
    assert v.context.verdict == "costly_pattern" and v.context.affected == ["J"]
    assert v.actions == []                                           # the ladder's own case asks, if it escalates
    b.on_event(ev("closed", tenant=None, iid=sid, ts=T0 + 12.5, detail="Split test over"), T0 + 12.6)
    assert v.status == "closed"
    wire.Case.model_validate(b.to_wire(cid).model_dump())


def test_host_wide_slowdown_case_blames_nobody():
    b = book()
    hid = "srv-1791815503-1"
    first = ev("detected", tenant=None, iid=hid, detail="All 5 active projects are slow. Nobody is blamed.")
    first.report = {"projects": ["commerce", "free", "health", "media", "mobility"]}
    cid = b.on_event(first, T0)
    no_blame = ev("verdict", tenant=None, iid=hid, detail="All 5 active projects are slow. Nobody is blamed.")
    no_blame.report = {"verdict": "host_fault", "projects": ["commerce"]}
    b.on_event(no_blame, T0 + 0.1)
    v = b.get(cid)
    assert v.tenant is None and v.actions == [] and v.context.verdict == "host_fault"
    assert v.title.startswith("All active projects slowed down together")
    b.on_event(ev("closed", tenant=None, iid=hid, ts=T0 + 40, detail="The host-wide slowdown is over."), T0 + 40)
    assert v.status == "closed"
