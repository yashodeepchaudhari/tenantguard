"""Mock of the Web PS3 "Digital Handoff" app, for demos without the partner team.

  python tools/mock_web.py serve [--port 8500]               receive POST /cases, print each version
  python tools/mock_web.py owner INC-0001 Rahul              send an owner (acknowledge) update
  python tools/mock_web.py decide INC-0001 A1 approve Rahul  send a decision (approve|reject|extend|release|resolve)
  python tools/mock_web.py down|up                           make the mock answer 503 (simulate an outage) / recover

The bridge URL for updates comes from env HANDOFF_URL (default http://127.0.0.1:8400). Updates carry the
partner token from env HANDOFF_PARTNER_TOKEN as X-Handoff-Token (the bridge refuses updates without it).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tenantguard_handoff import wire  # noqa: E402
from tenantguard_handoff.timefmt import ist  # noqa: E402

HANDOFF_URL = os.environ.get("HANDOFF_URL", "http://127.0.0.1:8400")
MOCK_URL = os.environ.get("WEB_URL", "http://127.0.0.1:8500")


def partner_headers() -> Dict[str, str]:
    """The partner token header for updates sent to the bridge; exits with a clear message if it is not set."""
    token = os.environ.get("HANDOFF_PARTNER_TOKEN", "")
    if not token:
        raise SystemExit("HANDOFF_PARTNER_TOKEN is not set: the bridge refuses partner updates without it. "
                         "Set it to the same value the bridge uses.")
    return {wire.PARTNER_TOKEN_HEADER: token}


def make_app():
    app = FastAPI(title="mock Digital Handoff")
    cases: Dict[str, dict] = {}
    state = {"down": False, "received": 0}

    @app.post("/cases")
    async def receive(request: Request):
        if state["down"]:
            return JSONResponse({"error": "maintenance"}, status_code=503)
        body = await request.json()
        try:
            case = wire.Case.model_validate(body)        # strict: exactly the agreed wire format
        except Exception as e:
            print(f"REJECTED invalid case: {e}", flush=True)
            return JSONResponse({"error": "invalid case", "detail": str(e)[:500]}, status_code=422)
        state["received"] += 1
        new = case.case_id not in cases
        cases[case.case_id] = body
        pend = ", ".join(f"{a.action_id} until {a.deadline}" for a in case.pending_actions) or "none"
        print(f"[{ist(time.time())}] {'NEW ' if new else 'UPD '} {case.case_id} {case.severity:6} {case.status:21} "
              f"pending: {pend} | {case.history[-1].event if case.history else ''}", flush=True)
        return {"ok": True, "case_id": case.case_id, "created": new}

    @app.get("/cases")
    async def list_cases():
        return list(cases.values())

    @app.post("/admin/down")
    async def down():
        state["down"] = True
        return state

    @app.post("/admin/up")
    async def up():
        state["down"] = False
        return state

    @app.get("/healthz")
    async def health():
        return {"ok": True, **state, "cases": len(cases)}

    return app


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8500)
    o = sub.add_parser("owner")
    o.add_argument("case_id")
    o.add_argument("owner")
    d = sub.add_parser("decide")
    d.add_argument("case_id")
    d.add_argument("action_id")
    d.add_argument("decision", choices=["approve", "reject", "extend", "release", "resolve"])
    d.add_argument("by")
    sub.add_parser("down")
    sub.add_parser("up")
    a = p.parse_args()
    if a.cmd == "serve":
        import uvicorn
        uvicorn.run(make_app(), host=a.host, port=a.port, log_level="warning")
        return
    if a.cmd in ("down", "up"):
        print(httpx.post(f"{MOCK_URL}/admin/{a.cmd}").json())
        return
    headers = partner_headers()
    if a.cmd == "owner":
        body = {"case_id": a.case_id, "type": "owner", "owner": a.owner, "time": ist(time.time())}
    else:
        body = {"case_id": a.case_id, "type": "decision", "action_id": a.action_id, "decision": a.decision,
                "by": a.by}
    r = httpx.post(f"{HANDOFF_URL}/control/case-update", json=body, headers=headers, timeout=10)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
