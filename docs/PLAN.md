# PLAN: TenantGuard (CodeAstra 2.0, Cloud PS 3): coordinator plan file

**This file is the source of truth.** Decisions made in chat get written here in the same turn.
Product/design background: `docs/FINAL-PLAN.md` (scope tiers, demo), `enhanced-prompts/tenantguard-build-prompt.md` (detailed requirements).
`reshdata/` is out of scope: user instruction, do not read, use or edit it.

---

## §1 How we work

- **Coordinator:** session `codeastra-ea`, running in `D:\yashodeep\codeAstra` (master). It owns this plan, `common/`, `tenantguard.yaml`, `layouts/`, `docker-compose.yml`, `README.md`, merges, and the user-facing status.
- **Task sessions:** each task runs in its own git worktree `D:\yashodeep\codeAstra-T<n>` on branch `task/T<n>-<slug>`, with its brief in `TASK-T<n>.md` in that worktree. The user starts the session there (`cd` + `claude`, then paste the start line).
- **Support sessions** (in the main dir, write nothing under code paths):
  - `codeastra-21`: product/spec owner (author of the TenantGuard plan); convergence partner liaison. Writes only under `docs/convergence/`.
  - `codeastra-2b`: reviewer. Read-only reviews of task branches on request.
- **Report moments** (SendMessage to `codeastra-ea`), and nothing else:
  - `T<n>: started. Plan: <3 lines>.`
  - `T<n>: BLOCKED on <thing>. I need <exact decision or file>.`
  - `T<n>: done on branch task/<...>. <what changed>. Checks: <all> pass.`
- The coordinator merges into `master`, re-runs the checks, and **always tells the session it was merged**, giving it its next task in the same message. A session never commits to master and never exits; its next task runs in the same worktree on a fresh branch from master.

## §2 Ground rules (settled; do not relitigate)

1. **Python only for all backend services** (gateway, workers, brain, lab). Go is not installed and no one has shipped Go, so it is dropped. UI is React 18 + Vite + TypeScript + Tailwind + Recharts.
2. **Python 3.10 compatible.** The local interpreter is 3.10.1; Docker images use `python:3.12-slim`. Use `from __future__ import annotations`; no `asyncio.TaskGroup`, `ExceptionGroup`, `tomllib` or `typing.Self`.
3. **Contracts:** every cross-service message is a model in `common/tenantguard_common/contracts.py`. Import it; never redefine a contract locally. Need a field? Ask the coordinator.
4. **Config:** every tunable is in `tenantguard.yaml`, read via `tenantguard_common.config`. No hard-coded thresholds, ports or URLs. Workers and tenants come from `load_tenants()` / `worker_urls()`.
5. **Local dev ports:**
   - gateway `8000`
   - workers `w0..w8` → `8100..8108`, `spare` `8109`, `quarantine` `8110`
   - brain `8200`, lab `8300`, UI `5173`

   Inside Docker every worker listens on `8100`; compose sets `TENANTGUARD_WORKER_URLS`.
6. **Shared package:** `tenantguard_common` is installed editable from the main repo (`D:\yashodeep\codeAstra\common`) into the global Python 3.10. Task sessions do not reinstall it. Every service also runs without Docker (`uvicorn ... --port N`), because Docker is the deployment, not the only dev path.
7. **Tenant identity** comes only from `X-API-Key` at the gateway. Workers trust only `X-TenantGuard-Tenant` (set by the gateway). Workers report handler CPU per request in the `X-TenantGuard-Cpu-Ms` response header, and the gateway debits the cost bucket with it.
8. **Modes:**
   - `off`: FIFO, no limits, static routing.
   - `industry`: token + cost + concurrency limits, weighted DRR, cgroups, static equal routing.
   - `tenantguard`: everything, including the brain's control commands.

   The gateway owns the mode. The brain applies control commands only in `tenantguard` mode.
