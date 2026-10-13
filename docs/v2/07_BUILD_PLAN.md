# 07 — v2 build plan (ready to hand to task sessions)

**Each task lists:** the files it owns, the functions it delivers (from `04_FUNCTIONS.md`), the tests it must pass, and its acceptance check.
**Order** follows the dependency graph in `06_DIAGRAMS.md` §21.
**Branch naming:** `v2/<task>`. Build v2 on a fresh branch from master *after* v1 is submitted, or as a separate line of work (01 §6).

## 0. Ground rules (same as v1 PLAN.md)
- Tasks own disjoint files. Contracts (V1) change only through the coordinator.
- Each function: ≤ 30 lines, ≤ 4 parameters, a one-line docstring, at least one production caller (04 §1).
- **CI gates:** `pytest`, `ruff` (C901 ≤ 8, PLR0913), `vulture --min-confidence 80`, import-linter (pure modules import no I/O; only `docker_adapter` imports the Docker client).
- Nothing touches the live Docker host except the V4 host checks and V8, and only in agreed acceptance slots.

## 1. Tasks
### V1: contracts and config
- **Files:** `common/tg_common/contracts.py` (v2 models, 05 §1), `common/tg_common/config.py`, the `tenantguard.yaml` v2 sections.
- **Tests:**
  - Models round-trip as JSON.
  - Validators reject floor > cap, negative CPU, bad ids, ttl > 3600.
- **Accept:** other tasks import the models without editing them.

### V2: registry and Admin API
- **Files:** `brain/tg_brain/registry.py`, `registry_store.py`, the `admin` router.
- **Functions:** `check_capacity`, `set_pool`, `create_project`, `update_project`, `add_tenant`, `remove_tenant`, `load`, `save`.
- **Tests:**
  - **Property test (hypothesis):** any accepted registry satisfies I1–I3.
  - The key is shown once and only its hash is stored.
  - An atomic save survives a kill between the temp write and the rename.
  - Seeding from yaml gives the 05 §3 registry.
- **Accept:** `POST` project/tenant → `GET /api/state` shows it; an over-capacity project → 422 listing the violated invariant.

### V3: allocator and scaler (pure)
- **Files:** `allocator.py`, `scaler.py`.
- **Functions:** `estimate_demand`, `water_fill`, `allocate`, `replica_memory_mb`, `replicas_needed`, `cpu_settings`, `plan`.
- **Tests:**
  - **Golden:** 03 §3.1 and §3.2 tables, exact to 3 decimals.
  - **Property:**
    - Σ alloc ≤ usable.
    - alloc_p ≥ min(demand_p, floor_p).
    - alloc_p ≤ cap_p.
    - Work-conserving: if any want is unmet, then Σ alloc = usable.
    - Weights respected among uncapped projects.
  - **Scaler property:** Σ memory after every prefix of the action list ≤ usable (I5); drains always come before starts in the same plan.
  - **Hysteresis:** up after 2 ticks, down after 30, zero after 300 only with `warm_min` 0.
  - Golden: 03 §5.3 replica counts.
- **Accept:** 100% branch coverage on both files.

### V4: docker_adapter
- **Files:** `brain/tg_brain/docker_adapter.py` (start from T12 `DockerControl`).
- **Functions:** `list_replicas`, `apply`, `cpu_ms`.
- **Tests:**
  - Fake Engine API server (httpx MockTransport): create/start/stop/update bodies are exactly as specified, with labels set, `CgroupParent`, `NanoCpus`, `CpuShares` and `Memory`.
  - `apply` never raises; failures come back as results.
  - Reconcile after restart rebuilds the replica set from labels.
- **Host checks** (acceptance slot):
  - **T1:** `tg-pool.slice` exists with `cpu.max` = pool.
  - **T2:** two busy containers with shares 3:1 get about 3:1 CPU under contention, and the idle one's share flows to the busy one.
  - **T3:** measure the container start time with p50/p95 over 20 starts. This replaces the 1–3 s estimate.
- **Accept:** T1–T3 numbers written to `lab/results/v2-host-checks.json`.

### V5: gateway v2
- **Files:** `gateway/tg_gateway/*`.
- **Keep:** `tenants.py`, `retry.py`, `buckets.py` (token only), `scheduler.py`, `limiter.py`.
- **Remove:** `router.py`, the cost bucket, route/relocate/drain overrides.
- **Functions:** `identify`, `classify` (size bucket), `admit`, `ProjectQueues`, `route` (rendezvous + pin + bounded spill), `observe`, `request_wake`, `on_snapshot`, `costs.reference_cost`, `costs.update`.
- **Tests:**
  - **Rendezvous:**
    - Adding the 4th replica moves ≈ 1/4 of tenants (±10% over 1000 tenants).
    - A tenant is sticky while its replica is ready.
    - A dead replica moves only its own tenants.
  - **Isolation:** a flood in project X never delays dequeue in project Y (separate queue sets).
  - **Wake:** a zero-replica project queues requests and sends wake once per 2 s; queued items are served when the snapshot brings a ready replica; expiry at `cold_wait_ms` → 503.
  - **Snapshot swap is atomic:** concurrent requests see either the old or the new version, never a mix.
  - Costs seeded from `worker/calibration.json`.
