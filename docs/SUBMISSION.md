# TenantGuard: submission notes

CodeAstra 2.0 · Cloud Computing & Distributed Systems · **PS 3: Multi-Tenant Isolation and Noisy-Neighbor Defense**.

Status key: **done** = merged with tests; **in progress** = being built on a task branch, not merged at the time of writing; **planned** = designed, not started; **to be measured** = no committed result file yet.

## PS 3 checklist → where it is implemented

| PS 3 item | Status | Where (files) |
|---|---|---|
| **Tenant identity** | done | `gateway/tenantguard_gateway/tenants.py` (SHA-256 of `X-API-Key` → tenant; only the hash is stored). Workers trust only the gateway-set `X-TenantGuard-Tenant` (`worker/app/tenancy.py`) |
| **Resource quotas** | done | Rate, burst, CPU-ms cost and concurrency per tier and per tenant: `tenantguard.yaml` (`tiers`), `gateway/tenantguard_gateway/admission.py`, `buckets.py`. Per-tenant memory budget (`worker/app/memory.py`), body limit, one SQLite file and a cache share per tenant (`worker/app/store.py`, `cache.py`) |
| **Rate limiting** | done | Token bucket + cost bucket (debited with the worker-measured `X-TenantGuard-Cpu-Ms`) + concurrency cap + retry budget (`gateway/tenantguard_gateway/retry.py`). 429 with an accurate `Retry-After` and `RateLimit` / `RateLimit-Policy` headers |
| **Workload scheduler** | done | Weighted deficit round robin per tenant in three lanes (express / interactive / batch, batch shed first), bounded queue wait (503), gradient concurrency window per worker: `gateway/tenantguard_gateway/scheduler.py`, `limiter.py`, `core.py` |
| **Usage metrics** | done | Per (tenant, server) latency p50/p95 and errors every 250 ms (`TelemetryFrame` on WS `/internal/telemetry`); per-tenant handler, background, black-box and DB CPU (`worker/app/meter.py`, `/stats`); Prometheus `/metrics` on the gateway |
| **Noisy-neighbour detector** | done | Usage-share noisy score × victim impact (`brain/tenantguard_brain/score.py`); server health by peer ratio (`health.py`); rescue by route-around (`rescue.py`); barcode decoder naming the culprit with a confidence, or a host fault with no tenant blamed (`decoder.py`, `stage3.py`) |
| Relocation proof (move the suspect to the spare, show the slowdown follows it) | in progress | Interface in `brain/tenantguard_brain/prover.py`; implementation on branch `task/T12-brain-prover` (needs the Docker host) |
| Five verdicts (costly pattern, over plan, honest growth, host fault, mitigated / no blame) | in progress | Names, host-fault and no-blame decoding are done; the full classifier is in T12 |
| **Adaptive throttling** | done | Graduated ladder normal → warn → throttle (AIMD) → deprioritize → quarantine, with hold times, hysteresis and recovery (`brain/tenantguard_brain/ladder.py`). Every action carries a TTL and auto-reverts (`gateway/tenantguard_gateway/overrides.py`). Fail-static gateway when the brain is silent. Operator holds (`POST /api/operator`) so humans and the ladder never fight |
| **Admin API** | done | `GET/POST/DELETE /admin/tenants`, `PATCH /admin/tenants/{id}/quota`, `GET/POST /admin/mode`, `GET /admin/overrides`, `GET /admin/reference_costs`, all behind a control token (`gateway/tenantguard_gateway/app.py`) |
| **Dashboard** | done | `ui/src/pages/`: ControlRoom (tiles, server grid, live p95 and CPU-share charts, noisy bars, incident timeline, fairness and utilization gauges, mode toggle, attack launcher), Tenants (quota edit), Cases + CaseFile, Scorecard |
| **"Not just a rate limit"** | partly measured | Hidden work measured on the Docker host (`worker/calibration.json`): `POST /api/ingest` shows **0.066 ms** of handler CPU but causes **20.25 ms** of black-box indexer CPU. The 3-mode scorecard (OFF / Industry / TenantGuard) is **to be measured**: no `lab/results/*.json` is committed yet |
| Traffic + attacks | done / in progress | 12 Poisson personas; flood, whale, hog, memory and retry_storm are done (`lab/tenantguard_lab/attacks.py`). blackbox_bomb, background_bomb, cache_flood, faulty_server and honest_spike are in progress (T10; the lab answers 501 until then) |
| Convergence (Web PS 3 "Digital Handoff") | done | `integrations/handoff/`: incidents become partner cases (owner, pending actions, deadlines, history); acknowledge and approve flow back as TTL-bound control. Agreed samples in `docs/convergence/` |
| Demo | done | `tools/demo.py`: the 4-minute script, driven over HTTP, with live numbers |

Tests: `python -m pytest` in `gateway/`, `worker/`, `brain/`, `lab/`, `integrations/handoff/` and `tools/`; `npm test` in `ui/`. The gateway, handoff and demo test suites were also checked by mutation: each protected behaviour was broken on purpose, and a test failed every time.

## Measured numbers we quote

Only from committed files:
- `worker/calibration.json` (Docker host, one worker limited to 0.5 CPU, sequential client, mean per request):
  - `light`: 0.396 ms handler CPU, 0.95 ms whole-process CPU.
  - `search`: 3.831 ms handler CPU.
  - `write`: 0.185 ms handler + 1.429 ms background-job CPU.
  - `ingest`: 0.066 ms handler + 20.25 ms black-box CPU (23.5 ms whole-process).
  - `export`: 19.498 ms handler CPU.
- Victim p99 per mode and attack, time to rescue and time to verdict: **to be measured** (they will come only from `lab/results/*.json`).

## Known limitations
- **One gateway, in-memory state.** Quotas, queues and overrides live in one gateway process. Fail-static covers a brain outage, but a gateway restart resets buckets and overrides (overrides expire by TTL anyway).
- **Measurements need the Docker host.** CPU limits and noisy-neighbour effects are only realistic in containers with cgroup limits. On a single loaded laptop, latency tails are machine noise, not results.
- **Relocation proof and the full verdict classifier** are in progress (T12). Until then a named culprit is "named, not yet proven".
- **Barcode naming assumes the AG(2,3) layout** (12 tenants on 9 servers). Tenants added at runtime through `POST /admin/tenants` are served and limited by the gateway, but the brain only reasons about the layout's tenants.
- **Background CPU** is not yet charged to the cost bucket. The gateway accepts `POST /control/charge`, but the brain does not send it yet (planned). Background CPU is metered per tenant on the workers. Black-box CPU is unattributed by design: it is what the barcode decoder exists to explain.
- **The control token is a shared secret** (default `dev`). There is no TLS between services; we assume a closed demo network.

## Roadmap
- Merge the relocation prover and the five verdicts (T12), the wave-2 attacks and the crash-test runner (T10), then publish the measured 3-mode scorecard.
- A multi-gateway deployment with shared limiter state (Redis or CRDT counters) and a highly available brain.
- Larger layouts (STS(13), AG(2,5)) for more tenants per fleet, with the same "any two tenants share at most one server" property.
- Kernel signals (PSI, eBPF) for noisy-neighbour evidence below the process level; GPU and memory-bandwidth fairness.
- A Kubernetes operator: barcode placement, the per-node spare and quarantine pools, and TTL-bound control as CRDs.
