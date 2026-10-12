# Convergence (Easy Version): TenantGuard × Cyber Identity Team

**Simple idea:** Cyber team = **ID card office**. TenantGuard = **security guard at the gate**.

## What each team builds (about 1 hour each)

| Cyber team builds (for us) | TenantGuard builds (for them) |
|---|---|
| 1. A **pass** (token) for each of our 12 customers | 1. The gate **asks the Cyber team** "is this pass valid?" before letting a customer in |
| 2. A **Cancel** button for passes we report | 2. A **report** sent to the Cyber team when we see a pass being misused |

## Only 2 API calls between the teams

**Call 1: TenantGuard asks Cyber, "Is this pass valid?"**
```
POST {CYBER_URL}/verify
{ "pass": "<token>" }

Reply:
{ "ok": true,  "customer": "J", "plan": "enterprise" }
or
{ "ok": false, "reason": "cancelled" }
```
TenantGuard remembers each answer for 5 seconds, so it doesn't ask on every request.

**Call 2: TenantGuard tells Cyber, "This pass is being misused"**
```
POST {CYBER_URL}/report
{ "customer": "K", "pass": "<token>", "reason": "Used from new places at 9x normal speed" }
```
Cyber shows it on their screen with a **Cancel** button. Once it's cancelled, `/verify` answers `ok: false`.

## Safety
- If the Cyber service is down, TenantGuard keeps using the last answers and the old API keys. Nothing breaks.

## Demo (45 seconds)
1. A customer enters with a pass, and TenantGuard admits it as Enterprise.
2. Someone steals the pass and floods the system. TenantGuard catches it and sends a report.
3. A judge clicks **Cancel** on the Cyber screen, and TenantGuard blocks the thief within 5 seconds.

## Agree now
- Cyber's URL (laptop IP + port)
- Cyber sends 12 test passes (one per customer) + 1 cancelled pass, within 1 hour
- Joint test at H9, joint demo at H10
