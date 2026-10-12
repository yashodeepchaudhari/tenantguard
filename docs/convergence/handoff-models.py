"""PROPOSAL: TenantGuard <-> Web PS3 (Digital Handoff) contract models.

Not part of common/ yet. The coordinator moves these into
common/tenantguard_common/contracts.py at the start of Wave 3.

Matches docs/convergence/cases/*.json (TenantGuard -> POST {WEB_URL}/cases) and
docs/convergence/case-update-examples.json (Web -> POST {TENANTGUARD_URL}/control/case-update).

Unlike the rest of contracts.py, times here are wall-clock "HH:MM:SS" strings
(same day, IST), as agreed with the Web team, not unix epoch floats.

Self-check (no pytest):  python docs/convergence/handoff-models.py
"""
from __future__ import annotations

from typing import List, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

HHMMSS = r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$"

TenantId = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
Verdict = Literal["costly_pattern", "over_plan", "honest_growth", "host_fault", "mitigated_no_blame"]
Severity = Literal["low", "medium", "high"]
CaseStatus = Literal["waiting_for_decision", "in_progress", "resolved", "closed"]
Decision = Literal["approve", "reject", "extend", "release", "resolve"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- TenantGuard -> Web: POST {WEB_URL}/cases ----------

class CaseContext(_Strict):
    verdict: Verdict
    affected: List[TenantId]            # victim tenant IDs; may be empty
    evidence_link: str                  # http://<ui-host>:5173/case/<incident_id>
    summary: str                        # plain English, offender named only in operator-facing cases


class PendingAction(_Strict):
    action_id: str                      # "A1", "A2", ... unique within a case
    text: str
    deadline: str = Field(pattern=HHMMSS)


class HistoryItem(_Strict):
    time: str = Field(pattern=HHMMSS)
    event: str


class Case(_Strict):
    case_id: str = Field(pattern=r"^INC-\d{4}$")   # = TenantGuard incident_id; re-sent on every change
    title: str
    severity: Severity
    status: CaseStatus
    context: CaseContext
    pending_actions: List[PendingAction]
    history: List[HistoryItem]


# ---------- Web -> TenantGuard: POST {TENANTGUARD_URL}/control/case-update ----------

class CaseUpdateOwner(_Strict):
    case_id: str = Field(pattern=r"^INC-\d{4}$")
    type: Literal["owner"] = "owner"
    owner: str
    time: str = Field(pattern=HHMMSS)


class CaseUpdateDecision(_Strict):
    case_id: str = Field(pattern=r"^INC-\d{4}$")
    type: Literal["decision"] = "decision"
    action_id: str
    decision: Decision
    by: str


CaseUpdate = Union[CaseUpdateOwner, CaseUpdateDecision]   # discriminated by `type`


def parse_case_update(body: dict) -> CaseUpdate:
    if body.get("type") == "owner":
        return CaseUpdateOwner.model_validate(body)
    return CaseUpdateDecision.model_validate(body)


# ---------- self-check: python docs/convergence/handoff-models.py ----------

if __name__ == "__main__":
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parent
    case_files = sorted((here / "cases").glob("*.json"))
    assert len(case_files) == 5, f"expected 5 case files, found {len(case_files)}"

    verdicts = set()
    for f in case_files:
        case = Case.model_validate(json.loads(f.read_text(encoding="utf-8")))
        ids = [a.action_id for a in case.pending_actions]
        assert len(ids) == len(set(ids)), f"{f.name}: duplicate action_id"
        assert case.context.evidence_link.endswith("/case/" + case.case_id), f"{f.name}: evidence_link mismatch"
        verdicts.add(case.context.verdict)
        print(f"ok  {f.name:32} {case.case_id} {case.context.verdict}")
    assert len(verdicts) == 5, f"expected one case per verdict, got {sorted(verdicts)}"

    examples = json.loads((here / "case-update-examples.json").read_text(encoding="utf-8"))["examples"]
    decisions = set()
    owners = 0
    for ex in examples:
        upd = parse_case_update(ex["body"])
        if isinstance(upd, CaseUpdateOwner):
            owners += 1
            detail = upd.owner
        else:
            decisions.add(upd.decision)
            detail = upd.decision
        print(f"ok  case-update {upd.case_id} {upd.type} {detail}")
    assert owners >= 1, "missing owner example"
    assert decisions == {"approve", "reject", "extend", "release", "resolve"}, f"decision coverage: {sorted(decisions)}"

    # negative checks: strict models must reject bad input
    bad_inputs = [
        (Case, {**json.loads(case_files[0].read_text(encoding="utf-8")), "unexpected": 1}),
        (CaseUpdateDecision, {"case_id": "INC-0104", "type": "decision", "action_id": "A1", "decision": "maybe", "by": "x"}),
        (CaseUpdateOwner, {"case_id": "INC-0104", "type": "owner", "owner": "Rahul", "time": "25:00:00"}),
    ]
    for model, body in bad_inputs:
        try:
            model.model_validate(body)
        except Exception:
            continue
        raise AssertionError(f"{model.__name__} accepted invalid input: {body}")

    print(f"ALL OK: {len(case_files)} cases, {len(examples)} case-updates, {len(bad_inputs)} rejections")
