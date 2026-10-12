# 09 — v2 as built: deviations from the spec, status, what is left

Branch `v2/main`. Each change below was found while building against the simulator (`brain/tenantguard_brain/sim.py`, driven through the real `Runtime` by `brain/tests/conftest.py`). Values marked *estimate* are unmeasured.

## 1. Deviations from 02/03 (and why)
| # | Spec said | Built | Why |
|---|---|---|---|
| D1 | Per-replica `NanoCpus = max(alloc, floor) / n` | `min(1 core, max(alloc, floor))` per replica | Tenants stick to a replica (rendezvous). An even split halved the CPU of the replica the busy tenants were on, while another replica sat idle (spike scenario: victims at 12× their baseline p95). A usage-weighted split was tried and oscillated. Floors stay protected by `CpuShares ∝ floor / n`. **Cost:** with several replicas the allocation is a per-replica ceiling, not an exact project total. |
| D2 | `cpu_enforcement: shares \| caps` | One mode; `cgroup_parent` optional | The Docker host is Docker Desktop on Windows (`docs/PLAN.md` rule 12c), where a pool slice may not exist. Caps = `max(alloc, floor)` + shares hold floors either way. |
| D3 | Demand = used + queued + refused | Plus a saturation probe: at ≥ 90% of its caps, demand ≥ 1.25 × used (*estimates*) | A project held at its cap can only show what it gets, so lending never started. |
| D4 | ≥ 2 replicas while busy | Only if each would still get ≥ `busy_cpu` (0.5 core) | Splitting a 0.75-core project into two 0.375-core replicas made the sticky tenant slower. |
| D5 | Score queue share = (depth + in flight) / slots | A tenant that is itself hurt counts only its queued requests | Whale scenario: the victims' in-flight count rose *because* they were slowed, so victims were flagged and one was throttled. |
| D6 | Overhead split by request share | Split up to a ceiling of 6.6 ms per request (max of process − handler − bg − black-box CPU in `worker/calibration.json`); the rest is unattributed | Without a ceiling, hidden work would be smeared over every tenant and never be found. |
| D7 | Unattributed > 25% of container CPU | Also > 50 ms/s (*estimate*) | An idle replica's own runtime (about 5 ms/s) is "100% unexplained". |
| D8 | Project volume shared by replicas | Each replica keeps its own per-tenant SQLite (as v1 workers did) | The worker seeds and resets its data at start, so a shared volume would be wiped by every new replica. |
| D9 | Memory reserved = `warm_min` replicas | `own_replicas = max(warm_min, ceil(floor / (1 core × 0.8)))` | A project at its floor must always be able to run its own replicas; only replicas above that are borrowed. |
| D10 | Separate `publisher`, `collect`, `policy`, `docker_adapter` modules | `ControlLoop.snapshot` + `Runtime.push_snapshot`; `Brain.replica_usage` / `demand_signals`; the ladder and limits in `Brain`; `backends.py` | Fewer modules, each with one owner (catalog in `04_FUNCTIONS.md`). |
| D11 | Heartbeat for fail-static | The snapshot's arrival time | One message instead of two. |
| D12 | `off` mode = one shared project | `off` = unlimited replicas with equal shares, a static fleet, gateway FIFO; `industry` = static floor split evenly, no lending | Same tenants and projects in all three modes, so the scorecard compares like with like. |

## 2. Simulated scenarios (simulator, not measurements)
| Scenario | Outcome in the simulator | Test |
|---|---|---|
| Calm, 60 s | No incident; 1 warm replica per project | `test_core::test_quiet_pool...` |
| Whale K in mobility | Only K: warn → throttle 0.5 → 0.25, then recovered and closed. Other projects' caps never changed | `test_core::test_whale...`, `test_other_projects_never_feel_the_whale` |
| Honest burst, all of mobility ×2, others idle | Allocation above the floor (loan), 2 replicas, no incident | `test_control::test_honest_project_spike...` |
| Idle project with `warm_min` 0 | Asleep after 300 ticks; a wake starts a replica on the next tick | `test_control::test_a_project_allowed_to_sleep...` |
| Hidden CPU from F (black-box-like) | 2 split rounds (⌈log₂ 3⌉), verdict F, then the ladder | `test_control::test_hidden_work...` |
| Slow host (+30 ms everywhere) | One no-blame contention finding about 5 s in; nobody throttled | `test_control::test_a_slow_host...` |

These numbers come from the simulator's latency model (`sim.py`), not from the host. They show the logic, not the performance.

## 3. Task status (07_BUILD_PLAN)
| Task | Status |
|---|---|
| V1 contracts + config | Done |
| V2 registry + admin API | Done (`registry.py`, `registry_store.py`, `main.admin_router`) |
| V3 allocator + scaler | Done (golden and seeded-random property tests) |
| V4 backends | Code + fake Engine API tests done. **Host checks T1–T3 not run** (no host traffic from this branch): pool slice, shares 3:1, container start p50/p95 |
| V5 gateway | Done |
| V6 control + detector + ladder + runtime | Done |
| V7 worker changes | Done |
| V8 lab + scorecard | Compose, lab calibrate, handoff and narrator adapted. **The scorecard has not been run on the host** |
| V9 UI | **Not done.** The UI still expects barcodes / servers (`useBarcodes`, `FlowView`, `ServerGrid`, the mock simulator). `TenantAdminView` now has `project` and `replicas`; `ServerView` entries are replicas with a `project` |
| V10 billing | **Not done.** Billing lives on the unmerged `task/T17-billing`; it must read per-tenant CPU from attribution and the replica list from the brain |
| V11 docs | `docs/v2` updated (this file, `04_FUNCTIONS.md`). The `prototype-docs` pack still describes v1 |

## 4. Test counts on this branch
| Suite | Result |
|---|---|
| gateway | 91 passed |
| brain | 87 passed |
| worker | 55 passed, 1 skipped |
| handoff | 47 passed |
| lab | 33 passed |
| tools | 16 passed |
| narrator | 9 passed |

Run each suite from its own directory with `PYTHONPATH=<repo>/common`. The shared editable install points at the main checkout.

## 5. Before v2 can replace v1 (07 §5 gate)
1. Run the V4 host checks: does a pool slice exist, do CpuShares split 3:1, and what are the container start p50/p95. Then replace the estimates in `tenantguard.yaml`.
2. Build the worker image and run the stack on the host (`docker compose --profile image build worker && docker compose up -d`).
3. Run S1–S8 × off / industry / tenantguard, 3 runs each, into `lab/results/v2-*.json`. Compare against v1's tenantguard whale ratio (3.57×, a single run).
4. Do V9 (UI) and V10 (billing).
