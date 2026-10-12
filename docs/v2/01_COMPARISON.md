# 01 — v1 (built) vs v2 (full rewrite): comparison

| Version | Definition |
|---|---|
| **v1** | TenantGuard as built: master `bd500eb` + branches T12, T17, T10, T25 and the layout rebalance. AG(2,3) server matrix, gateway DRR, brain ladder + stage 3 (health, rescue, decoder, prover). |
| **v2** | The rewrite in this folder: admin-set pool → projects (floor, cap, tier) → tenants. Per-project containers scaled on demand, kernel CPU sharing, rendezvous hashing, an on-demand hash split. |

## 1. State of v1 when this was written (2026-10-13 02:45)
- **Nothing is merged into master** since `bd500eb`. T10, T12, T17, T25, T26, T30 and `coord/layout-balance` are all still unmerged.
- **T12** has moved on, with the latest commit `7f72906` at 02:34:
  - `over_use` 1.6;
  - 4+ hurt neighbours;
  - warn hold 5 s;
  - `confidence_to_prove` 0.8;
  - no host drain while a hot tenant runs on the server.
- **The only measurements** are 3 untracked files in `codeAstra-T5/lab/results/`: one `whale` run per mode, attacker K, taken 2026-10-12 19:36–19:41. That is *before* the T12 commits above.

## 2. Measured v1 numbers (whale, attacker K, one run per mode)
| Mode | Victim p99 baseline (ms) | Victim p99 during attack (ms) | Attack ÷ baseline | Victim p99 after (ms) | Time to detect (s) | Time to recover (s) | Attacker 429s |
|---|---|---|---|---|---|---|---|
| off | 310.1 | 1768.5 | 5.70× | 316.3 | — | 13.0 | 0 |
| industry | 451.9 | 708.3 | 1.57× | 322.6 | — | 15.0 | 71 |
| tenantguard | 345.7 | 1233.4 | 3.57× | 262.1 | 25.3 | 4.0 | 61 |

Source: `codeAstra-T5/lab/results/20261012-19{3643,3839,4007}-whale-*.json`. The ratios are computed from those files.

**What these numbers say (with n = 1, treat them as signals, not results):**
1. **Industry mode protected victims better during the attack than TenantGuard mode:** 1.57× vs 3.57×. TenantGuard needed 25.3 s to detect, and victims suffer until then. That is built into v1: harm across tenants is bounded **only after detection**, because tenants share servers.
2. **TenantGuard recovered fastest after detection:** 4.0 s vs 13–15 s.
3. **Baseline victim p99 is already 310–452 ms**, above every SLA in the rebalance layout (40–150 ms). The fleet looks overloaded *before* the attack, so these numbers mostly measure saturation.

**Consequence for the design:**
- v2 removes the detection delay for **cross-project** harm. Each project's CPU floor is held by the kernel scheduler from t = 0 (§3), so no detection is needed for that case.
- Detection still matters **inside** a project. There v2 uses the same score and ladder as v1, limited to the project's tenants.

