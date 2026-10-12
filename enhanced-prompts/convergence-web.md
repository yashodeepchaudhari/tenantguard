# Convergence: TenantGuard (Cloud PS 3) × Digital Handoff (Web PS 3)

**Simple idea:** TenantGuard finds cloud problems. Someone on duty (on-call) must own each problem. When their shift ends, they **hand it over** to the next person. The Web team's app makes that handover complete and safe.

Their PS even says: **"Convergence: Web + Cybersecurity + Cloud"**, so we are their official Cloud partner.

---

## What each team builds (about 1 hour each)

| Web team builds (for us) | TenantGuard builds (for them) |
|---|---|
| 1. Turn every TenantGuard problem into a **case** with an owner, details and deadlines | 1. **Send every problem** with its pending actions and deadlines |
| 2. **Handoff check + Acknowledge + Approve buttons** that tell TenantGuard who owns the case and what was decided | 2. **Accept the new owner + decision** and act on it live |

---

## How our data fits their PS checklist

| Their PS needs | TenantGuard gives them |
|---|---|
| Case creation and ownership | Every incident = one case |
| Centralized context and documents | Verdict, affected customers, evidence link, incident report |
| Pending actions and deadline tracking | Our actions have timers, e.g. "Quarantine of K ends in 2 min: extend or release?" |
| Handoff completeness check | "Can't hand off: INC-104 still needs an approval" |
| New-owner acknowledgement | New owner clicks Acknowledge → TenantGuard shows the new owner |
| Activity history | Every TenantGuard step (detected → rescued → proved → fixed) is added to the case |
| Overdue notifications | A deadline near and no decision → alert. If nobody decides, TenantGuard safely auto-undoes |

---

## Only 2 messages between the teams

**Message 1: TenantGuard → Web, "New problem / update"**
```
POST {WEB_URL}/cases
{
  "case_id": "INC-104",
  "title": "Customer K is slowing 3 servers",
  "severity": "high",
  "status": "waiting_for_decision",
  "context": {
    "verdict": "costly pattern",
    "affected": ["B", "E", "H"],
    "evidence_link": "http://<tenantguard>/case/INC-104",
    "summary": "K's bulk-write requests create 9x hidden work."
  },
  "pending_actions": [
    { "action_id": "A1", "text": "Approve moving K's bulk-writes to the heavy lane", "deadline": "14:05:00" }
  ],
  "history": [ { "time": "14:02:10", "event": "Detected" }, { "time": "14:02:11", "event": "Victims rescued" } ]
}
```
Sent again (same `case_id`) whenever something changes.

**Message 2: Web → TenantGuard, "Owner changed / decision made"**
```
POST {TENANTGUARD_URL}/control/case-update
{ "case_id": "INC-104", "type": "owner",    "owner": "Rahul", "time": "14:03:30" }
{ "case_id": "INC-104", "type": "decision", "action_id": "A1", "decision": "approve", "by": "Rahul" }
```
`decision`: `approve` | `reject` | `extend` | `release` | `resolve`

---

## Safety
- If the Web app is down, TenantGuard keeps working with its own safe rules (timers + auto-undo).
- If TenantGuard is down, the Web app keeps the cases and history.

## Demo (60 seconds)
1. A judge starts an attack. TenantGuard detects it, and **a case appears in the Handoff app** owned by Priya (on duty).
2. Priya's shift ends and she clicks **Hand off**. The app **blocks it**: "1 pending approval, deadline in 2 min."
3. Priya adds a note and hands off. **Rahul clicks Acknowledge**, and TenantGuard's screen shows **Owner: Rahul**.
4. Rahul clicks **Approve**, and TenantGuard fixes it **live**. The case closes with the full history on both screens.

## Agree now
- Both laptop addresses (IP + port)
- We send them **5 sample cases** within 1 hour
- Joint test at H9, joint demo at H10