- **Accept:** the v1 gateway tests that still apply pass; the stub-worker load test shows no cross-project queueing.

### V6: control loop, detector, policy, publisher, collect
- **Files:** `control.py`, `detector.py`, `policy.py`, `publisher.py`, `collect.py`; `ladder.py` reused.
- **Delete:** `decoder.py`, `health.py`, `rescue.py`, `stage3.py`, `prover.py`, `verdicts.py`, and the matrix part of `sim.py`.
- **Tests:**
  - **Tick order** matches 05 §4 (a spy records the call sequence).
  - Stale telemetry → no scaling.
  - **Attribution:** synthetic stats where the unattributed remainder is 0, and where it is 40%.
  - **Score:** v1 golden cases re-run with project scope.
  - **Split:** with 8 tenants and a hidden culprit, it is found in 3 rounds for every culprit position (simulated observations); it aborts cleanly when there is no memory for the temporary replica.
  - **External contention:** all projects slow, CPU within alloc → finding with no tenant.
  - `apply_finding` writes the event before changing the ladder.
- **Accept:** the sim harness (`brain/tools/sim_v2.py`) runs the 8 scenarios in §3 offline.

### V7: worker changes
- **Files:** `worker/app/blackbox.py` (`docs_by_tenant` in `/stats`), `worker/app/config.py` (tenant list from `TENANTGUARD_TENANTS` env, set by the brain per project).
- **Tests:** unit test of the counter; `/stats` contains it; the env tenant list overrides the yaml.

### V8: lab v2 and scorecard
- **Files:** `lab/tenantguard_lab/*` (personas per project), `lab/tenantguard_lab/scorecard.py` (T25 base).
- **Modes:**

| Mode | What it means |
|---|---|
| `off` | One shared project, FIFO, no limits |
| `industry` | Per-project containers with static `cpu.max` = floor, no shares, no scaling, no lending (≈ a K8s quota) |
| `tenantguard` | Full v2 |

- **Accept:** all §3 scenarios × 3 modes, 3 runs each, results in `lab/results/v2-*.json`.

### V9: UI
- Panels:
  - pool bar (floors, loans, headroom);
  - per-project replicas and state;
  - per-tenant share inside each project;
  - findings, including split rounds;
  - scaling timeline.
- Reuse the v1 Playwright harness.

### V11: docs and acceptance
- Rewrite the `prototype-docs` pack from this folder.
- Fill §4 with measured numbers.

## 2. Suggested parallel waves
| Wave | Tasks in parallel | Needs |
|---|---|---|
| 1 | V1 | — |
| 2 | V2, V3, V4 (code + fakes), V5, V7 | V1 |
| 3 | V6, V4 host checks | V2, V3, V4 |
| 4 | V8, V9 | V5, V6 |
| 5 | V11 | All |

## 3. Scenarios (lab and sim)
| ID | Scenario | What it proves | Pass (tenantguard mode) |
|---|---|---|---|
| S1 | Whale in project mobility (G) | Intra-project ladder | Other projects' p99 ratio ≤ 1.2 *(est. target)*; E and K recover after throttle |
| S2 | Whale across all projects at once | Floors hold under full contention | Every project's CPU ≥ its floor (cgroup data) |
| S3 | Honest spike (K, 3×) with media/free idle | Lending | Refused honest requests lower than `industry` mode; no floor violated |
| S4 | Owner returns (media idle → 2.0 cores) | Reclaim | Media at ≥ floor within 1 s (shares) or 1 tick (caps fallback) |
| S5 | Free project sleeps, then a request arrives | Scale to zero / wake | First response ≤ cold_wait_ms; measured start time reported |
| S6 | Hidden work (black-box flood by F in free) | Attribution + split | Culprit F found in ≤ ⌈log2 3⌉ = 2 rounds |
| S7 | Retry storm | v1 retry budget | Storm capped at max(3, 10%) |
| S8 | Brain killed during S1 | Fail-static | Traffic continues; override expires at TTL; floors hold |

## 4. Measurements to fill (scorecard columns)
| Metric | Source |
|---|---|
| Victim p99: baseline / attack / after, and the attack ÷ baseline ratio | Lab |
| Time to detect / recover | Lab + events |
| Honest requests refused (429/503) | Lab |
| Floor violations (CPU below floor while demanded) | cgroup samples |
| Running containers (avg/max) and reserved memory | `docker_adapter.list_replicas` |
| Reclaim time (S4) | Events + cgroup |
| Container start p50/p95 | V4-T3 |
| Brain tick p95 | Brain metrics |

## 5. Acceptance gate (v2 replaces v1 only if all hold)
On the same host, with the same 12 tenants and S1 + S3, 3 runs each:
1. Victim p99 attack ÷ baseline ratio in tenantguard mode is lower than v1's tenantguard mode. v1's single whale run was 3.57×.
2. Honest refused requests are lower than v2 `industry` mode.
3. Average running containers are lower than v1's 11.
4. Zero floor violations in S2.
