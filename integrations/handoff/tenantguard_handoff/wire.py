"""The partner's wire format (agreed with the Web PS3 team; see docs/convergence/).

This is an EXTERNAL API, so it lives only here, in the bridge. Times are "HH:MM:SS" wall-clock IST strings,
as agreed with the Web team; everything inside TenantGuard uses epoch floats (see timefmt.py, which converts).

  TenantGuard -> Web: POST {WEB_URL}/cases                 body = Case
  Web -> TenantGuard: POST {bridge}/control/case-update    body = CaseUpdateOwner | CaseUpdateDecision
"""
from __future__ import annotations

from typing import List, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

HHMMSS = r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$"
CASE_ID = r"^INC-\d{4}$"

TenantId = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
TENANT_IDS = set(TenantId.__args__)  # type: ignore[attr-defined]
Verdict = Literal["costly_pattern", "over_plan", "honest_growth", "host_fault", "mitigated_no_blame"]
Severity = Literal["low", "medium", "high"]
CaseStatus = Literal["waiting_for_decision", "in_progress", "resolved", "closed"]
Decision = Literal["approve", "reject", "extend", "release", "resolve"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- TenantGuard -> Web ----------
class CaseContext(_Strict):
    verdict: Verdict
    affected: List[TenantId]
    evidence_link: str
    summary: str


class PendingAction(_Strict):
    action_id: str
    text: str
    deadline: str = Field(pattern=HHMMSS)


class HistoryItem(_Strict):
    time: str = Field(pattern=HHMMSS)
    event: str


class Case(_Strict):
    case_id: str = Field(pattern=CASE_ID)
    title: str
    severity: Severity
    status: CaseStatus
    context: CaseContext
    pending_actions: List[PendingAction]
    history: List[HistoryItem]


# ---------- Web -> TenantGuard ----------
class CaseUpdateOwner(_Strict):
    case_id: str = Field(pattern=CASE_ID)
    type: Literal["owner"] = "owner"
    owner: str = Field(min_length=1, max_length=80)
    time: str = Field(pattern=HHMMSS)


class CaseUpdateDecision(_Strict):
    case_id: str = Field(pattern=CASE_ID)
    type: Literal["decision"] = "decision"
    action_id: str = Field(min_length=1, max_length=16)
    decision: Decision
    by: str = Field(min_length=1, max_length=80)


CaseUpdate = Union[CaseUpdateOwner, CaseUpdateDecision]


def parse_case_update(body: dict) -> CaseUpdate:
    """Discriminated by `type`; raises pydantic.ValidationError on anything else."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    if body.get("type") == "owner":
        return CaseUpdateOwner.model_validate(body)
    return CaseUpdateDecision.model_validate(body)
