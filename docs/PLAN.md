# PLAN: TenantGuard (CodeAstra 2.0, Cloud PS 3): coordinator plan file

**This file is the source of truth.** Decisions made in chat get written here in the same turn.
Product/design background: `docs/FINAL-PLAN.md` (scope tiers, demo), `enhanced-prompts/tenantguard-build-prompt.md` (detailed requirements).
`reshdata/` is out of scope: user instruction, do not read, use or edit it.

---

## §1 How we work

- **Coordinator:** session `codeastra-b1`, running in `D:\yashodeep\codeAstra` (master). It owns this plan, `common/`, `tenantguard.yaml`, `layouts/`, `docker-compose.yml`, `README.md`, merges, and the user-facing status.
- **Task sessions:** each task runs in its own git worktree `D:\yashodeep\codeAstra-T<n>` on branch `task/T<n>-<slug>`, with its brief in `TASK-T<n>.md` in that worktree. The user starts the session there (`cd` into it, start a session, then paste the start line).
- **Support sessions** (in the main dir, write nothing under code paths):
  - `codeastra-21`: product/spec owner (author of the TenantGuard plan); convergence partner liaison. Writes only under `docs/convergence/`.
  - `codeastra-2b`: reviewer. Read-only reviews of task branches on request.
- **Report moments** (SendMessage to `codeastra-b1`), and nothing else:
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
10a. **No tool attribution anywhere (user rule):** never add co-author trailers, generator footers, or the name of any development tool to commit messages, PR bodies, code, comments or docs. Plain commit messages only.
11. **Offline-first:** no external network at runtime.
12. **Numbers on slides come only from measured runs** (`lab/results/*.json`).
12a. **This laptop is NOT a Docker host (user rule, 2026-10-12: the laptop hung).**
  - No docker build/compose/containers here; Docker runs only on the friend's laptop.
  - Locally run at most **3 workers + gateway + brain**, and kill every process right after each proof run.
  - Only one session runs a local stack at a time; ask the coordinator first.
  - The laptop has 8 cores / 7.8 GB, and the dev sessions themselves use most of the RAM.
12b. **The coordinator never runs git commands that change a live session's worktree** (checkout, merge, branch switch). It sends the session the exact commands instead. (Learned 2026-10-12: a branch switch landed T15b's commit on the wrong branch.) After every merge, the coordinator verifies `git log` shows the merge commit before announcing it.
12c. **Never run the `docker` CLI on this laptop, not even with `--context tg-host`.** Invoking it relaunches Docker Desktop + WSL locally (~1.4 GB RAM). Learned 2026-10-13: it was a main cause of the out-of-memory kills. Drive the Docker host ONLY via `ssh tg-host "docker ..."` / `ssh tg-host "cd /d C:\Users\preeti\tenantguard-deploy && docker compose ..."`. Image builds needing Docker Hub credentials can't run over SSH, so build ON the host with the classic builder: `ssh tg-host "cd /d C:\Users\preeti\tenantguard-deploy && set DOCKER_BUILDKIT=0&& docker build -f <svc>/Dockerfile -t codeastra-<svc>:latest ."` (verified 2026-10-13: no credentials needed, nothing starts locally).
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

**Wave 3: polish.** Genuineness/importance matrix, incident narrator, Web PS3 convergence adapter (`brain/tenantguard_brain/handoff.py`, contract in `enhanced-prompts/convergence-web.md`), and the COULD list from FINAL-PLAN §3.

## §5 Task briefs (Wave 1)

Each brief is copied into its worktree as `TASK-T<n>.md`. **Every TASK file also carries an "Addendum: contracts v2 + two-laptop setup" that overrides the brief text where they differ** (QueueStat `rejected_429`/`expired_503`/`p95_ms`, TenantAdminView, OverridesView, LimitsCmd "always send both factors", `noisy_norm`, Lab models, bind 0.0.0.0, URLs from env, CORS).

