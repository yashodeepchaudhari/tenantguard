# TenantGuard: submission notes

CodeAstra 2.0 · Cloud Computing & Distributed Systems · **PS 3: Multi-Tenant Isolation and Noisy-Neighbor Defense**.

Status key: **done** = merged on `master` with tests; **measured** = backed by a committed result file; **not included** = deliberately left out of this submission.

## PS 3 checklist → where it is implemented

| PS 3 item | Status | Where (files) |
|---|---|---|
| **Tenant identity** | done | `gateway/tenantguard_gateway/tenants.py` (SHA-256 of `X-API-Key` → tenant; only the hash is stored). Workers trust only the gateway-set `X-TenantGuard-Tenant` (`worker/app/tenancy.py`) |
| **Resource quotas** | done | Rate, burst, CPU-ms cost and concurrency per tier and per tenant: `tenantguard.yaml` (`tiers`), `gateway/tenantguard_gateway/admission.py`, `buckets.py`. Per-tenant memory budget (`worker/app/memory.py`), body limit, one SQLite file and a cache share per tenant (`worker/app/store.py`, `cache.py`) |
| **Rate limiting** | done | Token bucket + cost bucket (debited with the worker-measured `X-TenantGuard-Cpu-Ms`) + concurrency cap + retry budget (`gateway/tenantguard_gateway/retry.py`). 429 with an accurate `Retry-After` and `RateLimit` / `RateLimit-Policy` headers |
| **Workload scheduler** | done | Weighted deficit round robin per tenant in three lanes (express / interactive / batch, batch shed first), bounded queue wait (503), gradient concurrency window per worker: `gateway/tenantguard_gateway/scheduler.py`, `limiter.py`, `core.py` |
| **Usage metrics** | done | Per (tenant, server) latency p50/p95 and errors every 250 ms (`TelemetryFrame` on WS `/internal/telemetry`); per-tenant handler, background, black-box and DB CPU (`worker/app/meter.py`, `/stats`); Prometheus `/metrics` on the gateway |
| **Noisy-neighbour detector** | done | Usage-share noisy score × victim impact (`brain/tenantguard_brain/score.py`); server health by peer ratio (`health.py`); rescue by route-around (`rescue.py`); barcode decoder naming the culprit with a confidence, or a host fault with no tenant blamed (`decoder.py`, `stage3.py`). Calibrated on the Docker host so calm traffic raises nothing: signals must be sustained over a window, a server is never "slow" below the larger of 25 ms and half the strictest SLA on it, a tenant is flagged only above 1.6× its fair share with at least 4 hurt neighbours, and warn holds 5 s. Servers the culprit's neighbours share (held hosts) are judged first. All of these keys are in `tenantguard.yaml` (`brain.noisy`, `brain.health`, `brain.decoder`, `brain.verdicts`) |
| **Relocation proof** (move the suspect to the spare, show the slowdown follows it) | done | `brain/tenantguard_brain/prover.py`. `DockerProver` sizes the spare through the Docker socket, relocates the named culprit and computes R = 1 − post/pre slowdown, confirming only if the harm followed it. `HostProver` drains a suspect host and measures it idle. Needs the Docker host (`TENANTGUARD_PROVER=auto` uses the socket when it answers; otherwise no proofs are run) |
| **Five verdicts** | done | `brain/tenantguard_brain/verdicts.py`: `mitigated_no_blame` (not confirmed: nobody penalised), `over_plan` (limits tightened through the ladder), `costly_pattern` (only the class that grew moves to the quarantine lane), `honest_growth` (stays on the spare as a burst lane, no penalty), `host_fault` (host stays drained until healthy). Every action is TTL-bound and limited by an action budget; repeat offenders get their last verdict without a new proof |
| **Adaptive throttling** | done | Graduated ladder normal → warn → throttle (AIMD) → deprioritize → quarantine, with hold times, hysteresis and recovery (`brain/tenantguard_brain/ladder.py`). Every action carries a TTL and auto-reverts (`gateway/tenantguard_gateway/overrides.py`). Fail-static gateway when the brain is silent. Operator holds (`POST /api/operator`) so humans and the ladder never fight |
| **Admin API** | done | `GET/POST/DELETE /admin/tenants`, `PATCH /admin/tenants/{id}/quota`, `GET/POST /admin/mode`, `GET /admin/overrides`, `GET /admin/reference_costs`, all behind a control token (`gateway/tenantguard_gateway/app.py`) |
| **Dashboard** | done | `ui/src/pages/`: ControlRoom (tiles, server grid, live p95 and CPU-share charts, noisy bars, incident timeline, fairness and utilization gauges, mode toggle, attack launcher), Tenants (quota edit), Cases + CaseFile, Scorecard |
| **"Not just a rate limit"** | measured | Hidden work on the Docker host (`worker/calibration.json`): `POST /api/ingest` shows **0.066 ms** of handler CPU but causes **20.25 ms** of black-box indexer CPU. The 3-mode scorecard (OFF / Industry / TenantGuard) is in `lab/results/summary-scorecard-host.json` with 18 run files (see below) |
| Traffic + attacks | done | 12 Poisson personas whose rates and SLA targets (`sla_p95_ms`) follow the tiers; flood, whale, hog, memory and retry_storm (`lab/tenantguard_lab/attacks.py`); run marks; the scorecard runner (`lab/tenantguard_lab/scorecard.py`) |
| Wave-2 attacks (blackbox_bomb, background_bomb, cache_flood, faulty_server, honest_spike) | not included | A decision for this submission: the lab answers 501 for them. The faulty-server scene is produced with the worker's `/admin/fault`; `tools/demo.py` uses the whale in place of the bomb and skips the honest spike |
| Convergence (Web PS 3 "Digital Handoff") | done | `integrations/handoff/`: incidents become partner cases (owner, pending actions, deadlines, history); acknowledge and approve flow back as TTL-bound control. Agreed samples in `docs/convergence/` |
| Fair billing + "Why was I slow?" | done | `billing/` (:8600): bills at calm reference prices (victims are never charged for contention), SLA credits from each tenant's `sla_p95_ms`, an "unmetered" true-cost line for a named culprit's black-box CPU, and an anonymised "why" text for victims |
| Incident reports | done | `narrator/` (:8700): a deterministic incident report per incident and an anonymised customer message, republished whenever the evidence changes |
| Demo | done | `tools/demo.py`: the 4-minute script, driven over HTTP, with live numbers |

