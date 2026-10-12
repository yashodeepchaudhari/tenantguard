# TenantGuard incident narrator

Every TenantGuard incident gets a plain-English incident report for the operator and an anonymised message for affected customers. Both are built deterministically from the brain's own evidence and republished whenever the incident changes. The narrator is a read-only consumer of the brain and never makes a decision.

```
brain /api/incidents (backfill) + WS /ws/events ──► evidence pack ──► IncidentReport + CustomerMessage ──► ReportView (versioned)
```

## Run
```
cd narrator
pip install -r requirements.txt               # tenantguard_common must already be installed
python -m tenantguard_narrator                # binds 0.0.0.0:8700
python -m pytest
```

Config:
- Yaml `narrator.max_reports` (default 200) sets how many reports are kept in memory.
- `TENANTGUARD_BRAIN_URL` defaults to yaml `services.brain`.
- `TENANTGUARD_CORS_ORIGINS` sets the allowed UI origins.

## What it does
1. **Evidence pack** (`evidence.py`). It is built when an incident reaches `named`, `verdict` or `closed`, and rebuilt on every later event. It contains:
   - stages with IST times and the brain's own details;
   - suspects with confidence %;
   - for server incidents, the slow servers with their peak peer ratio (from the brain's detected list and `StateSnapshot`);
   - actions with parsed TTLs;
   - affected tenants (barcode neighbours), the peak level and the verdict.

   Replayed events are ignored (WS reconnect backfill).
2. **Incident report** (`template.py`): `IncidentReport {headline, timeline, cause, evidence_points, action_taken, operator_recommendation}`.
   - Server incidents name the culprit with the decoder's confidence, or say "no tenant blamed".
   - Tenant (ladder) incidents explain the usage-share over-use with the brain's measurements.
3. **Customer message.** It is built from an **anonymised** pack containing only times, duration, a generic cause and what was done. There is no tenant, no server and no copied brain text, so nothing can leak.
4. **Versions.** A report is republished (version + 1) only when its evidence changes; the store is bounded by `max_reports`.

## API
| Endpoint | |
|---|---|
| `GET /reports?limit=50` | `List[ReportView]`, newest first |
| `GET /reports/{incident_id}` | `ReportView` (404 if none) |
| `WS /ws/reports` | the latest 50 on connect, then every new version |
| `GET /healthz` | brain connection and counters |

`ReportView` (contracts §8): `incident_id`, `tenant`, `trigger_stage`, `version`, `report`, `customer_message`, `evidence` (the pack it was built from), `created_at`, `updated_at`.