9. **Safety:** every control override has `ttl_s` and auto-reverts. If the brain heartbeat stops for `fail_static_after_s`, the gateway keeps its last config (fail-static). The gateway validates control input and returns 400; it never crashes on bad input.
10. **Verification standard:** "done" means unit tests pass (`pytest` in your dir) **and** you ran the real service and proved the behaviour with real HTTP calls. Paste the commands and their output in your done message. A test must be shown to fail when the behaviour is broken.
11. **Offline-first:** no external network at runtime except the optional Claude narrator (which has a template fallback).
12. **Numbers on slides come only from measured runs** (`lab/results/*.json`).
13. **Machine quirks:** Windows 11; PowerShell plus Git Bash. Docker Desktop must be running before any `docker compose` (it was found stopped on 2026-10-12). Use `127.0.0.1`, not `localhost`. Some peers run in "prompting" mode, so permission prompts may appear in their terminals. **Never rewrite UTF-8 files with Windows PowerShell 5 `Get-Content -Raw`/`Set-Content`**: it mis-decodes them and garbles `→ × ≥ §` (this happened on 2026-10-12). Use the Edit tool or Python with `encoding="utf-8"`.

## §3 Ownership

| Task | Owns (only these paths) | Must ask coordinator before |
|---|---|---|
| Coordinator | `docs/PLAN.md`, `common/**`, `tenantguard.yaml`, `layouts/**`, `docker-compose.yml`, `README.md`, `.gitignore` | n/a |
| T1 Gateway | `gateway/**` | new contract fields, new yaml keys, compose changes |
| T2 Workers | `worker/**` | same as above |
| T3 Brain | `brain/**` | same as above + any new gateway control endpoint |
| T4 UI | `ui/**` | new fields in StateSnapshot / IncidentEvent |
| T5 Lab | `lab/**` | same as above |
| codeastra-21 | `docs/convergence/**` | anything else |
| codeastra-2b | nothing (read-only) | n/a |

A task that needs a change in someone else's path sends a BLOCKED message with the exact change, and the coordinator routes it.

## §4 Waves

**Wave 0 (coordinator, done 2026-10-12):** repo, `common/` contracts + config, `layouts/ag23.json`, `tenantguard.yaml`, `docker-compose.yml`, this plan, briefs.

**Wave 1 (parallel T1–T5): Stage 1 + Stage 2 = every PS deliverable.**
Gate G1: in Docker, the lab runs 12 calm tenants plus a `whale` attack on tenant K.
- In `off` mode, victims' p95 rises a lot.
- In `industry` mode it is contained.
- In `tenantguard` mode the brain flags K (usage-share score), walks the throttle ladder and recovers it.
- The UI shows all of it live.
- The admin API edits a quota.

**Wave 2: Stage 3 (TenantGuard magic).**
- T1: route weights + relocation + drain overrides honoured; adaptive per-worker limiter; retry budget; express lane.
- T2: black-box indexer, cache isolation + eviction ledger, fault injection, per-class costs.
- T3: health (peer ratio) → rescue → barcode decoder → relocation prover (`docker update`) → verdicts.
- T4: 3×3 server grid, Case File, Scorecard pages.
- T5: blackbox_bomb, cache_flood, faulty_server, honest_spike, crash-test runner → `lab/results`.

Gate G2: black-box bomb is missed by `industry` and rescued → named → proved → resolved by `tenantguard`, 3 runs in a row.

**Wave 3: polish.** Genuineness/importance matrix, Claude narrator, Web PS3 convergence adapter (`brain/tenantguard_brain/handoff.py`, contract in `enhanced-prompts/convergence-web.md`), and the COULD list from FINAL-PLAN §3.

## §5 Task briefs (Wave 1)

Each brief is copied into its worktree as `TASK-T<n>.md`. **Every TASK file also carries an "Addendum: contracts v2 + two-laptop setup" that overrides the brief text where they differ** (QueueStat `rejected_429`/`expired_503`/`p95_ms`, TenantAdminView, OverridesView, LimitsCmd "always send both factors", `noisy_norm`, Lab models, bind 0.0.0.0, URLs from env, CORS).