Tests: `python -m pytest` in `gateway/`, `worker/`, `brain/`, `lab/`, `billing/`, `integrations/handoff/`, `narrator/` and `tools/` (459 tests: gateway 94, worker 53 + 1 Linux-only skip, brain 166, lab 49, billing 25, handoff 47, narrator 9, tools 16); `npm test` in `ui/` (76 unit tests) and `npm run e2e` (27 Playwright end-to-end tests in `ui/e2e/`, 26 by default; the `@live` one needs `E2E_LIVE=1`). The gateway, handoff, narrator and demo test suites were also checked by mutation: each protected behaviour was broken on purpose, and a test failed every time.

## Measured numbers we quote

Only from committed files:
- `worker/calibration.json` (Docker host, one worker limited to 0.5 CPU, sequential client, mean per request):
  - `light`: 0.396 ms handler CPU, 0.95 ms whole-process CPU.
  - `search`: 3.831 ms handler CPU.
  - `write`: 0.185 ms handler + 1.429 ms background-job CPU.
  - `ingest`: 0.066 ms handler + 20.25 ms black-box CPU (23.5 ms whole-process).
  - `export`: 19.498 ms handler CPU.
- `lab/results/summary-scorecard-host.json` (Docker host, 3 runs per cell, attacker K, 20 s calm then 40 s attack). Median victim p99, baseline → attack → after, in ms:
  - whale: OFF 149 → **691** → 254; Industry 157 → 176 → 165; TenantGuard 158 → 183 → 154.
  - flood: OFF 200 → 162 → 186; Industry 166 → 164 → 163; TenantGuard 143 → 144 → 175.
  - Innocent tenants penalised: none, in any run.
  - Honest reading: both attacks are contained by the gateway limits that Industry and TenantGuard share, so the two protected modes are about equal here, and the brain named no culprit in these runs.
  - Anomaly: one OFF flood run (`20261012-222805-flood-off.json`) recorded 1,522 refused requests, which OFF should not produce; most likely a carry-over from the previous mode switch. Another OFF flood run (`20261012-222323-flood-off.json`) recorded 6,415 attacker 503s, from the bounded queue wait that applies in every mode.
- **Host acceptance run** (2026-10-13, inside the lab container on the Docker host), which shows what the brain adds beyond the shared limits:
  - idle: 0 events; calm traffic: 0 ladder moves (2 real slowdowns on w7 routed around, no tenant blamed);
  - big-plan whale K: named with 99% confidence, proved by relocation with R = 0.88, verdict `honest_growth`, held hosts w1/w8 judged first, victims rerouted;
  - faulty server w4 (`/admin/fault`): verdict `host_fault` at 5.1× idle latency, no tenant blamed;
  - 0 overrides left at the end.

## Known limitations
- **One gateway, in-memory state.** Quotas, queues and overrides live in one gateway process. Fail-static covers a brain outage, but a gateway restart resets buckets and overrides (overrides expire by TTL anyway).
- **Measurements need the Docker host.** CPU limits and noisy-neighbour effects are only realistic in containers with cgroup limits. On a single loaded laptop, latency tails are machine noise, not results.
- **Proofs need the Docker host.** The relocation prover resizes and uses the spare container through the Docker socket (found by its compose project and service labels). Without the socket, culprits are still named and victims rescued, but a high-confidence culprit is shown as "named, unproven" and gets no verdict; only the rescue and the usage ladder act (a low-confidence one gets `mitigated_no_blame`).
- **Barcode naming assumes the AG(2,3) layout** (12 tenants on 9 servers). Tenants added at runtime through `POST /admin/tenants` are served and limited by the gateway, but the brain only reasons about the layout's tenants.
- **Background CPU** is charged to the cost bucket once per second by the brain (`ChargeCmd`, tenantguard mode only), so it counts against the tenant with up to a second of delay. Black-box CPU is unattributed by design: it is what the barcode decoder exists to explain (billing shows it as an "unmetered" line for a named culprit).
- **The control token is a shared secret** (default `dev`). There is no TLS between services; we assume a closed demo network.

## Roadmap
- Wave-2 attack runners (black-box bomb, background bomb, cache flood, faulty server, honest spike) in the lab, and a scorecard scenario where hidden work separates TenantGuard from Industry.
- A multi-gateway deployment with shared limiter state (Redis or CRDT counters) and a highly available brain.
- Larger layouts (STS(13), AG(2,5)) for more tenants per fleet, with the same "any two tenants share at most one server" property.
- Kernel signals (PSI, eBPF) for noisy-neighbour evidence below the process level; GPU and memory-bandwidth fairness.
- A Kubernetes operator: barcode placement, the per-node spare and quarantine pools, and TTL-bound control as CRDs.
