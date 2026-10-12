"""The AI writer: one structured-output call per report version. Never on the decision path.

Uses the official `anthropic` SDK (AsyncAnthropic). The system prompt is frozen and sent first with a
cache breakpoint; the evidence goes in the user turn. Output is constrained with a JSON schema and then
validated into the contract models. Any refusal, API error, timeout or invalid output returns None and
the caller keeps the template.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from pydantic import ValidationError
from tenantguard_common.contracts import CustomerMessage, IncidentReport

log = logging.getLogger("tenantguard.narrator.ai")

SYSTEM_PROMPT = """You write incident reports for TenantGuard, a control plane that keeps tenants of a shared \
multi-tenant service from slowing each other down. TenantGuard has already detected, contained and logged \
the incident; your job is only to explain it clearly. You never decide or recommend anything drastic on your own.

You receive two JSON documents:
1. EVIDENCE: the full evidence pack for one incident (stages with IST clock times, suspects with confidence, \
slow servers with peer ratios, control actions with TTLs, affected tenant ids, verdict).
2. CUSTOMER_EVIDENCE: an anonymised subset that is safe to show to an affected customer.

Write two things:
- report: for the on-call operator. A one-line headline; a timeline (one line per stage, oldest first, \
starting with its HH:MM:SS time); the cause in one or two sentences; evidence points; the action taken; one \
operator recommendation. Use tenant ids in parentheses after names, e.g. "KaamKaro (K)".
- customer_message: three or four plain sentences for an affected customer, written from CUSTOMER_EVIDENCE \
only. Never name or hint at any other tenant, any tenant id or any server.

Hard rules (a checker rejects your output otherwise):
- Every tenant id, tenant name, server id (w0..w8, spare, quarantine), clock time and number you write must \
appear in the evidence you were given. Do not compute new numbers (no sums, averages, differences or \
percentages that are not already there); you may round a number that is there.
- If the evidence does not say something, do not say it. Prefer fewer, exact statements.
- Plain English, no markdown."""


def _schema() -> dict:
    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    s = {"type": "string"}
    arr = {"type": "array", "items": {"type": "string"}}
    return obj({
        "report": obj({"headline": s, "timeline": arr, "cause": s, "evidence_points": arr,
                       "action_taken": s, "operator_recommendation": s}),
        "customer_message": obj({"message": s}),
    })


SCHEMA = _schema()


@dataclass
class AIResult:
    report: Optional[IncidentReport]
    customer: Optional[CustomerMessage]
    status: str            # done | refusal | error | invalid
    detail: str = ""
    model: Optional[str] = None


class AIWriter:
    def __init__(self, client: Any, model: str, effort: str = "low", max_tokens: int = 4096,
                 timeout_s: float = 30.0):
        self.client, self.model, self.effort = client, model, effort
        self.max_tokens, self.timeout_s = max_tokens, timeout_s

    def request(self, pack: dict, anon: dict) -> dict:
        user = ("EVIDENCE:\n" + json.dumps(pack, sort_keys=True, ensure_ascii=False)
                + "\n\nCUSTOMER_EVIDENCE:\n" + json.dumps(anon, sort_keys=True, ensure_ascii=False))
        return dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            # Server-side refusal fallback: on a policy decline the API re-runs the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    async def write(self, pack: dict, anon: dict) -> AIResult:
        import anthropic                                   # imported lazily: template mode needs no SDK
        try:
            resp = await self.client.with_options(timeout=self.timeout_s, max_retries=1).beta.messages.create(
                **self.request(pack, anon))
        except anthropic.APIStatusError as e:
            return AIResult(None, None, "error", f"API {e.status_code}: {e.message}"[:300])
        except anthropic.APIConnectionError as e:          # includes timeouts
            return AIResult(None, None, "error", f"connection: {type(e).__name__}")
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            return AIResult(None, None, "refusal", f"refusal ({cat})", resp.model)
        if resp.stop_reason == "max_tokens":
            return AIResult(None, None, "invalid", "output truncated (max_tokens)", resp.model)
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        try:
            data = json.loads(text or "")
            report = IncidentReport.model_validate(data["report"])
            customer = CustomerMessage.model_validate(data["customer_message"])
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            return AIResult(None, None, "invalid", f"invalid output: {type(e).__name__}", resp.model)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            log.info("ai report: model=%s in=%s cache_read=%s out=%s", resp.model, usage.input_tokens,
                     getattr(usage, "cache_read_input_tokens", None), usage.output_tokens)
        return AIResult(report, customer, "done", "", resp.model)
