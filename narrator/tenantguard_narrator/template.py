"""Deterministic reports, built only from the evidence pack (instant, always grounded)."""
from __future__ import annotations

from typing import Tuple

from tenantguard_common.contracts import CustomerMessage, IncidentReport

STAGE_WORDS = {"detected": "Detected", "level_change": "Level change", "rescued": "Victims rescued", "named": "Named",
               "proving": "Split round", "verdict": "Verdict", "resolved": "Resolved", "explained": "Explained",
               "closed": "Closed"}


def _who(p: dict) -> str:
    return f"{p['tenant_name']} ({p['tenant']})" if p.get("tenant_name") else (p.get("tenant") or "no single tenant")


def _short(text: str, limit: int = 160) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _round_point(r: dict) -> str:
    halves = " vs ".join("{" + ", ".join(h) + "}" for h in r["halves"]) or "two halves"
    cpu = ", ".join(f"{rep} {v:g} ms/s" for rep, v in sorted(r["unattributed_ms_per_s"].items()))
    return (f"Round {r['round']}: {halves}; unexplained CPU {cpu or 'not reported'}; "
            f"kept {', '.join(r['kept']) or 'nobody'}")


def _headline(p: dict, who: str) -> str:
    if p["kind"] == "host":
        n = len(p["projects"])
        return f"{n or 'All'} active projects slowed down together; no tenant blamed"
    if p["kind"] == "split":
        rounds = len(p["rounds"])
        if p.get("tenant"):
            return (f"Hidden CPU in project {p['project']} traced to {who} in {rounds} split "
                    f"round{'s' if rounds != 1 else ''}")
        return f"Hidden CPU in project {p['project']}: split test ended without a culprit"
    return f"{who} used more than its fair share (peak level: {p.get('peak_level') or 'warn'})"


def _cause(p: dict, who: str) -> str:
    cause = f"Verdict: {p['verdict'].replace('_', ' ')}. " if p.get("verdict") else ""
    if p["kind"] == "host":
        return cause + ("Every active project was slower than its calm baseline while each stayed within its CPU "
                        "allocation: the cause is outside the projects (host, disk or another machine).")
    if p["kind"] == "split" and p.get("tenant"):
        return cause + (f"CPU that no request explained followed {who}'s half of project {p['project']} in every "
                        "round, so that CPU is now charged to it; the ladder judges it from there.")
    if p["kind"] == "split":
        return cause + "No tenant could be tied to the hidden CPU, so nobody was blamed."
    return cause + (f"{who} used more CPU or dispatch share than its fair share of project {p['project']} while "
                    "its neighbours were slower than their calm baseline (usage-share detector).")


def _evidence(p: dict) -> list:
    if p["kind"] == "host":
        points = [f"Projects slowed: {', '.join(p['projects'])}"] if p["projects"] else []
    else:
        first = next((s for s in p["stages"] if s["stage"] == "detected"), None)
        points = [_short(first["detail"], 220)] if first and first.get("detail") else []
        points += [_round_point(r) for r in p["rounds"]]
    if p["affected"]:
        points.append(f"Tenants in the same project: {', '.join(p['affected'])}")
    return points or ["See the timeline: the brain's own measurements for each step."]


def _recommendation(p: dict, who: str) -> str:
    if p["kind"] == "host":
        return ("No action needed: the slowdown is over and nobody was throttled." if p["closed"] else
                "Check the host (CPU, disk, other workloads on the machine); TenantGuard throttles no tenant for it.")
    if p["kind"] == "split" and p.get("tenant"):
        return (f"Review {who}'s background work; the ladder throttles it automatically if its neighbours stay "
                "hurt, and every override expires by its TTL.")
    if p["kind"] == "split":
        return "No action needed: nobody was blamed. If the hidden CPU returns, the split test runs again."
    if p["closed"]:
        return "No action needed: the incident closed and every override expires by its TTL."
    return f"Watch {who}; the throttle ladder recovers it automatically once the over-use stops."


def report(p: dict) -> IncidentReport:
    who = _who(p)
    headline = _headline(p, who)
    if p["closed"]:
        headline += f"; closed after {p['duration_s']:g} s"
    timeline = [f"{s['time']} {STAGE_WORDS.get(s['stage'], s['stage'])}"
                + (f" ({s['level']})" if s["stage"] == "level_change" and s.get("level") else "")
                + (f": {_short(s['detail'])}" if s.get("detail") else "") for s in p["stages"]]
    action = ("; ".join(f"{a['time']} {a['action']}" for a in p["actions"]) if p["actions"]
              else "No control action was needed.")
    return IncidentReport(headline=headline, timeline=timeline, cause=_cause(p, who), evidence_points=_evidence(p),
                          action_taken=action, operator_recommendation=_recommendation(p, who))


def customer_message(anon: dict) -> CustomerMessage:
    when = f"from {anon['opened']}" + (f" to {anon['last_event']}" if anon["closed"] else "")
    if anon["limited"]:
        done = "limited the cause automatically, so your share of capacity was protected"
    elif anon["kind"] == "host":
        done = "kept every customer's share of capacity unchanged while it lasted"
    else:
        done = "contained it automatically"
    status = "Service is back to normal." if anon["closed"] else "We are still monitoring it."
    return CustomerMessage(message=(
        f"Some of your requests were slower than usual {when} IST because {anon['cause']}. "
        f"TenantGuard detected it and {done}. Your data was never affected. {status}"))


def build(pack: dict, anon: dict) -> Tuple[IncidentReport, CustomerMessage]:
    return report(pack), customer_message(anon)