## 3. Side-by-side
| Area | v1 | v2 | Winner and why |
|---|---|---|---|
| **Placement** | Fixed AG(2,3): 12 tenants × 3 servers, 9 always-on workers + spare + quarantine | Each project runs on its own replicas, from 0 or 1 up to N, started and stopped on demand | v2: placement follows demand; no fixed 12×9 limit |
| **Cross-project isolation** | Statistical: a neighbour shares ≤ 1 of 3 servers; harm is found after the fact | Kernel: per-project `cpu.weight` ∝ floor inside a pool cgroup, plus a per-replica memory limit | **v2**: instant, no detection needed |
| **Idle capacity** | Wasted: tier limits are hard caps and the brain can only lower them | Work-conserving: the kernel gives idle CPU to busy projects; the allocator shapes the surplus by tier weight; memory is lent as whole replicas | **v2** |
| **Reclaim when an owner returns** | Not applicable (nothing is lent) | CPU: kernel scheduler, immediate. Borrowed replicas: drained and stopped in ≤ 2 s *(est.)* | **v2** |
| **Containers running when idle** | 11 always | 1 warm per project (5 in the example), 0 for opt-in free projects | **v2** |
| **Memory reserved** | 9 × 256 + 256 + 128 = 2688 MB (compose limits) | Example: 864 MB warm (05 §3; computed from config, not measured) | **v2** *(computed, not measured)* |
| **Finding the culprit across projects** | Barcode decoder + relocation prover, 25.3 s to detect in the one whale run | Not needed: per-container CPU is exact | **v2** |
| **Finding the culprit inside a project** | Score + ladder | Same score + ladder, scoped to the project, plus an on-demand hash split for unattributed CPU | Equal; v2 adds the split |
| **Request cost estimate** | 3 places: gateway EWMA, billing EWMA, `calibration.json` (unused) | 1 function: `costs.reference_cost` (seeded from calibration) | **v2** |
| **Traffic movement** | 3 commands: RouteWeights, Relocate, Drain | 1 function: `route()` over the replica map | **v2** |
| **Cold start** | None | Only for scale-to-zero projects: queue up to 5 s *(est.)*; paid tiers stay warm | v1 |
| **Single-replica fragility** | A tenant spans 3 servers | ≥ 2 replicas while busy; 1 when calm | v1 slightly |
| **Code size (brain stage 3)** | `stage3.py` 323 + `decoder.py` 159 + `health.py` 133 + `rescue.py` 96 + `prover.py` 46 = 757 lines on master (more on T12) | `allocator` + `scaler` + `split` ≈ 250 lines *(estimate)* | **v2** |
| **Novelty (PS3 pitch)** | Barcode decoding | Floors held by the kernel + work-conserving surplus + replicas on demand + on-demand hash group testing + proof before punishment | Different; v2 is easier to defend with numbers |
| **Delivery risk** | Built, in acceptance | New build | v1 |

## 4. What v2 keeps from v1 (reused, not rewritten)
| v1 file | Reused as |
|---|---|
| `gateway/tenants.py` (key hash, class) | Unchanged; the class gains a size bucket |
| `gateway/retry.py` | Unchanged |
| `gateway/buckets.py` | Token bucket kept; cost bucket removed (the kernel and DRR do this job) |
| `gateway/scheduler.py` (DRR) | Unchanged class; one instance per project |
| `gateway/limiter.py` (Gradient2) | Unchanged; one window per replica |
| `gateway/telemetry.py`, `metrics.py`, `security.py` | Adapted to project fields |
| `worker/app/*` | Unchanged, plus one counter (black-box docs per tenant) |
| `brain/ladder.py`, `score.py` | Unchanged formula; the neighbour set becomes the same-project tenants |
| `brain/incidents.py`, operator endpoint | Unchanged |
| T12 `DockerControl` (Engine API over the unix socket, NanoCpus) | Extended with create/start/stop/update(CpuShares, Memory) |
| `integrations/handoff`, `narrator`, `billing` (T17) | Unchanged logic; the replica list comes from the snapshot |

## 5. What v2 deletes
- `layouts/ag23.json`
- `brain/decoder.py`, `health.py`, `rescue.py`, `stage3.py`, `prover.py` (incl. T12 Docker/Host provers), `verdicts.py` (T12), and the matrix part of `sim.py`
- `gateway/router.py` (replaced by rendezvous), plus the route-weight, relocate and drain parts of `overrides.py`
- The fixed `spare` and `quarantine` services

**Reason:** each of these exists only to *infer* what cgroups per project *measure* directly. Under the "no unused code" rule they have no caller left.

## 6. Recommendation
- **Ship v1 for the submission** (merge, run the scorecard).
- **Build v2 as a separate line of work** from `02_ARCHITECTURE.md` and `07_BUILD_PLAN.md`.
- **Acceptance gate:** v2 replaces v1 only if, on the same host and scenarios, it shows all of the following (07 §5):
  1. lower victim p99 ratio than v1 in TenantGuard mode;
  2. fewer refused honest requests;
  3. fewer running containers.
