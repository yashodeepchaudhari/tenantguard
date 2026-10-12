"""End-to-end proof: brain incident -> case at the (mock) Web app -> owner -> decisions -> control commands.

Assumes running: fake gateway (FAKE_GW_URL, default :8099), brain, bridge (HANDOFF_URL), mock web (WEB_URL).
Partner updates carry HANDOFF_PARTNER_TOKEN (must match the bridge's), as the real partner app would.
"""
from __future__ import annotations

import json
import os
import time

import httpx
from mock_web import partner_headers   # same directory; also checks that the token is set

HANDOFF = os.environ.get("HANDOFF_URL", "http://127.0.0.1:8400")
WEB = os.environ.get("WEB_URL", "http://127.0.0.1:8500")
FAKE_GW = os.environ.get("FAKE_GW_URL", "http://127.0.0.1:8099")


def show(title, obj):
    print(f"\n=== {title}")
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=1))


def wait_for(pred, timeout_s, every=1.0):
    end = time.time() + timeout_s
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def case_with_pending():
    for c in httpx.get(f"{HANDOFF}/cases").json():
        if any(a["state"] == "pending" for a in c["actions"]):
            return c
    return None


def update(body):
    r = httpx.post(f"{HANDOFF}/control/case-update", json=body, headers=partner_headers(), timeout=10)
    return {"status": r.status_code, "response": r.json()}


def main():
    partner_headers()                    # fail fast, before waiting for an incident
    c = wait_for(case_with_pending, 150)
    if c is None:
        show("no case with a pending action appeared", httpx.get(f"{HANDOFF}/cases").json())
        return
    cid = c["case_id"]
    web_cases = {x["case_id"]: x for x in httpx.get(f"{WEB}/cases").json()}
    show(f"1. case at the Web app (what it received for {cid})", web_cases.get(cid))

    t = time.strftime("%H:%M:%S", time.gmtime(time.time() + 19800))
    show("2. Web: Rahul acknowledges (owner)", update({"case_id": cid, "type": "owner", "owner": "Rahul", "time": t}))
    show("2b. bridge view: owner", {k: httpx.get(f"{HANDOFF}/cases/{cid}").json()[k]
                                    for k in ("owner", "owner_since_ist", "status")})

    # wait for quarantine (block action) for up to 60 s, else act on the hold action
    def block_pending():
        v = httpx.get(f"{HANDOFF}/cases/{cid}").json()
        return next((a for a in v["actions"] if a["state"] == "pending" and "blocking" in a["text"]), None)
    blk = wait_for(block_pending, 30)
    hold = next((a for a in httpx.get(f"{HANDOFF}/cases/{cid}").json()["actions"] if a["state"] == "pending"
                 and "auto-reverts" in a["text"]), None)
    if blk:
        show(f"3. Web: Rahul approves {blk['action_id']} ({blk['text'][:60]}...)", update(
            {"case_id": cid, "type": "decision", "action_id": blk["action_id"], "decision": "approve", "by": "Rahul"}))
    if hold:
        show(f"4. Web: Rahul extends {hold['action_id']} (hold)", update(
            {"case_id": cid, "type": "decision", "action_id": hold["action_id"], "decision": "extend", "by": "Rahul"}))
    if blk:
        time.sleep(2)
        show(f"4b. Web: Priya releases {blk['action_id']} (unblock now)", update(
            {"case_id": cid, "type": "decision", "action_id": blk["action_id"], "decision": "release", "by": "Priya"}))
        show(f"4c. Web: Priya resolves {cid}", update(
            {"case_id": cid, "type": "decision", "action_id": blk["action_id"], "decision": "resolve", "by": "Priya"}))
    show("5. Web: bogus case -> 404", update({"case_id": "INC-9999", "type": "decision", "action_id": "A1",
                                             "decision": "approve", "by": "x"}))
    log = httpx.get(f"{FAKE_GW}/fake/control_log").json()
    show("6. control commands the gateway received from the bridge (kind=block)",
         [e for e in log if e.get("kind") == "block"])
    time.sleep(1.5)
    v = httpx.get(f"{HANDOFF}/cases/{cid}").json()
    show("7. case history (bridge view)", [f"{h['time_ist']} {h['event']}" for h in v["history"]])
    show("7b. actions", [{k: a[k] for k in ("action_id", "state", "deadline_ist", "decided_by")} for a in v["actions"]])
    w = {x["case_id"]: x for x in httpx.get(f"{WEB}/cases").json()}[cid]
    show("8. latest version at the Web app", {k: w[k] for k in ("case_id", "severity", "status", "pending_actions")}
         | {"history_tail": w["history"][-4:]})
    show("9. bridge health", httpx.get(f"{HANDOFF}/healthz").json())


if __name__ == "__main__":
    main()