### Common footer (part of every brief)
> Read `README.md`, `docs/PLAN.md` (§2 is settled: follow it, don't relitigate it) and `docs/FINAL-PLAN.md`. Build the standard feature set for your domain, not the bare minimum.
> **Done** means `pytest` passes in your directory AND you ran the real service and proved the behaviour with real HTTP calls; report the commands and output. Report what actually happened.
> Your coordinator is the session named `codeastra-b1` (confirm with ListAgents). SendMessage it at exactly three moments: started, blocked, done. No chatter. Commit to your own branch; never commit to master. The coordinator merges.

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

### T6: Workers wave 2 (same session as T2, same worktree)
**You are TASK T6:** when this is finished, the worker hosts a realistic "vendor component" whose CPU no tenant meter can see. That is the hidden-cost attack every rate limiter misses. The lab can also calibrate traffic from measured endpoint costs.

You own `worker/**`.

Build:
1. `blackbox.py`: a separate `multiprocessing` child process (the "vendor indexer") fed by a queue. `POST /api/ingest {docs:int}` enqueues work: each doc costs a fixed CPU amount inside the child. The handler itself stays cheap (<1 ms), so `X-TenantGuard-Cpu-Ms` and the tenant meters stay small. The child's CPU, read with psutil on the child, is reported ONLY as cumulative `WorkerStats.blackbox_cpu_ms`, never per tenant. The queue is bounded (drop + count when full). The child is restarted if it dies.
2. The black-box work must really slow co-located tenants: verify that `/api/light` p95 for tenant A on the same worker rises under sustained ingest from K, while K's own handler CPU stays near zero. Report the numbers, as in the T2 demo.
3. Import `FaultReq` from `tenantguard_common.contracts` (now added) instead of the local model. Keep the behaviour.
4. Read the new yaml keys (`worker.cache_mb`, `cache_guaranteed_share`, `bg_threads`, `seed_rows`, now in `tenantguard.yaml`), and add `worker.blackbox: {cpu_ms_per_doc, queue_max}`. Send that key to the coordinator as a request; default it in code meanwhile.
5. `worker/tools/calibrate.py`: inside Docker (0.5 CPU), measure the mean/p95 handler CPU and latency of every endpoint at default params, and write `worker/calibration.json`. T5 uses it to size the personas to ~30% fleet CPU. Run it in Docker on this laptop if Docker is up; otherwise locally, and say which.
6. Tests: black-box CPU never appears in tenant meters; the queue bound; child restart; FaultReq from contracts.

Done = pytest passes + the measured black-box noisy-neighbor numbers + calibration.json committed. Same protocol: started / blocked / done to `codeastra-b1`; no tool attribution in commits.

### T7: Gateway wave 2 (T1 session, same worktree)
**You are TASK T7:** when this is finished, the gateway is fully adaptive. The queue lives at the gateway with a per-worker window that tracks real latency, retry storms are capped, cheap requests never wait behind whales, and background CPU is charged to the tenant who caused it.

You own `gateway/**`. Ask before contract/yaml changes (send exact proposals).

Build:
1. **Adaptive per-worker window** (gradient limiter): `new = old × (minRTT/sampleRTT) + sqrt(old)`, clamped to `gateway.limiter` min/max, with minRTT decaying slowly. Exposed in `LimiterStat.window`. Active in industry + tenantguard; off = fixed.
2. **Express lane:** requests whose class reference cost < `gateway.express_cost_ms` skip ahead of heavy items (still per-tenant fair). Prove that light p95 stays flat while a whale is queued.
3. **Retry budget:** a request is a retry if it repeats an `Idempotency-Key`, or (tenant, class, body-hash) within 10 s. Retries are allowed up to `gateway.retry_budget` (propose the key, default 0.1) of the tenant's recent requests, else 429 reason `retry_budget`. Telemetry/metrics count retries.
4. **Batch class:** `X-TenantGuard-Class: batch` header or `batch_routes` → batch; bounded wait 5000 ms; batch is shed first when a worker's window is saturated.
5. **Control `charge`** (new contract, propose it): `{tenant, cpu_ms}`, which debits the cost bucket. The brain will send per-tenant bg CPU each second (closing "background work included").
6. **Control `reference_costs`** (ReferenceCostsCmd exists): overrides the class cost EWMA for DRR.
7. Read `gateway.upstream_timeout_s` and `batch_routes` from yaml (keys now exist).
8. Tests for each, plus mutation checks.

Prove it against real workers: the whale vs light p95 with/without the express lane; a retry storm capped; window values moving under load.

### T8: Brain integration + Stage 3 detection (T3 session, same worktree)
**You are TASK T8:** when this is finished, the brain is proven end to end against the real gateway and workers. It can also tell *which server* is slow, rescue victims by routing around it within about a second, and name the culprit from the barcode pattern, including the hidden-cost case no meter sees.

You own `brain/**`.

Build:
1. **Integration first:** run the real gateway (master) + real workers + the brain locally. Drive K with a whale (a curl/httpx loop with K's key, or `brain/tools/`) and show the ladder walking on REAL telemetry. Report the event log. This laptop is CPU-starved (~97%), so run 9 workers (no spare/quarantine) and keep traffic modest.
2. **Health:** per server, peer_ratio = p95(server) / median(p95 of other servers) over `probe_window_ticks`; slow when ≥ `slow_peer_ratio` and +`slow_abs_ms`, for `open_after_ticks`. Fill `ServerView.peer_ratio`/`status`.
3. **Rescue:** on slow servers, send RouteWeightsCmd shifting each affected tenant's weight to its healthy barcode servers, capped by `rescue_capacity_guard`. Stage `rescued` in IncidentEvent.
4. **Decoder:** score each tenant from barcode coverage (min and mean of the logistic p_s over its 3 servers) + "stuck" (all its servers slow) + ledger hints (bg_cpu growth, evictions_caused, blackbox_cpu growth on all its servers). Softmax over candidates + `host_fault` + `none`; explain-away for 2 culprits. Stage `named` with Suspects. Pure module + tests, including the property "for all 66 culprit pairs with clean signals, the decoder returns exactly that pair".
5. **Host fault:** a slow server that no candidate explains → host suspect (verdict later). Use the worker's `/admin/fault` to prove it live.
6. Prover/relocation (`docker update`) comes in the next task (it needs the Docker host); leave a clean interface.

Prove it live: (a) fault-inject w4 → rescue shifts B/E/G/L away within ~1–2 s and names "host suspect w4", no tenant blamed; (b) K hammering /api/search on its 3 servers → named K with confidence.

### T9: UI wave 2 (T4 session, same worktree)
**You are TASK T9:** when this is finished, judges can watch TenantGuard find a culprit: which server is slow, who caused it, how the system proved it, and the OFF / Industry / TenantGuard scorecard with measured numbers. The UI also runs against the real stack.

You own `ui/**`.

Build:
1. **Real-stack check first:** master now has the gateway + brain + workers. Run them locally (see their READMEs; this laptop is CPU-starved, so use 9 workers and modest load), point `ui/.env` at them, run your e2e + screenshot against the REAL brain, and fix any mismatch. Report anything the backend gets wrong as BLOCKED with the exact field.
2. **3×3 server grid** (w0..w8) on the Control Room: peer_ratio, status colour, cpu_pct. Selecting a tenant highlights its 3 barcode servers. Drained servers are hatched.
3. **Case File page** at route `/case/:incident_id` (convergence evidence_link uses this exact path):
   - incident timeline (detected → rescued → named → proving → verdict → resolved → explained, with timestamps)
   - suspects with confidence bars
   - barcode match drawn on the grid
   - verdict + action + TTL countdown
   - `detail` text
   - `report` (render the dict as sections when present; a template placeholder otherwise)

   Data comes from brain `GET /api/incidents?incident_id=`.
4. **Scorecard page:** a table + bar charts of `RunResult` files (victim p99 baseline/attack/after, time_to_detect, time_to_recover, attacker 429s) grouped by scenario × mode (off / industry / tenantguard). Data comes from lab `GET /lab/results` (T5 provides a list of RunResult; until then use fixtures in `ui/src/mock/`).
5. Keep the Control Room on one 1080p screen; add a nav.
6. Tests + e2e for the new pages; screenshots in `ui/screenshots/`.

### T10: Lab wave 2 + crash-test (T5 session, same worktree)
**You are TASK T10:** when this is finished, every attack in the demo can be launched, and one command produces the full OFF / Industry / TenantGuard scorecard as RunResult files. That includes the hidden-cost attacks that defeat a plain rate limiter.

You own `lab/**`.

Build:
1. **Wave 2 attacks** (replace the 501s):
   - `background_bomb`: POST /api/write bursts → heavy tagged bg jobs
   - `blackbox_bomb`: POST /api/ingest {docs}; T6 is adding it on the worker, so until it merges, run against a stub and skip the live proof
   - `cache_flood`: many distinct /api/read keys to evict others
   - `faulty_server`: worker POST /admin/fault with `FaultReq` from contracts, header X-Admin-Token = env TENANTGUARD_ADMIN_TOKEN; the target is a worker URL from config
   - `honest_spike`: 8× the persona's normal mix, staying within plan quota
2. **crash-test runner:** `python -m tenantguard_lab crashtest --scenarios whale,blackbox_bomb,... --modes off,industry,tenantguard --runs N --random-culprit`. It runs RunReq sequentially and writes one RunResult each, plus a summary JSON (`lab/results/summary-<ts>.json`: mean/median per scenario × mode, culprit-correct rate from brain incidents, innocents penalised = incidents/LimitsCmds on non-attackers). If the summary needs fields, propose a `CrashSummary` contract to me.
3. **Docker-host readiness:** the lab runs on THIS laptop, pointing at the friend's laptop. Make all target URLs come from env (`TENANTGUARD_GATEWAY_URL`, `TENANTGUARD_BRAIN_URL`, `TENANTGUARD_WORKER_URLS`). Add `lab/README.md` "run against the Docker host" with exact commands.
4. Tests (no stack needed).

Proof: unit tests + a tiny live run of 1 scenario × 3 modes (ask for the stack slot first). The full crash-test runs later on the Docker host.

### T11: Handoff bridge, Web PS3 convergence (T1 session, same worktree)
**You are TASK T11:** when this is finished, every TenantGuard incident automatically becomes a case in the Web PS3 "Digital Handoff" app, with an owner, pending actions and deadlines. An on-call engineer's handoff, acknowledge or approve in their app flows back and takes effect in TenantGuard live.

You own a NEW top-level dir `integrations/handoff/` (package `tenantguard_handoff`, FastAPI on :8400, Dockerfile, tests). Do not edit `brain/` (T8 owns it). Request any brain endpoint you need from the coordinator.

Inputs:
- `enhanced-prompts/convergence-web.md` (the agreed contract)
- `docs/convergence/` (5 sample cases, case-update examples, `handoff-models.py`)

Build:
1. **Case models:** propose moving `docs/convergence/handoff-models.py` into `contracts.py` (send the exact text). Follow the review rule: IST "HH:MM:SS" strings only on display fields named `*_ist`; epoch floats elsewhere; the adapter converts.
2. **Outbound:** subscribe to brain WS `/ws/events` (IncidentEvent + StateSnapshot, reconnecting). Build/update a Case per incident_id: title, severity, status, context (verdict, affected = barcode neighbours, evidence_link `http://<ui-host>:5173/case/<id>`, summary from detail), pending_actions (with deadlines from action TTLs), history. `POST {WEB_URL}/cases` on every change (idempotent by case_id), with retry + backoff. Never block, and drop stale updates.
3. **Inbound:** `POST /cases/update` accepts CaseUpdateOwner / CaseUpdateDecision. Owner → stored, and shown in a `GET /cases/{id}` view. Decision approve/reject/extend/release/resolve → mapped to gateway control (LimitsCmd/BlockCmd/RelocateCmd/TTL extend) via the control token, honouring safety (no permanent block; TTL ≤ ttl_max_s). Note: the brain owns the throttle ladder, so for now decisions apply only to actions the bridge marked pending. Coordinate with me before anything fights the brain.
4. **Mock Web app** `integrations/handoff/tools/mock_web.py` (receives cases, prints them, can send updates) for a demo without the partner team.
5. Config: `WEB_URL`, the brain/gateway URLs and the token all from env; add yaml keys only via the coordinator.
6. Tests: model round-trip of all sample files, event→case mapping, decision→control mapping, the Web app down never blocks, unknown case_id → 404.

Proof: run the mock web + bridge against a recorded event stream (use `brain/tools/fake_gateway.py` + the brain, after asking for the slot) and show a case created → owner change → approve → control command sent.

### T12: Brain prover + verdicts (T3 session, same worktree)
**You are TASK T12:** when this is finished, TenantGuard doesn't just suspect a culprit, it **proves** it. It moves the suspect to the spare server and checks that the slowdown follows it, then picks the right verdict and the right action.

You own `brain/**`.

Build:
1. **Prover:** a DockerProver implementing your prover interface.
   - Size the spare to `spare_sizing × footprint` via the Docker SDK (`container.update(cpu_quota=…)`; containers are named `codeastra-spare-1` etc., use env COMPOSE_PROJECT).
   - Relocate the suspect (RelocateCmd target spare), wait `prove_settle_s`, measure `prove_window_s`.
   - R = 1 − post/pre slowdown on its original servers. Confirm when R ≥ recovery_R AND the harm followed (spare p95/peer ratio or cpu rose).
   - Stage `proving` → `verdict`. NullProver stays the default when Docker is unavailable (env/yaml switch).
2. **Verdicts:**
   - costly_pattern: paid share < `paid_share_costly`, or black-box/eviction evidence. Then run a class-level relocation to find the guilty request class and send it to the quarantine (heavy lane) via RelocateCmd with cls.
   - honest_growth: paid work explains the load → keep it on the spare as a burst lane; no penalty.
   - over_plan: from admission rejections.
   - host_fault: from your host suspects, confirmed by drain + idle re-measure (DrainCmd, ≤1 drained server, peers <75% busy).
   - mitigated_no_blame: below the confidence bar.

   Actions carry TTLs, honour the action budget, and use the repeat-offender memory.
3. **Fix T9's observation:** tenant L, with all its servers down (fast 502s), was flagged at warn on 16% of dispatch slots with 0% CPU. Failed or 5xx requests must not count as slot usage for the noisy score.
4. **Bg CPU charge:** each second, send `ChargeCmd` per tenant for its bg_cpu_ms delta (the gateway debits it; tenantguard mode only). charge_id = `<tenant>-<ts>`.
5. Move decoder weights and health.min_requests/canary_s into yaml: send me exact keys.
6. Tests: the prover with a fake Docker client (confirm, not-confirm, the culprit pausing mid-test), each verdict, the L false-flag regression, charge.

Proof: unit tests here. The live prover proof runs on the Docker host. When ready, send me the exact commands to run there and I'll run them via the `tg-host` docker context and send you the output.

### T13: UI wave 3, story features (T4 session, same worktree)
**You are TASK T13:** when this is finished, the dashboard tells the judges the story at a glance: money and customers protected, requests visibly flowing away from a slow server, and the proof animation. It also runs against the real Docker host.

You own `ui/**`.

Build:
1. **Docker host run:** the full stack is now running on the friend's laptop. Gateway `http://100.112.232.8:8000`, brain `http://100.112.232.8:8200` (Tailscale), control token `dev`. Point `ui/.env` there and run your e2e + screenshots. Only Vite + Edge run on this laptop; no slot needed (CPU-light). Report mismatches.
2. **₹ impact panel:** revenue protected (victims' `monthly_revenue_rs` × SLO-violation time avoided), customers shielded, innocents penalised (must be 0). Derive it client-side from snapshots/incidents and the layout tenant data (propose a contract field if the brain should own it).
3. **Live flow view** (canvas): dots travel tenant → server, sampled from snapshot rps. Slow servers turn red and dots visibly shift during the rescue (route weights from gateway `/admin/overrides`).
4. **Proof animation** in the Case File during the `proving` stage: red "smoke" moves from the suspect's servers to the spare.
5. **Handoff:** a Cases column showing the bridge's `CaseView` (GET :8400/cases) with owner + pending actions, when the bridge is reachable.
6. Keep the Control Room on one 1080p screen (a toggle between tiles and the flow view is fine). Tests, e2e, screenshots.

### T16: Submission docs (T1 session, same worktree)
**You are TASK T16:** when this is finished, a judge who opens the repo understands TenantGuard in 2 minutes and can run it in 10. Everything says TenantGuard, matches the real Python stack, and quotes only measured numbers.

You own (delegated by the coordinator for this task only): `README.md`, `docs/architecture.html`, `docs/Architecture.pdf`, new `docs/SETUP.md`, new `docs/SUBMISSION.md`. Do not touch `docs/PLAN.md` or `docs/FINAL-PLAN.md`.

Build:
1. **README.md:** the one-line pitch; the problem in 3 lines; a Mermaid architecture diagram (data / control / management planes + 5 isolation layers); a component table with ports; a quick start (Docker host + local); how to run the demo (`tools/demo.py`); the "not just a rate limit" evidence (3-mode scorecard, black-box: `ingest` 0.07 ms visible vs 20.25 ms hidden CPU from `worker/calibration.json`); repo map.
2. **docs/architecture.html + PDF:** rewrite the earlier version (it shows a FastAPI + Redis + Postgres stack that we didn't build). Show the real architecture, components and tech stack. Keep "Version 1 technical / Version 2 simple flow". Render the PDF with headless Edge like before (`msedge --headless=new --print-to-pdf`); check the page breaks.
3. **docs/SETUP.md:** the two-laptop + Tailscale setup, Docker host steps (link DOCKER-HOST-SETUP.md), local dev, env vars, tokens, ports.
4. **docs/SUBMISSION.md:** PS3 checklist → where implemented (file paths); known limitations; roadmap.
5. Numbers: only from committed result files (lab/results or calibration.json). Mark anything else "to be measured".

No code changes. Done = files rendered and checked (open the PDF pages as images via a headless screenshot, as before).

### T17: Fair billing + "Why was I slow?" (T2 session, same worktree)
**You are TASK T17:** when this is finished, every tenant sees a fair bill. They pay only for the resources they used, at calm reference prices; victims are never charged for an attack and get SLA credits. A victim can ask "Why was I slow?" and get a plain-English, anonymised answer.

You own a NEW dir `billing/` (package `tenantguard_billing`, FastAPI on :8600, Dockerfile, tests). Read-only consumer: never sends control commands.

Inputs (from env URLs; inside compose: http://w0..w8:8100, brain:8200, gateway:8000):
- worker `/stats` (cumulative per-tenant handler/bg/db CPU, per-class costs, blackbox)
- brain `/api/incidents` + WS `/ws/events`
- gateway `/admin/tenants` (control token)
- the layout (revenue, SLA)

Build:
1. **Metering:** per tenant per minute, handler + bg CPU-ms, DB ms, requests. Diff the cumulative counters (handle worker restarts via `started_at`).
2. **Reference pricing:** a price per CPU-ms per tier from yaml (propose `billing:` keys). Charge each request class at its *calm* reference cost (EWMA updated only when no incident is open), so contention never inflates a victim's bill.
3. **SLA credits:** per victim, SLO-violation-seconds (p95 > `sla_p95_ms` from the lab personas, or a yaml default) during incidents → credit % per the yaml table.
4. **True-cost line** for a named culprit: black-box CPU on its servers during its incident is attributed to it as an "unmetered cost" line (evidence: decoder confidence).
5. **"Why was I slow?":** `GET /why/{tenant}?from=&to=` → a sentence like *"Between 14:02:10 and 14:02:12, another tenant's workload slowed 1 of your 3 servers. Your traffic was moved to healthy servers in 0.9 s. A 4% service credit has been applied."* Built from brain incident stages; never reveals the other tenant.
6. API: `GET /bills`, `GET /bills/{tenant}`, `GET /why/{tenant}`. Propose contract models (`BillView`, `WhyView`) to the coordinator first.
7. Tests: counter diffing incl. restarts, calm-price freeze during incidents, credits math, anonymisation (the culprit id never appears in a victim's text), restarts.

Proof: unit tests + a run against the Docker host (I'll add the compose service and run it there; send me the exact commands).

### T18: Incident narrator (T1 session, same worktree)
**You are TASK T18:** when this is finished, every TenantGuard incident gets an instant plain-English incident report, built deterministically from the evidence pack.

You own a NEW dir `narrator/` (package `tenantguard_narrator`, FastAPI on :8700, Dockerfile, tests). Read-only consumer of the brain.

Build:
1. Subscribe to brain WS `/ws/events` (+ `/api/incidents` backfill). On `named`, `verdict` and `closed`, build an **evidence pack**.
2. A deterministic `IncidentReport` plus a `CustomerMessage` from the *anonymised* pack (no other tenant named).
3. API: `GET /reports`, `GET /reports/{incident_id}`, WS `/ws/reports`.
4. Tests: report correctness and anonymisation.

### T22: Prototype documentation pack (4 writers in parallel)
**Goal:** a new folder `prototype-docs/` that fully documents what is BUILT on master (what we show as the prototype): architecture, SOLID/design, data models, sequence diagrams, risks + mitigations, unique features, and EVERY file and EVERY function with a detailed description.

Common rules for every writer:
- Describe only code that exists on `master` (read it; cite file paths and real function names). Mark unmerged work (T10 lab wave 2, T12 prover/verdicts/calibration, T17 billing) as "in progress", never as done.
- Numbers only from committed files (`worker/calibration.json`, tests). Diagrams in Mermaid (` ```mermaid `).
- No development-tool mentions (PLAN §2.10a). Plain English, but complete.
- Function catalog format, per file: `### path/to/file.py`, a 2–4 line purpose paragraph, then a table `| Function / class | Signature | What it does | Called by |` covering EVERY function, method and class (private ones too), then "Key decisions" bullets.
- Write ONLY your own files below. Commit on your branch with a plain message.

| Writer | Files in `prototype-docs/` |
|---|---|
| T1 session (`codeastra-t1-32`, branch `task/T22a-docs`) | `00_INDEX.md` (map of all files + how to read), `01_OVERVIEW.md` (problem, what is built, status table, demo script), `02_ARCHITECTURE.md` (system, planes, 5 isolation layers, deployment two-laptop + Tailscale, component/port table: all Mermaid), `03_SOLID_AND_DESIGN.md` (SOLID principles with concrete code examples, patterns, refactoring done and possible), `06_RISKS_AND_MITIGATIONS.md`, `07_UNIQUE_FEATURES.md`, `09_CONFIG_REFERENCE.md` (every yaml key, env var, port, token), `10_TESTING.md` (suites, counts, mutation checks), `11_FILES_gateway.md`, `11_FILES_handoff.md`, `11_FILES_tools.md`, `11_FILES_narrator.md` |
| T2 session (`codeastra-t2-9a`, branch `task/T22b-docs`) | `04_DATA_MODELS.md` (every contract model + field + unit, class diagrams, layout + yaml shapes, SQLite per-tenant schema), `11_FILES_common.md`, `11_FILES_worker.md` |
| T4 session (`codeastra-t4-0e`, branch `task/T22c-docs`) | `05_SEQUENCE_DIAGRAMS.md` (request path, admission/429, DRR dispatch, detection→throttle ladder→recovery, health→rescue→decoder naming, host fault, operator/handoff decision, narrator report, UI live stream), `11_FILES_ui.md`, `11_FILES_lab.md` |
| `codeastra-2b` (main dir, no branch; coordinator commits) | `11_FILES_brain.md` |

### T24: Playwright end-to-end verification (T4 session, same worktree)
**You are TASK T24:** when this is finished, one command proves the whole running system works from a user's point of view: dashboard, gateway, brain, lab, admin, handoff and narrator. Every failure is either fixed (UI) or reported with an exact repro (backend).

You own `ui/**` (Playwright lives in `ui/e2e/` + `ui/playwright.config.ts`). New dev dependency approved: `@playwright/test`. Use the **system Edge** (`channel: "msedge"`), so no browser download is needed. Headless, 1 worker (this laptop has very little free RAM).

Build:
1. **Config:** targets come from env (`E2E_UI_URL`, `E2E_GATEWAY_URL`, `E2E_BRAIN_URL`, `E2E_LAB_URL`, `E2E_HANDOFF_URL`, `E2E_NARRATOR_URL`, `E2E_CONTROL_TOKEN`). The default is the mock server, so it runs anywhere.
2. **API smoke** (Playwright `request`):
   - gateway healthz; 401 without key; 401 on admin without token; a valid tenant request returns 200 + `X-TenantGuard-Cpu-Ms`
   - brain `/api/state` validates its shape
   - lab status
   - optional services skip cleanly when unreachable
3. **UI flows:**
   - Control Room renders 12 tiles + the 3×3 grid
   - status sentence
   - mode toggle round-trip (verify via `GET /admin/mode`, restore at the end)
   - attack launch → visible in running attacks → stop
   - Tenants page quota PATCH + restore
   - incident timeline → Case File route
   - scorecard page
   - flow view toggle
   - cases column (bridge reachable or not)
   - no console errors
4. **Live scenario test** (tagged `@live`, host only): calm → whale on K → expect a K incident/level change within 60 s → stop → expect recovery within 90 s.
5. Run the mock suite now. Run the host suite **only when the coordinator grants a host slot** (the host is busy with T12 acceptance first).
6. Fix any UI bug you find. For backend bugs, send BLOCKED/finding messages with an exact repro; don't edit other dirs.

Done = the mock suite green + the host suite results (pass/fail per test) + the list of fixed and reported issues.

### T25: Scorecard runner (T2 session)
**You are TASK T25:** when this is finished, one command runs the existing lab scenarios in all three modes (off / industry / tenantguard) and writes a summary table of MEASURED results that the dashboard Scorecard and the slides use.

You own ONLY `lab/tenantguard_lab/scorecard.py` and `lab/tests/test_scorecard.py` (plus one CLI hook line in `lab/tenantguard_lab/__main__.py`). Base the branch on `task/T10-lab-crashtest` (it has the run phase marks): `git checkout -b task/T25-scorecard task/T10-lab-crashtest`.

Build:
1. Use ONLY the lab's existing HTTP API: `POST /lab/run` (RunReq) for scenarios already registered (flood, whale, hog, memory, retry_storm) and `GET /lab/run/{id}` / `GET /lab/results`. Write no new traffic or attack code. Unregistered scenarios are skipped with a clear message.
2. `python -m tenantguard_lab scorecard --scenarios whale,flood --modes off,industry,tenantguard --runs N`: run sequentially, then write `lab/results/summary-<ts>.json`. Per scenario × mode: median/mean victim p99 (baseline/attack/after), time_to_detect, time_to_recover, attacker 429s, runs, plus brain incidents aligned to the run marks (culprit named correctly? innocents penalised?). Propose a `CrashSummary` contract to the coordinator.
3. Tests with a mocked lab/brain transport.
4. The real run happens on the Docker host via the coordinator. Send the exact command.

### T27: Dashboard clarity fix (T4 session) - user not satisfied
**You are TASK T27:** when this is finished, a person who has never seen TenantGuard can look at the Control Room for 10 seconds and say what is happening, who is affected, why, and whether the system is doing the right thing. Nothing jumps or overlaps at 1920×1080 or 1366×768.

The user's screenshot (shared in the coordinator chat) shows:
1. The status sentence renders as a huge red 6+ line paragraph that changes size each update, so the page jumps.
2. The 3×3 server grid text overlaps itself and the panels below.
3. The bottom panels (p95 chart, CPU stacked, launcher, timeline) are squashed and overlap.
4. A, B and G show THROTTLE with no visible reason, and there's no indicator of whether an attack is running.

Build:
1. **Fixed-height status bar:**
   - A one-line headline from structured data: `No attack · 12 tenants healthy` / `Attack: whale on K · 9 tenants protected` / `Server w4 slow · 4 tenants moved away · no tenant blamed`.
   - A small second line of details, truncated; the full brain sentence in a tooltip.
   - Normal text size.
2. **Scenario badge** from lab `/lab/status`: Calm traffic / Attack: whale on K (0:42 left) / No traffic.
3. **Why line on every non-normal tile:** a short reason from the latest IncidentEvent for that tenant, plus a link to its Case File. A badge never appears without a reason.
4. **Layout:**
   - Grid min-heights; fixed-size server tiles with clamped text; fixed chart heights (~220 px).
   - Nothing overlaps at 1366×768 or 1920×1080 (scrolling is fine).
   - Playwright screenshots at both sizes, plus an e2e assertion that the panels' bounding boxes don't overlap.
5. **Legend line:** `p95 = slowest 5% response time · THROTTLE = its limits are reduced · fair share = its tier's portion`.
6. Keep the T24 suites green; add tests for the headline builder and the scenario badge.

Done = screenshots at both sizes from the REAL host (fixed brain), plus the tests.

### T26: Final docs refresh (T1 session, worktree codeAstra-T1)
**You are TASK T26:** when this is finished, every doc tells the truth about the FINAL system on master (59d0920 + 3fd756b), with measured numbers.

Branch: `git checkout -b task/T26-docs-final master` (your old WIP branch task/T26-docs-refresh may be reused for text, but start from master). For this task you own `README.md`, `docs/SUBMISSION.md`, `docs/SETUP.md`, `docs/architecture.html` + `docs/Architecture.pdf`, and `prototype-docs/**`.

Facts to apply (all on master):
1. **Merged:**
   - T12 brain: DockerProver relocation proof; 5 verdicts; calibration (sustained signals, SLA floor ≈25 ms, over_use 1.6, min_hurt_neighbours 4, warn hold 5 s); held hosts; ChargeCmd sender; tuning keys now in tenantguard.yaml.
   - Also merged: T10 tiers/SLA/run marks; T25 scorecard runner; T17 billing; tier rebalance (A pro, F ent, G free, L ent; per-tenant sla_p95_ms); T28 worker memory fix; T27/T30 UI clarity.
2. **Wave-2 lab attacks are NOT included** (user decision). Say "not included", never "in progress". The faulty-server scene is triggered via the worker's /admin/fault.
3. **Measured scorecard:** `lab/results/summary-scorecard-host.json` + 18 RunResult files. Medians, victim p99 baseline→attack→after (ms):
   - whale off 149→691→254; whale industry 157→176→165; whale tenantguard 158→183→154;
   - flood off 200→162→186; flood industry 166→164→163; flood tenantguard 143→144→175;
   - innocents penalised: none in any run.

   State honestly: whale and flood are contained by the gateway limits that both protected modes share, so industry ≈ tenantguard on these two. The brain's distinct value is shown by the host-acceptance scenarios (PLAN decisions log 2026-10-13): a big-plan whale (named 99%, proved R=0.88, honest_growth, victims rerouted) and a faulty server (host_fault, 5.1× idle, no tenant blamed). Quote those from the log, labelled "host acceptance run".
   - Note the anomaly: one OFF-mode flood run recorded 1,522 refused requests (likely a mode-switch carry-over).
4. Test counts on master: gateway 94, worker 53 + 1 Linux-only skip, brain 166, lab 49, billing 25, handoff 47, narrator 9, tools 16 (459 Python); UI unit + e2e per ui/.
5. Update the `11_FILES_*` catalogs for changed code: brain (prover, verdicts, health, decoder, held hosts, charge), lab (scorecard.py, marks), billing (new file `11_FILES_billing.md`), ui (T27/T30), worker (already done in T29). Update 00_INDEX, 01_OVERVIEW status, 05 sequence diagrams (prover/verdict/held-host), 06 risks, 07 unique, 09 config (all new yaml keys), 10 testing.
6. Re-render the PDF and check it page by page.
7. No tool attribution anywhere. Plain commits.

Done = the files updated, the term grep empty, the PDF checked; report the file list.

### T31: Final code cleanup from the T26 findings (T1 session)
Branch `git checkout -b task/T31-cleanup master`. Temporary ownership for this task (other task sessions are closed): `brain/`, `lab/`, `ui/src/mock/`, `billing/`.
1. **BUG (priority):** a proven over_plan tenant pushed below warn by force_throttle can be counted in ImpactMeter.innocents_penalised. Only tenants acted on WITHOUT a verdict/score ≥ warn may count. Add a regression test that fails before the fix.
2. Lab: the 501 detail "attack '<name>' ships in Wave 2" → "attack '<name>' is not included in this build"; update test_scorecard FakeLab + its assert and lab/README (also add the `scorecard` subcommand to lab/README).
3. ui/src/mock/fixture.ts: tiers A pro, F enterprise, G free, L enterprise + sla_p95_ms from layouts/ag23.json; keep the mock suites green.
4. Stale text: the score.py docstring (over_use 1.6, min_hurt_neighbours 4); remove ladder.py's TODO(Wave 2) or reword it; lab/personas.yaml header total rate (it now sums to 121 req/s).
5. Dead code: remove health.weighted_median, HealthTracker.ratios, billing Timeline.blamed_in and Priced.waived_ms if truly unused (git grep), or give each a caller. Add /control/charge to brain/tools/fake_gateway.py.
6. Every affected suite green, the term grep empty, plain commits; update the matching 11_FILES_* rows. Report done.

### T32: Integrate Mayur's Wave 2 attacks (T1 session)
Mayur (GitHub Mayur280star, author mayurchaudhari2405) hand-wrote the 5 Wave 2 attacks: commit 228bf62 on GitHub branch `guardian/wave2-attacks`, based on the PRE-rewrite master a571f3e (same content as the new master d9f9035).
1. `git fetch origin && git checkout -b task/T32-wave2 origin/master && git cherry-pick 228bf62`. Keep Mayur as the author (cherry-pick does by default). Resolve any conflict in favour of his intent.
2. Lab: pytest green (incl. his tests/test_wave2_attacks.py).
3. UI: the T24/T27 e2e currently asserts the launcher offers EXACTLY flood/whale/hog/memory/retry_storm. Change it to the 10 attacks (+ the server picker for faulty_server). Keep it mutation-checked. vitest + mock e2e green.
4. Docs: everywhere it says the Wave 2 attacks are "not included" (README, SUBMISSION, prototype-docs 01/06/07/09/10, 11_FILES_lab/ui, lab/README, tools/README), say they ARE included, written by the team. The demo driver: blackbox_bomb no longer falls back to whale, and faulty_server/honest_spike run. Keep the measured-scorecard text (it covers whale/flood only).
5. Plain commits, term grep empty. Do not push; report done.

## Task table

| Task | Branch | Worktree | Session | Status |
|---|---|---|---|---|
| T1 Gateway | task/T1-gateway | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | **merged** |
| T7 Gateway wave 2 | task/T7-gateway-adaptive | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | **merged** |
| T11 Handoff bridge (Web PS3 convergence) | task/T11-handoff-bridge | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | **merged** |
| T14 Demo driver (`tools/demo.py`) | task/T14-demo-runbook | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | **merged** |
| T16 Submission docs | task/T16-submission-docs | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | **merged** (re-check after T10/T12) |
| T24 Playwright e2e verification | task/T24-playwright | D:\yashodeep\codeAstra-T4 | codeastra-t4-0e | assigned |
| T27 Dashboard clarity fix | task/T27-ui-clarity | D:\yashodeep\codeAstra-T4 | codeastra-t4-0e | assigned (top priority) |
| T22 Prototype docs pack (`prototype-docs/`, 20 files, 6.7k lines) | T22a/b/c + 2b | 4 writers | T1, T2, T4, 2b | **merged** |
| T23 Handoff tools token + telemetry counter bug + docstring | task/T23-handoff-token-tools | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | assigned |
| T21 Narrator template-only (user decision) | task/T21-narrator-template-only | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | **merged** (with the coordinator scrub branch) |
| T18 Incident narrator service | task/T18-narrator | D:\yashodeep\codeAstra-T1 | codeastra-t1-32 | **merged** (template-only per user) |
| T2 Workers | task/T2-workers | D:\yashodeep\codeAstra-T2 | codeastra-t2-9a | **merged** (9cebaaf) |
| T6 Workers wave 2 | task/T6-worker-blackbox | D:\yashodeep\codeAstra-T2 | codeastra-t2-9a | **merged** |
| T15 Worker calibration in-container | task/T15-worker-calib | D:\yashodeep\codeAstra-T2 | codeastra-t2-9a | **merged**; host calibration committed (fa011b3) |
| T20 Fixes: handoff bridge + demo driver (T19 review) | task/T20-handoff-demo-fixes | D:\yashodeep\codeAstra-T2 | codeastra-t2-9a | **merged** (ownership of integrations/handoff + tools/ returned to T1 session) |
| T17 Fair billing + "Why was I slow?" | task/T17-billing | D:\yashodeep\codeAstra-T2 | codeastra-t2-9a | **done, merge pending** (tests need coord/layout-balance; merges together with T12) |
| T3 Brain | task/T3-brain | D:\yashodeep\codeAstra-T3 | codeastra-t3-1b | **merged** |
| T8 Brain integration + health/rescue/decoder | task/T8-brain-detect | D:\yashodeep\codeAstra-T3 | codeastra-t3-1b | **merged** |
| T12 Brain prover + verdicts + calibration | task/T12-brain-prover | D:\yashodeep\codeAstra-T3 | codeastra-t3-1b | in progress (merges coord/layout-balance) |
| T4 UI | task/T4-ui | D:\yashodeep\codeAstra-T4 | codeastra-t4-0e | **merged** |
| T9 UI wave 2 | task/T9-ui-casefile | D:\yashodeep\codeAstra-T4 | codeastra-t4-0e | **merged** |
| T13 UI wave 3 (story + Docker-host run) | task/T13-ui-story | D:\yashodeep\codeAstra-T4 | codeastra-t4-0e | **merged** (standby; screenshot retake at rehearsal) |
| T5 Lab | task/T5-lab | D:\yashodeep\codeAstra-T5 | codeastra-t5-45 | **merged** |
| T10 Lab wave 2 + crash-test | task/T10-lab-crashtest | D:\yashodeep\codeAstra-T5 | codeastra-t5-45 | **partial, done** (tiers, SLA, run marks); merges with T12. Wave 2 runners: user decision. Scorecard runner moved to T25 |
| T25 Scorecard runner (OFF/Industry/TenantGuard summary) | task/T25-scorecard | D:\yashodeep\codeAstra-T2 | codeastra-t2-9a | **done** (9738181; merges in the T12 batch: it carries T10 + layout) |
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
- 2026-10-12: The Docker host (friend's laptop, GitHub reki-shu) is set up per `docs/DOCKER-HOST-SETUP.md`. The coordinator drives it via the docker context `tg-host` over SSH (key `~/.ssh/tenantguard_ed25519`), building from the coordinator's files. This laptop has 8 cores / 7.8 GB RAM, too small to also host the full stack beside 7 dev sessions.
- 2026-10-12 (user): **No tool mentions anywhere:** the narrator is template-only (no external model, no key), and no development tool is named or disclosed in any file or commit.
- 2026-10-12 (user): **Project name is TenantGuard.** The old name was a mistake and must not appear anywhere.
  - Package `tenantguard_common`; config `tenantguard.yaml`; env prefix `TENANTGUARD_`; headers `X-TenantGuard-Tenant` / `-Class` / `-Cpu-Ms`; mode literal `tenantguard` (off | industry | tenantguard).
  - Service packages: `gateway/tenantguard_gateway`, `worker/app`, `brain/tenantguard_brain`, `lab/tenantguard_lab`.
  - Persona F renamed to FitZone.
  - Repo: private GitHub `yashodeepchaudhari/tenantguard`, collaborator `reki-shu` (friend's laptop = Docker host).
- 2026-10-12 (user): **Two-laptop setup.**
  - The friend's laptop = Docker host ("Laptop A"): gateway, workers, spare, quarantine and brain in compose. The brain must sit with docker.sock.
  - This laptop = dev + traffic ("Laptop B"): all dev sessions, the lab and the UI, pointing at `<friend-ip>`.
  - Dev needs no Docker; Docker is only required at gate G1.
  - Code reaches Laptop A via a private GitHub remote (pending user OK).
  - Laptop A needs ≥ 8 cores / 16 GB; with fewer, lower `worker.cpus` or use the 6-worker layout.
  - Services must therefore bind `0.0.0.0` and allow CORS from the UI origin; nothing may assume 127.0.0.1 for a peer service (always read URLs from config/env).
- 2026-10-12 (2b review of T2): these fixes are folded into T6 as step 0, before the black-box work:
  - #1/#2: tenant allow-list, exact match, else 400 unknown_tenant
  - #3: deterministic suite (drain jobs before close, close under lock, poll instead of sleep)
  - #4: export bounded by `worker.max_request_s`
  - #5: bg backlog caps `worker.bg_pending`
  - #6: worker-wide memory cap
  - #7: FaultReq from contracts
  - #8: 500s carry the CPU header

  T1: treat a missing `X-TenantGuard-Cpu-Ms` header as 0.
- 2026-10-12 (T3, accepted): the noisy score uses **barcode-scoped shares**: tenant CPU / capacity of its own servers vs weight / Σ active weights per server. victim_impact is averaged over barcode neighbours. Global shares flagged calm free-tier tenants at ~1.55×. New keys are under `brain.noisy`.
- 2026-10-12 (T1, accepted): TTLs keep expiring during fail-static (auto-undo beats stale punishment). Overrides apply only in `tenantguard` mode. Bounded wait applies in all modes. New keys: `gateway.batch_routes`, `gateway.upstream_timeout_s`.
- 2026-10-12: This laptop hits ~97% CPU with 11 workers + sessions, so latency tails here are machine noise. Real numbers come from the Docker host (friend's laptop).
- 2026-10-12 (T7, accepted):
  - `ChargeCmd` contract added; `QueueStat.retries` added; `gateway.retry_budget` key added.
  - A retry = a repeated Idempotency-Key, or a repeat of a full fingerprint (method + path + full query + body hash) whose previous attempt FAILED within window_s. Class-level matching would punish normal repeat traffic.
  - Lanes are express > interactive > batch.
- 2026-10-12 (2b review of T1/T3/T4):
  - **Security:** `/admin/*` and `/control/*` on the gateway require header `X-TenantGuard-Control-Token` (env `TENANTGUARD_CONTROL_TOKEN`, default `dev`). The brain, UI (`VITE_CONTROL_TOKEN`) and lab send it.
  - `/api/` path traversal blocked; x-admin-token and client x-tenantguard-* stripped.
  - TTLs finite and ≤ `gateway.ttl_max_s`; quotas finite and ≤ `gateway.quota_max`.
  - Brain: time-based baseline (`baseline_half_life_s`, frozen while a neighbour over-uses), hold by `hold_ratio`, sends serialised per tenant, reconcile overrides on start.
  - The UI reads thresholds from `StateSnapshot.noisy_thresholds`.
  - Gateway fixes are T7 step 0; brain fixes T8; UI fixes T9; lab token T5.
- 2026-10-12: **Docker host is live.**
  - Friend's laptop `laptop-f8bqbdsi\preeti`: 20 logical CPUs, Docker VM 8 GB.
  - Reached over **Tailscale 100.112.232.8** (stable across networks; Wi-Fi IP 10.228.100.60 is a fallback). SSH alias `tg-host` is in ~/.ssh/config.
  - Docker context `tg-host`. Stack command: `docker --context tg-host compose -p codeastra up -d --build`, run from the main repo; it builds from the coordinator's files.
  - The UI and lab on this laptop use `http://100.112.232.8:8000` (gateway) and `:8200` (brain).
  - Only the coordinator drives the host; sessions ask the coordinator for host runs.
- 2026-10-12 (T7, merged): gateway path guard (raw path forwarded), control-token routers, finite TTL/quota bounds, capped body stream, the X-Request-Id rule, disconnect debit. Gradient window on worker-side *excess* latency; three lanes; retry budget; charge/reference_costs (tenantguard only). **Lab note:** the whale must vary its query, because identical failed searches count as retries.
- 2026-10-12 (T11, accepted):
  - The partner wire format lives only in the bridge; `CaseView` etc. in contracts §6 use epoch + `*_ist`.
  - The bridge assigns `INC-NNNN` case IDs (persisted) next to incident_id.
  - Human release/extend go to the brain as `OperatorCmd` (`POST /api/operator`, T8 adds it) so humans and the ladder never fight. Quarantine approve → gateway BlockCmd, TTL-bound.
  - Severity: warn low, throttle/deprioritize medium, quarantine/blocked high. Default verdict over_plan until the brain emits verdicts.
- 2026-10-12 (T8, merged):
  - Server health = median over its tenants of cell p95 (a whale's own slow requests don't count). Canary fallback.
  - Rescue never moves a named culprit; rescue comes first, and the ladder acts only if harm persists.
  - `srv-*` incidents for server-level detection.
  - `POST /api/operator` is live.
- 2026-10-12 (T9, merged): the brain ends incidents with `closed`, which the UI treats as resolved. TenantView.p95_ms = 0 is shown as "no traffic".
- 2026-10-12: **Docker host runbook (verified):**
  - Remote `docker --context tg-host compose` fails when starting/building in parallel: Windows sshd resets the concurrent SSH sessions. So:
    - (a) build via the context one at a time, `COMPOSE_PARALLEL_LIMIT=1 docker --context tg-host compose -p codeastra build <svc>`, or build on the host;
    - (b) deploy source with `git archive --format=tar master | ssh tg-host "tar -x -f - -C C:\Users\preeti\tenantguard-deploy"`;
    - (c) run `ssh tg-host "cd /d C:\Users\preeti\tenantguard-deploy && docker compose -p codeastra up -d --build"`, a single SSH session.
  - All 13 containers are healthy. From this laptop: gateway `http://100.112.232.8:8000` (healthz 200, admin 401 without token), brain `:8200`; mode tenantguard, fleet CPU 2% idle.
  - Workers are reachable only inside the Docker network.
- 2026-10-12 (calm run on the Docker host): the brain flagged servers as slow with NO attack.
  - Cause 1: the AG(2,3) tier assignment put ALL 4 enterprise tenants on w0 (load 21 vs 10 on w2).
    - Fix: rebalanced tiers on branch `coord/layout-balance`: A pro, F enterprise, G free, L enterprise; D, J enterprise and K pro kept for the story.
    - Server load spread is now 11–16 rps; max 2 enterprise per server.
    - It breaks one tuned brain test, so T12 merges the branch and adapts the test. The lab personas' rps must follow the new tiers (T10).
  - Cause 2: peer ratios are noisy at low per-tick counts. T12 calibrates on the host.
- 2026-10-12 (T6, T11 merged): worker isolation allow-list + black-box vendor indexer; handoff bridge (`integrations/handoff`, :8400, compose profile `handoff`).
- 2026-10-12 (T19 review of handoff + demo): fixed in T20 by the T2 session, which owns `integrations/handoff/` + `tools/` for this task (the T1 session is on T18, in `narrator/`).
  - Partner updates fail closed: no token configured → 503. Compose keeps an empty default, because a `:?` would break every `compose up`.
  - Hold actions are deduplicated.
  - History and cases pruned.
  - Per-message exception isolation in the bridge.
  - Demo preflight for attacks the lab can't run; robust cleanup; config-based defaults.
- 2026-10-12 (user): **Separate frozen DEMO stack** for the 8:30 PM mentoring checkpoint, isolated from development.
  - Compose project `tgdemo` on the Docker host, host ports 9000 (gateway) / 9200 (brain) / 9300 (lab) / 9400 / 9600 / 9700, set via the `*_PORT` env vars now in compose.
  - Deployed from git tag `demo-vN` into `C:\Users\preeti\tenantguard-demo`.
  - Dev/test keeps project `codeastra` on 8000/8200/8300. No session ever touches `tgdemo`; only the coordinator deploys it, and only on a new tag.
  - The brain must find ITS OWN spare: filter by label `com.docker.compose.project`, not only by service.
- 2026-10-13: **T12 host acceptance.** Step 0 PASS (DockerProver). 1a idle PASS (0 incidents in 5 min). **1b calm FAIL:** in 270 s of calm traffic there were 78 srv detections across all 9 servers (ratio 0.6–19.3, e.g. 54 ms vs 5 ms), 20 named, 19 ladder level changes. Likely cause: CFS throttling tail spikes on 0.5-CPU cgroups against a 3 s p95 window. Back to T12 for calibration.
- Session names change on every restart: always confirm with ListAgents. Coordinator is currently `codeastra-b1`.
- 2026-10-13 (user: "A"): **the 5 Wave 2 attack runners are dropped** (blackbox_bomb, background_bomb, cache_flood, faulty_server, honest_spike). The demo and scorecard use the existing flood, whale, hog, memory and retry_storm. The faulty-server scene is triggered by the coordinator with `docker exec … /admin/fault` on the host. The UI attack launcher should not offer the dropped attacks; docs say "not included" rather than "in progress".
- 2026-10-13: **T12 final host acceptance PASSED** on 7f72906, run inside the lab container on the host: idle 0 events; calm 0 ladder moves (2 real w7 slowdowns rerouted, no tenant blamed); K big-plan whale named 99%, proved R=0.88, honest_growth, held hosts w1/w8 judged first; w4 fault host_fault 5.1x idle, no tenant blamed; 0 overrides left. Batch merged: layout-balance, T10, T25, T17, T12. Master: 459 tests green. Brain tuning keys moved into tenantguard.yaml.
- 2026-10-13 (user): GitHub collaborator `Mayur280star` (Mayur Dipak Chaudhari) invited with write access, alongside `reki-shu`.
- 2026-10-13: History rewrite #2 done (2 commit messages reworded; master + v2/main force-pushed by the user; coord/no-ai deleted). A third effort exists: branch `v2/main` + worktree `D:\yashodeep\codeAstra-v2` ("projects" redesign, 8 commits 03:25–05:12, owner session closed): cleaned, not merged.
- 2026-10-13: Mayur hand-wrote the 5 Wave 2 attacks (branch guardian/wave2-attacks, commit 228bf62), reversing the "not included" state. T32 integrates them.
- 2026-10-12: Cost bucket is debited with the worker-measured CPU (`X-TenantGuard-Cpu-Ms`) rather than an estimate, so it is a real resource quota, not a request count.
