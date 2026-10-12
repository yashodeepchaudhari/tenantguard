# TenantGuard incident narrator

Every TenantGuard incident gets a plain-English report:
- **At once:** a deterministic **template** version.
- **Seconds later:** an **AI** version, published only if it passes a fact-by-fact check against the evidence.

The narrator is a read-only consumer of the brain and never makes a decision.

```
brain /api/incidents (backfill) + WS /ws/events ──► evidence pack ──► template ReportView (instant)
                                                                  └─► AI writer (async) ──► grounding check ──► AI ReportView
                                                                                 refusal / error / ungrounded ──► template kept
```

## Run
```
cd narrator
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt -e ../common   # anthropic 1.x needs httpx2: keep it in its own venv
.venv/Scripts/python -m tenantguard_narrator          # binds 0.0.0.0:8700
.venv/Scripts/python -m pytest
```

Configuration:
- Yaml `narrator`: `model` (default `claude-opus-5-5`), `effort` (`low`), `max_tokens`, `timeout_s`. Env `NARRATOR_MODEL` overrides the model.
- `ANTHROPIC_API_KEY` enables the AI version; without it the narrator runs **template only**, and `NARRATOR_AI=0` forces that.
- `TENANTGUARD_BRAIN_URL` defaults to yaml `services.brain`.

## What it does
1. **Evidence pack** (`evidence.py`). It is built when an incident reaches `named`, `verdict` or `closed`, and rebuilt on every later event. It contains:
   - stages with IST times and the brain's own details;
   - suspects with confidence %;
   - slow servers with their peak peer ratio (from `StateSnapshot`);
   - actions with parsed TTLs;
   - affected tenants (barcode neighbours), the peak level and the verdict.

   Replayed events are ignored (WS reconnect backfill). An **anonymised** pack, used for the customer message, carries only times, duration, a generic cause and what was done: no tenant, no server and no copied brain text.
2. **Template** (`template.py`): `IncidentReport {headline, timeline, cause, evidence_points, action_taken, operator_recommendation}` plus `CustomerMessage`. It is built only from the pack, so it always passes the checker; a test runs it through the checker.
3. **AI writer** (`ai.py`). The official `anthropic` SDK (`AsyncAnthropic`) with these settings:
   - **System prompt:** a frozen system prompt first, with a `cache_control` breakpoint.
   - **Input:** the evidence (sorted-key JSON) in the user turn.
   - **Output:** `output_config = {effort, format: json_schema}`, validated into the contract models.
   - **Refusals:** the server-side refusal fallback (`fallbacks: "default"`); a final refusal still falls back to the template.
   - **Isolation:** it runs as a background task (at most 2 in parallel) with a timeout, and is never on the decision path.
4. **Grounding check** (`grounding.py`): every tenant id or name, server id, `HH:MM:SS` time and number in the AI report must appear in the evidence pack (a number may be a rounding of one). The customer message is checked against the anonymised pack and may name no tenant. If anything fails, or on a refusal, an API error, a timeout or invalid output, the template stays and the reason is recorded in `ai_status` / `ai_detail`.
5. **Staleness.** Each AI run is tied to the pack it was given. If the incident changes meanwhile, the stale result is dropped and a fresh run starts.

## API
| Endpoint | |
|---|---|
| `GET /reports?limit=50` | `List[ReportView]`, newest first |
| `GET /reports/{incident_id}` | `ReportView` (404 if none) |
| `WS /ws/reports` | the latest 50 on connect, then every new version (template first, then AI) |
| `GET /healthz` | brain connection, AI on/off, model, counters |

`ReportView` (contracts §8) carries `source` (template or ai), a `grounded` flag, `ai_status` (pending / done / disabled / rejected / error), `ai_detail`, the model, a `version` that increases on every change, and the evidence pack itself.