### Common footer (part of every brief)
> Read `README.md`, `docs/PLAN.md` (§2 is settled: follow it, don't relitigate it) and `docs/FINAL-PLAN.md`. Build the standard feature set for your domain, not the bare minimum.
> **Done** means `pytest` passes in your directory AND you ran the real service and proved the behaviour with real HTTP calls; report the commands and output. Report what actually happened.
> Your coordinator is the session named `codeastra-ea` (confirm with ListAgents). SendMessage it at exactly three moments: started, blocked, done. No chatter. Commit to your own branch; never commit to master. The coordinator merges.

### T1: Gateway
**You are TASK T1:** when this is finished, any tenant request entering on :8000 is identified, admitted against multi-dimensional quotas, fairly scheduled, routed to one of its barcode servers, measured, and controllable by the admin and the brain.

You own `gateway/**` (package `gateway/tenantguard_gateway/`, tests in `gateway/tests/`, `gateway/Dockerfile`, `gateway/requirements.txt`).

Build:
1. FastAPI app on :8000. `ANY /api/{path}` → forwards to a worker with httpx (pooled AsyncClient), adding headers `X-TenantGuard-Tenant`, `X-TenantGuard-Class`, `X-Request-Id` from `contracts.py`.
2. **Identity:** `X-API-Key` hashed with SHA-256 → TenantConfig (from `load_tenants()`). Unknown key → 401 JSON.
3. **Request class** = sha1(method + path + sorted query keys + body-size bucket)[:8].
4. **Admission** (skipped in `off` mode):
   - token bucket (rate/burst)
   - cost bucket in CPU-ms, debited *after* the response using the worker's `X-TenantGuard-Cpu-Ms`; a request is refused while the bucket is negative
   - concurrency cap

   Refusal → 429 with an accurate `Retry-After`, plus `RateLimit-Policy` / `RateLimit` headers.
5. **Scheduler:** per-tenant queues served by weighted deficit round robin (weight = tier weight × `weight_factor`), quantum in cost units (use the class's average cost, default 5 ms). Bounded wait from yaml → 503 if expired, never forwarded. A global dispatch concurrency limit per worker (fixed `initial_window` in Wave 1). In `off` mode: a single FIFO.
6. **Routing:** pick one of the tenant's 3 servers by weight (default equal, random weighted).
7. **Telemetry:** per tick (`tick_ms`), per (tenant, server): count, p50, p95, errors; per tenant: queue depth, wait p95, admitted, rejected, inflight. Push `TelemetryFrame` on WS `/internal/telemetry` to every connected client. Also `GET /metrics` (Prometheus text) and `GET /internal/telemetry/latest` (JSON).
8. **Admin API:**
   - `GET /admin/tenants`
   - `POST /admin/tenants` (TenantConfig)
   - `PATCH /admin/tenants/{id}/quota` (QuotaPatch)
   - `GET /admin/mode`, `POST /admin/mode` (ModeBody)
   - `GET /admin/overrides` (active control overrides with expiry)
9. **Control API:** `POST /control/limits|block|heartbeat` (LimitsCmd, BlockCmd, Heartbeat) with TTL auto-revert. Fail-static: track the last heartbeat and expose it in `/admin/overrides`. `POST /control/route_weights|relocate|drain` must accept and store (validated, TTL) in Wave 1; honouring them in routing is Wave 2, but you may do it now if done early.
10. Tests:
    - token/cost bucket math
    - DRR fairness: weights 4/2/1 over 10k simulated requests within ±5%
    - a whale tenant cannot starve others
    - bounded-wait expiry
    - TTL revert
    - 401 / 429 + Retry-After

Prove it: run 2+ stub workers (a 10-line FastAPI that echoes, or T2's worker if merged), then show with curl/httpx that tenant K flooding gets 429s while A stays 200, that a quota PATCH changes behaviour, and that the WS emits frames.

### T2: Workers
**You are TASK T2:** when this is finished, the shared "SaaS app" runs as 9+2 identical worker containers. Each serves realistic light and heavy work per tenant, measures every tenant's CPU, DB and memory use exactly, and keeps tenant data isolated.

You own `worker/**` (package `worker/app/`, tests in `worker/tests/`, `worker/Dockerfile` installing `./common`, `worker/requirements.txt`).

Build:
1. FastAPI app, `SERVER_NAME` env, port from the CLI. A middleware reads `X-TenantGuard-Tenant` into a `contextvars.ContextVar` (missing → 400), and sets `X-TenantGuard-Cpu-Ms` on every response (handler CPU via `time.thread_time()`; handlers are **sync** `def`).
2. Endpoints:
   - `GET /api/light` (~1 ms; the victim SLO probe)
   - `GET /api/read?key=` (cache → DB)
   - `GET /api/search?n=` (CPU-heavy, cost scales with n)
   - `POST /api/write` (DB write + a background job charged to the tenant, via context copy into a thread pool, CPU measured)
   - `GET /api/memory?mb=` (allocate and hold briefly, within the tenant memory budget, else 507)
   - `GET /api/export` (batch, slow)
   - `GET /canary`
   - `GET /healthz`
3. **Storage:** one SQLite file per tenant (`/data/{tenant}.db`, or `./data/<server>/` locally), seeded at start. The repository takes the tenant from context only.
4. **Meters:** cumulative per-tenant `requests`, `handler_cpu_ms`, `bg_cpu_ms`, `db_ms`, `bytes_in`, `mem_mb`; per (tenant, class) count + cpu total (class from `X-TenantGuard-Class`).
5. **Stats:** `GET /stats` → `WorkerStats` (contracts), with `cpu_total_ms` from psutil on the process tree.
6. **Fault injection:** `POST /admin/fault {latency_ms, cpu_burn_pct}` and `DELETE /admin/fault` (simulated faulty host; the header `X-Admin-Token` must equal env `TENANTGUARD_ADMIN_TOKEN`, default `dev`).
7. 1 MB body limit (413).
8. `worker/Dockerfile` (python:3.12-slim, build context = repo root, installs `common`, listens on 8100). `docker compose up w0 w1` must work.
9. Tests:
   - tenant B can never read tenant A's data
   - a background job is charged to the right tenant
   - meters add up
   - fault adds latency

Prove it: run 3 workers locally on 8100–8102. Show that hammering `/api/search?n=big` as tenant K on w1 raises `/api/light` latency for tenant A on w1 (the noisy-neighbor effect, measured) but not on w0. Report the numbers.

### T3: Brain (Wave 1 = Stage 2)
**You are TASK T3:** when this is finished, a control-plane service watches every tenant's usage and latency, detects a noisy neighbor, throttles it with a graduated, self-recovering ladder, explains every action, and streams live state to the dashboard.

You own `brain/**` (package `brain/tenantguard_brain/`, tests in `brain/tests/`, `brain/Dockerfile`, `brain/requirements.txt`).

Build:
1. FastAPI + asyncio app on :8200, with three ingest tasks:
   - the gateway telemetry WS (`TENANTGUARD_GATEWAY_URL` env, default from yaml `services.gateway`) → `/internal/telemetry`, reconnecting
   - poll every worker's `/stats` at 1 Hz (diff the cumulative counters)
   - send a heartbeat to the gateway every 500 ms
2. **Rolling state:** 60 s of per-tenant rps, p95, rejected, CPU ms/s (handler + bg), and per-server p95 and CPU.
3. **Noisy score** per tenant = `max(cpu_share/fair_share, queue_share/fair_share)` × (1 + victim_impact), where:
   - fair_share = weight / Σ active weights
   - victim_impact = mean over *other* tenants of max(0, p95/baseline_p95 − 1), clipped to [0, 3]
   - baseline_p95 = calm-period EWMA

   A tenant is flagged only if it over-uses **and** others are hurt.
4. **Throttle ladder** (yaml `brain.noisy`), with hold times and hysteresis:
   - normal → warn → throttle (LimitsCmd factor ×0.5 per step, AIMD) → deprioritize (weight_factor 0.25) → quarantine (BlockCmd is NOT quarantine; in Wave 1 quarantine = LimitsCmd factor 0.1 + weight_factor 0.1, with a TODO to relocate in Wave 2)
   - recovery: +0.1 factor per recover tick after `recover_below` holds

   Every action sends a TTL; the brain renews it while the action is needed. Only in `tenantguard` mode (read the mode from the telemetry frame).
5. **Incidents:** an incident opens when a tenant reaches warn; emit an `IncidentEvent` on every stage/level change, with `detail` holding plain-English reasons (D5: every throttle stores why).
6. **UI stream:** WS `/ws/events` broadcasts a `StateSnapshot` at 2 Hz (tenants, servers, utilization, Jain fairness over per-tenant normalized throughput, status sentence) plus every IncidentEvent. Also `GET /api/state`, `GET /api/incidents`.
7. Pure logic (score, ladder, fairness) lives in modules with no I/O; tests for each, including "a legit burst with no victim impact is never throttled" and "ladder recovers after the attack stops".

Prove it: you can't wait for T1/T2. Build `brain/tools/fake_gateway.py` (emits TelemetryFrames from a scripted scenario: calm → K whale → calm, and accepts /control/*) and show the ladder walk + recovery in the event log. After T1 merges, re-verify against the real gateway.

### T4: UI (Wave 1)
**You are TASK T4:** when this is finished, an operator sees the whole multi-tenant platform live on one screen: every tenant's health, who is noisy and what the system is doing about it. They can switch modes, launch attacks and edit quotas.

You own `ui/**` (Vite + React 18 + TS + Tailwind + Recharts; `npm create vite@latest ui -- --template react-ts`).

Build:
1. **Data:**
   - WS to the brain `/ws/events` (StateSnapshot + IncidentEvent; mirror the TS types from `contracts.py` into `ui/src/types.ts` exactly)
   - REST to the gateway `/admin/*` (mode, tenants, quota PATCH)
   - REST to the lab `/lab/*` (start/stop attacks)

   Base URLs come from `ui/.env` (`VITE_BRAIN_URL`, `VITE_GATEWAY_URL`, `VITE_LAB_URL`). Ask the coordinator if CORS is needed; it is part of the T1/T3/T5 briefs that their apps allow `http://127.0.0.1:5173`.
2. **Control Room page:**
   - status sentence (big)
   - 12 tenant tiles (name, tier, p95 ms, rps, level badge; colour = status only)
   - live line chart of p95 per tenant (last 60 s)
   - stacked CPU-share chart
   - noisy-score bars
   - incident timeline
   - fairness index + utilization gauges
   - mode toggle OFF / Industry / TenantGuard
   - attack launcher (attack, tenant, intensity, duration)
3. **Tenants page:** table with quotas and an inline quota edit (PATCH).
4. Design rules: colour means status (green / amber / red) plus one accent; big type; a unit on every number; works on a 1080p projector.
5. Build `ui/src/mock/` with a mock WS server or fixture replay, so the UI is developable before the brain exists; a toggle `VITE_MOCK=1`.

Prove it: `npm run build` passes plus `npm run lint`; a screenshot (headless Edge) of the Control Room on mock data, and later against the real brain.

### T5: Lab
**You are TASK T5:** when this is finished, one command fills the platform with 12 realistic tenants, and any attack can be launched or stopped from the dashboard or the CLI. Every run produces measured victim-latency results.

You own `lab/**` (package `lab/tenantguard_lab/`, tests in `lab/tests/`, `lab/Dockerfile`, `lab/results/.gitkeep`).

Build:
1. FastAPI on :8300 plus an asyncio traffic engine using httpx with keep-alive pools, sending to the gateway with each tenant's API key.
2. **Calm traffic:** 12 tenant personas (names from the layout), Poisson arrivals, each with a request mix (mostly `/api/light` + `/api/read`, a few `/api/search` and `/api/write`). Rates are configurable in `lab/personas.yaml`, calibrated to about 30% fleet CPU.
3. **Attacks**, via `POST /lab/attack` (AttackReq) → AttackStatus, `DELETE /lab/attack/{id}`, `GET /lab/status`:
   - `flood` (10× rps light)
   - `whale` (few, very heavy `/api/search`)
   - `hog` (many slow concurrent `/api/export`)
   - `memory`
   - `retry_storm` (retries immediately on 429/503, ignoring Retry-After)

   Wave 2 adds `background_bomb`, `blackbox_bomb`, `cache_flood`, `faulty_server`, `honest_spike`. Stub them now with a 501.
4. **Measurement:**
   - per tenant: client-side p50/p95/p99, success, 429, 503, per second
   - `GET /lab/metrics`
   - `POST /lab/run {scenario, mode}` runs a scripted scenario (calm 20 s → attack 40 s → calm 20 s) after setting the gateway mode via `/admin/mode`, then writes `lab/results/<ts>-<scenario>-<mode>.json` with victim p99 for baseline / attack / after, time-to-recover, and the attacker's 429 count
5. CLI: `python -m tenantguard_lab calm`, `python -m tenantguard_lab attack whale K`.
6. Allow CORS from `http://127.0.0.1:5173`.
7. Tests: Poisson rate within ±10%; the percentile math; the scenario timeline.

Prove it: run against a stub gateway (a 10-line FastAPI that sleeps 2 ms), then against real T1 + T2 when merged. Report a results JSON.

---

## Task table

| Task | Branch | Worktree | Session | Status |
|---|---|---|---|---|
| T1 Gateway | task/T1-gateway | D:\yashodeep\codeAstra-T1 | codeastra-t1-71 | in progress |
| T2 Workers | task/T2-workers | D:\yashodeep\codeAstra-T2 | codeastra-t2-d5 | in progress |
| T3 Brain | task/T3-brain | D:\yashodeep\codeAstra-T3 | codeastra-t3-b9 | in progress |
| T4 UI | task/T4-ui | D:\yashodeep\codeAstra-T4 | codeastra-t4-6b | in progress |
| T5 Lab | task/T5-lab | D:\yashodeep\codeAstra-T5 | codeastra-t5-79 | in progress |
| Convergence samples | n/a (main dir, docs/convergence/) | main | codeastra-21 | assigned |
| Review | n/a | main | codeastra-2b | standby |

## Decisions log
- 2026-10-12: Python-only backend (Go not installed). TenantGuard design adopted; `reshdata/` ignored per user. Convergence partner = Web PS3 Digital Handoff (`enhanced-prompts/convergence-web.md`); `convergence-contract.md` (Cyber) kept as a backup only.
- 2026-10-12 (contracts v2, from codeastra-2b review):
  - `noisy_score` is the raw ratio; `noisy_norm` (0..1) is for bars.
  - LimitsCmd semantics are in its docstring: replace + restart TTL; base = PATCHed quota; ints = max(1, round); 1.0/1.0 clears; 400 outside (0, 1].
  - `GET /admin/tenants` returns `TenantAdminView` (no api_key); `GET /admin/overrides` returns `OverridesView`.
  - IncidentEvent gains `tenant`, `level`, `prev_level`. QueueStat splits `rejected_429` / `expired_503` and adds end-to-end `p95_ms` + `completed` (the brain uses QueueStat.p95_ms for per-tenant victim latency).
  - Lab: `LabStatus`, `RunReq`, `RunResult` (the results JSON schema). AttackReq intensity is 0.1..5.
  - Units are documented in the contracts header.
  - Tenants added at runtime via `POST /admin/tenants` are out of scope for the brain in Wave 1 (it uses `load_tenants()`).
- 2026-10-12: Background-job CPU is NOT charged to the gateway cost bucket in Wave 1 (`X-TenantGuard-Cpu-Ms` = handler only). Wave 2 T1+T3: the brain pushes per-tenant bg CPU debits to the gateway (new control `charge`), closing the "background work included" claim.
- 2026-10-12 (convergence samples from codeastra-21, accepted):
  - verdicts are snake_case (`costly_pattern`, `over_plan`, `honest_growth`, `host_fault`, `mitigated_no_blame`)
  - case status is `waiting_for_decision|in_progress|resolved|closed`; severity is `low|medium|high`; times are HH:MM:SS IST
  - evidence_link = `http://<ui-host>:5173/case/<incident_id>` (T4 adds the `/case/:id` route in Wave 2)
  - Case/handoff Pydantic models are added to `contracts.py` at the start of Wave 3.
- 2026-10-12: The Docker host (friend's laptop, GitHub reki-shu) is set up per `docs/DOCKER-HOST-SETUP.md`. The coordinator drives it via the docker context `tg-host` over SSH (key `~/.ssh/tenantguard_ed25519`), building from the coordinator's files. This laptop has 8 cores / 7.8 GB RAM, too small to also host the full stack beside 7 Claude sessions.
- 2026-10-12 (user): **Project name is TenantGuard.** The old name was a mistake and must not appear anywhere.
  - Package `tenantguard_common`; config `tenantguard.yaml`; env prefix `TENANTGUARD_`; headers `X-TenantGuard-Tenant` / `-Class` / `-Cpu-Ms`; mode literal `tenantguard` (off | industry | tenantguard).
  - Service packages: `gateway/tenantguard_gateway`, `worker/app`, `brain/tenantguard_brain`, `lab/tenantguard_lab`.
  - Persona F renamed to FitZone.
  - Repo: private GitHub `yashodeepchaudhari/tenantguard`, collaborator `reki-shu` (friend's laptop = Docker host).
- 2026-10-12 (user): **Two-laptop setup.**
  - The friend's laptop = Docker host ("Laptop A"): gateway, workers, spare, quarantine and brain in compose. The brain must sit with docker.sock.
  - This laptop = dev + traffic ("Laptop B"): all Claude sessions, the lab and the UI, pointing at `<friend-ip>`.
  - Dev needs no Docker; Docker is only required at gate G1.
  - Code reaches Laptop A via a private GitHub remote (pending user OK).
  - Laptop A needs ≥ 8 cores / 16 GB; with fewer, lower `worker.cpus` or use the 6-worker layout.
  - Services must therefore bind `0.0.0.0` and allow CORS from the UI origin; nothing may assume 127.0.0.1 for a peer service (always read URLs from config/env).
- 2026-10-12: Cost bucket is debited with the worker-measured CPU (`X-TenantGuard-Cpu-Ms`) rather than an estimate, so it is a real resource quota, not a request count.
