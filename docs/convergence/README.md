# TenantGuard × Web PS 3 (Digital Handoff): sample data

1. `cases/01..05-*.json` are what TenantGuard sends to `POST {WEB_URL}/cases`, one per verdict: `costly_pattern`, `over_plan`, `honest_growth`, `host_fault`, `mitigated_no_blame`.
2. Create one case per `case_id`. TenantGuard re-sends the **same** `case_id` whenever something changes, so update the case; never create a duplicate.
3. Show `title`, `severity` (`low` | `medium` | `high`), `status` (`waiting_for_decision` | `in_progress` | `resolved` | `closed`), `context.summary` and `history` on the case page.
4. Each item in `pending_actions` has an `action_id` and a `deadline` (HH:MM:SS, same day, IST). Show it as a task, and alert when the deadline is near.
5. Handoff completeness check: block a handoff while any pending action has no decision.
6. When the new owner clicks **Acknowledge**, send the `owner` example from `case-update-examples.json` to `POST {TENANTGUARD_URL}/control/case-update`.
7. Approve / Reject / Extend / Release / Resolve buttons send the matching `decision` example for that `action_id`.
8. `affected` lists tenant IDs A–L. Names: A Aarogya Health, B BazaarKart, C Chitra Studio, D DhanSetu Payments, E Ekalavya Learn, F FitZone, G GaadiGo, H HisaabBooks, I IdliExpress, J JhatpatMart, K KaamKaro, L LokVani News.
9. `evidence_link` is `http://<ui-host>:5173/case/<incident_id>`; replace `<ui-host>` with the TenantGuard UI laptop's address on the day.
10. If no decision arrives before a deadline, TenantGuard applies its safe default (timer auto-undo), so a missed click never breaks anything.
