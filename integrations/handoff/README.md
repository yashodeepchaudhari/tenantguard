# TenantGuard handoff bridge (Web PS3 "Digital Handoff" convergence)

Every TenantGuard incident becomes a case in the Web team's Digital Handoff app, with an owner, pending actions and deadlines. The on-call engineer's acknowledge, approve and other decisions flow back and take effect in TenantGuard live.

```
brain WS /ws/events ──► bridge (:8400) ──POST {WEB_URL}/cases──► Web app
                          ▲    │
   Web app ──POST /control/case-update   ├─► brain   POST /api/operator   (OperatorCmd: extend / release / resume)
                               └─► gateway POST /control/block   (BlockCmd, TTL-bound)
```

## Run
```
cd integrations/handoff
pip install -r requirements.txt               # tenantguard_common must already be installed
WEB_URL=http://<web-laptop>:<port> python -m tenantguard_handoff      # binds 0.0.0.0:8400
python tools/mock_web.py serve --port 8500    # stand-in for the Web app (WEB_URL=http://127.0.0.1:8500)
python tools/mock_web.py owner INC-0001 Rahul
python tools/mock_web.py decide INC-0001 A1 extend Rahul
python -m pytest
```

Env:
- `WEB_URL`: the partner app (default `http://127.0.0.1:8500`, the mock).
- `TENANTGUARD_BRAIN_URL` / `TENANTGUARD_GATEWAY_URL`: default to `tenantguard.yaml services`.
- `TENANTGUARD_CONTROL_TOKEN`: sent on every control call.
- `TENANTGUARD_UI_URL`: used for `evidence_link`; default `http://127.0.0.1:5173`.
- `HANDOFF_STATE_PATH`: case-id persistence; default `data/handoff-state.json`.
- `HANDOFF_PARTNER_TOKEN`: REQUIRED for inbound updates. Without it every `/control/case-update` and `/cases/update` returns 503 (fail closed: an update can make the bridge send control commands); a missing or wrong `X-Handoff-Token` returns 401.

Yaml `handoff`: `decision_window_s`, `block_s`, `extend_s`, `push_timeout_s`. The bridge also reads `brain.ttl_s` and `gateway.ttl_max_s`.

## Two formats, converted at the edge
- **Wire** (`tenantguard_handoff/wire.py`): the partner's agreed format from `docs/convergence/`. It is strict (unknown fields rejected), uses `case_id` `INC-NNNN`, and carries times as `"HH:MM:SS"` IST strings (`time`, `deadline`).
- **Internal** (`CaseView` in `contracts.py`): epoch floats, with `*_ist` strings for display only. Served on `GET /cases` and `GET /cases/{id}`. `timefmt.py` is the only converter.
- The brain's `incident_id` (`inc-<epoch>-<tenant>-<n>`) maps to a bridge-assigned `INC-NNNN`, persisted, so a restart never reuses an ID at the Web app. `evidence_link` = `{TENANTGUARD_UI_URL}/case/<incident_id>`.

## Incident → case
| Brain event | Case |
|---|---|
| any stage | history line `"<Stage>: <detail>"`; `summary` = latest detail; replays (WS reconnect backfill) are ignored |
| hold level again (renewal, recovery step, operator echo) | refreshes a pending hold; a new hold/block request appears only when the tenant escalates (enters a hold level, or a higher one) |
| first event | new case: title, verdict `over_plan` (until the brain sends one), `affected` = barcode neighbours (tenants sharing a server) |
| level throttle / deprioritize / quarantine | severity medium / medium / high (never lowered). Pending **hold** action: "auto-reverts at HH:MM:SS: extend or release?", deadline = `brain.ttl_s` |
| level quarantine | an extra pending **block** action: "approve blocking for `block_s`", deadline = `decision_window_s` |
| server incident (`srv-…`, no tenant) | title from the slow-servers detail; severity medium |
| `named`, top suspect ≥ `brain.confidence_to_prove` | the case adopts the culprit (title, `affected` = its barcode neighbours); severity high; pending **block** action (approve to block for `block_s`, TTL-bound) |
| back to warn / normal | the hold action expires |
| `verdict` with a known verdict | `context.verdict` |
| resolved / closed | status resolved / closed; pending actions expire |
| deadline passes with no decision | action expired; history notes TenantGuard's safe default (timers auto-undo) |

`status` = `waiting_for_decision` while any action is pending, else `in_progress` (until resolved or closed).

## Decision → control
| Action | Decision | Effect |
|---|---|---|
| hold | approve / extend | brain `OperatorCmd extend`, `until_s = min(extend_s, ttl_max_s)`: the ladder holds its level |
| hold | release | brain `OperatorCmd release`: the ladder goes back to normal, with no re-escalation for `until_s` |
| hold / block | reject | nothing; the timers' safe default applies |
| block | approve / extend | gateway `BlockCmd`, `ttl_s = min(block_s, ttl_max_s)`: never permanent |
| block | release | gateway `BlockCmd blocked=false` |
| any | resolve | case resolved; clears only the controls the bridge applied (unblock, `OperatorCmd resume`) |

- The bridge never sends raw `LimitsCmd`: the brain owns the ladder, so humans act through `OperatorCmd`, and the two never fight.
- If the brain doesn't have `/api/operator` yet (404), the history says "best-effort: brain not ready".
- Every outcome of a control call is written to the case history.
- Responses: an unknown case or action gets 404; an impossible decision for the action's state gets 409; a malformed update gets 400.

## Delivery
`Outbox`: latest version wins per case, retries with exponential backoff (0.5 → 8 s), and other cases get turns while one fails. `put()` is O(1) and never blocks, so a dead Web app only causes stale versions to be skipped. A permanent 4xx from the Web app is logged and dropped. `GET /healthz` shows `brain_connected`, the outbox counters and the last error.

## API
`GET /healthz`, `GET /cases`, `GET /cases/{id}` (`CaseView`), `GET /cases/{id}/wire` (exactly what the Web app receives), `POST /control/case-update` (alias `POST /cases/update`).

## Limits
- Partner payload: the last `handoff.history_wire_max` (50) history items plus one "N earlier events" line; memory keeps 500 per case.
- Closed/resolved cases are evicted `handoff.retention_s` (3600 s) after they close; their incidents are retired, so a WS backfill cannot resurrect them. The incident -> case id map is capped (2000).
- A brain message that fails to parse or apply is skipped and counted (`/healthz` `bad_messages`, `last_bad`); the stream stays up. Reconnect backoff (0.5 -> 10 s) resets only after a connection lived 10 s.
