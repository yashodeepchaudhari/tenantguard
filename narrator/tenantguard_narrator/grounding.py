"""Grounding check: the AI may only say what the evidence pack says.

Every tenant id or name, server id, clock time and number in the AI report must appear in the evidence
pack (a number may be a rounding of one). The customer message is checked against the anonymised pack
and must not name any tenant at all.
"""
from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Set, Tuple

from tenantguard_common.contracts import CustomerMessage, IncidentReport

TIME_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")
SERVER_RE = re.compile(r"\b(w\d+|spare|quarantine)\b")
NUM_RE = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?")
# A standalone capital letter is a tenant id, except "A" / "I" used as ordinary words ("A tenant", "I").
LETTER_RE = re.compile(r"(?<![A-Za-z0-9_'-])([A-Z])(?![A-Za-z0-9_'-])")


def _texts_report(r: IncidentReport) -> str:
    return "\n".join([r.headline, *r.timeline, r.cause, *r.evidence_points, r.action_taken,
                      r.operator_recommendation])


def _numbers(text: str) -> List[str]:
    return NUM_RE.findall(text)


def _is_rounding_of(n: str, pool: Iterable[float]) -> bool:
    v = float(n)
    dec = len(n.split(".")[1]) if "." in n else 0
    tol = 0.5 * 10 ** (-dec) + 1e-9
    return any(abs(p - v) <= tol for p in pool)


def _letters(text: str) -> Set[str]:
    out = set()
    for m in LETTER_RE.finditer(text):
        c = m.group(1)
        nxt = text[m.end():m.end() + 2]
        if c in ("A", "I") and (nxt[:1] == " " and nxt[1:2].islower() or nxt[:1] in ("", "\n")):
            continue                                        # the English word, not a tenant id
        out.add(c)
    return out


def _strip(text: str) -> str:
    text = TIME_RE.sub(" ", text)
    return SERVER_RE.sub(" ", text)


def check_text(text: str, source: dict, names: Dict[str, str], allowed_tenants: Set[str]) -> Tuple[bool, str]:
    """Generic check of `text` against everything in `source` (a pack). Returns (ok, reason)."""
    src = json.dumps(source, sort_keys=True, ensure_ascii=False)
    src_times = set(TIME_RE.findall(src))
    src_servers = set(SERVER_RE.findall(src))
    src_numbers = [float(x) for x in _numbers(_strip(src))]
    for t in TIME_RE.findall(text):
        if t not in src_times:
            return False, f"ungrounded time {t}"
    for s in SERVER_RE.findall(text):
        if s not in src_servers:
            return False, f"ungrounded server {s}"
    for tid, name in names.items():
        if name and name in text and tid not in allowed_tenants:
            return False, f"ungrounded tenant {name}"
    for c in _letters(text):
        if c not in allowed_tenants:
            return False, f"ungrounded tenant id {c}"
    for n in _numbers(_strip(text)):
        if not _is_rounding_of(n, src_numbers):
            return False, f"ungrounded number {n}"
    return True, ""


def check(report: IncidentReport, customer: CustomerMessage, pack: dict, anon: dict,
          names: Dict[str, str]) -> Tuple[bool, str]:
    """`names`: tenant id -> name for every tenant in the layout."""
    allowed = {t for t in [pack.get("tenant"), *pack.get("affected", []),
                           *(s["tenant"] for s in pack.get("suspects", []))] if t}
    ok, why = check_text(_texts_report(report), pack, names, allowed)
    if not ok:
        return False, f"report: {why}"
    ok, why = check_text(customer.message, anon, names, allowed_tenants=set())
    if not ok:
        return False, f"customer message: {why}"
    return True, ""
