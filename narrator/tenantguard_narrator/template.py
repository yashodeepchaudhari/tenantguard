"""Deterministic reports, built only from the evidence pack (instant, always grounded)."""
from __future__ import annotations

from typing import Tuple

from tenantguard_common.contracts import CustomerMessage, IncidentReport

STAGE_WORDS = {"detected": "Detected", "level_change": "Level change", "rescued": "Victims rescued", "named": "Named",
               "proving": "Proving", "verdict": "Verdict", "resolved": "Resolved", "explained": "Explained",
               "closed": "Closed"}


def _who(p: dict) -> str:
    return f"{p['tenant_name']} ({p['tenant']})" if p.get("tenant_name") else (p.get("tenant") or "no single tenant")


def _short(text: str, limit: int = 160) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def report(p: dict) -> IncidentReport:
    who = _who(p)
    slow = ", ".join(s["server"] for s in p["slow_servers"]) or "none recorded"
    if p["kind"] == "server":
        headline = (f"Servers {slow} slowed down; {who} named as the cause" if p.get("tenant")
                    else f"Servers {slow} slowed down; no tenant blamed")
    else:
        headline = f"{who} used more than its fair share (peak level: {p.get('peak_level') or 'warn'})"
    if p["closed"]:
        headline += f"; closed after {p['duration_s']:g} s"

    timeline = [f"{s['time']} {STAGE_WORDS.get(s['stage'], s['stage'])}"
                + (f" ({s['level']})" if s["stage"] == "level_change" and s.get("level") else "")
                + (f": {_short(s['detail'])}" if s.get("detail") else "") for s in p["stages"]]

    named = next((s for s in p["suspects"]), None) if p["kind"] == "server" else None
    cause = f"Verdict: {p['verdict'].replace('_', ' ')}. " if p.get("verdict") else ""
    if named:
        cause += (f"{named['name'] or named['tenant']} ({named['tenant']}) was named with "
                  f"{named['confidence_pct']}% confidence from the pattern of slow servers ({slow}).")
    elif p["kind"] == "tenant":
        cause += (f"{who} used more CPU or dispatch share than its fair share while its neighbours were slower "
                  f"than their calm baseline (usage-share detector).")
    else:
        cause += "No tenant's server pattern explains the slowdown: a host fault is likely."

    evidence = [f"{s['server']} at {s['peer_ratio']:g}x its peers' latency" if s.get("peer_ratio") else
                f"{s['server']} reported slow" for s in p["slow_servers"]]
    if p["kind"] == "server":
        evidence += [f"{s['name'] or s['tenant']} ({s['tenant']}) suspected at {s['confidence_pct']}% confidence"
                     for s in p["suspects"]]
    else:
        first = next((s for s in p["stages"] if s["stage"] == "detected"), None)
        if first and first.get("detail"):
            evidence.append(_short(first["detail"], 220))
    if p["affected"]:
        evidence.append(f"Tenants sharing its servers: {', '.join(p['affected'])}")
    if not evidence:
        evidence.append("See the timeline: the brain's own measurements for each step.")

    if p["actions"]:
        action = "; ".join(f"{a['time']} {a['action']}" for a in p["actions"])
    else:
        action = "No control action was needed."

    if p["closed"]:
        rec = "No action needed: the incident closed and every override expires by its TTL."
    elif p.get("tenant") and p["kind"] == "server":
        rec = (f"Review {who}'s recent request pattern; approve a TTL-bound block only if the slowdown "
               f"persists after the rescue.")
    elif p.get("tenant"):
        rec = f"Watch {who}; the throttle ladder recovers it automatically once the over-use stops."
    else:
        rec = f"Check the host of {slow}; drain it if the slowdown persists."
    return IncidentReport(headline=headline, timeline=timeline, cause=cause, evidence_points=evidence,
                          action_taken=action, operator_recommendation=rec)


def customer_message(anon: dict) -> CustomerMessage:
    when = f"from {anon['opened']}" + (f" to {anon['last_event']}" if anon["closed"] else "")
    steps = []
    if anon["rescued"]:
        steps.append("your traffic was moved to healthy servers within seconds")
    if any("limited" in a for a in anon["actions"]):
        steps.append("the cause was limited automatically")
    done = "; ".join(steps) if steps else "the platform contained it automatically"
    status = "Service is back to normal." if anon["closed"] else "We are still monitoring it."
    return CustomerMessage(message=(
        f"Some of your requests were slower than usual {when} IST because {anon['cause']}. "
        f"TenantGuard detected it and {done}. Your data was never affected. {status}"))


def build(pack: dict, anon: dict) -> Tuple[IncidentReport, CustomerMessage]:
    return report(pack), customer_message(anon)
