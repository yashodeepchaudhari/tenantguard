# 02 — v2 architecture

## 1. Principles (each one removes a v1 mechanism)
| # | Principle | What it removes or replaces |
|---|---|---|
| A1 | **The kernel guarantees floors; software shapes the surplus.** All project replicas run under one pool cgroup, and each gets `cpu.weight` ∝ its project floor. Under contention every project gets ≥ its floor immediately; when there is no contention, idle CPU flows to whoever is busy. | Detection delay for cross-project harm; CPU loans and leases |
| A2 | **One container belongs to one project.** A replica only ever serves its own project's tenants. | AG(2,3), the decoder, peer-ratio health, rescue |
| A3 | **Replicas exist only when needed.** There is a warm minimum (1, or 0 if the project opts in); extra replicas start when demand rises and stop when it falls. | Always-on fleet, fixed spare and quarantine |
| A4 | **Memory is never shrunk live.** A replica's memory is fixed at creation; memory is lent only as *whole borrowed replicas*, which are stopped on reclaim. | OOM risk from shrinking (TenantShield R1) |
| A5 | **Single writer.** Only `ControlLoop.tick` changes allocations, replicas or policy. Only `DockerAdapter` talks to Docker. | Races between rescue, prover and ladder |
| A6 | **Measure, don't infer.** Per-container CPU comes from cgroups; per-tenant CPU comes from worker meters. Only the *unattributed remainder* needs an experiment (hash split). | Barcode inference, relocation proofs |
| A7 | **Fail-static.** If the brain dies, the gateway keeps the last replica map and policy, and the kernel keeps enforcing floors. Only scaling stops. | — |
| A8 | **One function per task, every function has a caller** (catalog in `04_FUNCTIONS.md`). | 3 cost estimators, 3 traffic-moving commands, 2 throttle paths |

## 2. Hierarchy and invariants
```
Pool (admin sets cpu_total, mem_total_mb, headroom_ratio)
 ├─ Project  (tier → weight · floor_cpu · cap_cpu · warm_min 0|1)
 │    ├─ Tenant (api key hash, equal share inside the project; ladder may lower it)
 │    └─ Replicas r1..rn (container: cpu.weight ∝ floor, cpu.max = per-replica cap, memory fixed)
 └─ Headroom (never promised; absorbs bursts and the cost of starting containers)
```
| ID | Invariant | Enforced by (one function) |
|---|---|---|
| I1 | A tenant belongs to exactly one project; the API key decides which | `registry.add_tenant` |
| I2 | Σ floor_cpu ≤ cpu_total × (1 − headroom); Σ warm memory ≤ mem_total × (1 − headroom) | `registry.check_capacity` |
| I3 | floor ≤ cap ≤ cpu_total for each project | `registry.check_capacity` |
| I4 | Allocation per project is between min(demand, floor) and cap | `allocator.allocate` |
| I5 | Σ replica memory ≤ mem_total × (1 − headroom) at all times | `scaler.plan` |
| I6 | A replica serves only its own project's tenants | `gateway.route` (only picks from the project's own replicas) |
| I7 | Every throttle has a finding with evidence, written before acting | `policy.apply_finding` |

## 3. Components
| Component | Plane | Purpose | State it owns | Talks to |
|---|---|---|---|---|
| **Registry** | Management | Pool, projects and tenants; validation | `registry.json` (atomic write) | Admin API, ControlLoop |
| **Admin API** (on the brain) | Management | Pool, project and tenant CRUD; operator actions | — | Registry |
| **Gateway** | Data | Identify → retry budget → admit → per-project lanes + DRR → rendezvous route → per-replica window → forward | Buckets, queues, windows, the current `Snapshot` | Replicas, brain (telemetry, wake) |
| **Replicas** (worker image) | Data | Run tenant work; meter CPU per tenant | Per-tenant SQLite on the replica's own filesystem; cache | Gateway, brain (`/stats`) |
| **ControlLoop** (brain) | Control | The single writer, tick every 1 s + wake | Allocation, replica set, ladder state | Everything below |
| **Allocator** | Control | Floors + weighted surplus (pure) | — | ControlLoop |
| **Scaler** | Control | Replicas per project + ordered actions (pure) | Hysteresis counters | ControlLoop |
| **DockerAdapter** | Control | The only Docker caller: create, start, stop, update, CPU stats, list | — | Docker Engine API (unix socket) |
| **Detector** | Control | Attribution, intra-project score, split test | Baselines, split state | ControlLoop |
| **Policy** | Control | Ladder (from v1) | Ladder per tenant | ControlLoop → Snapshot |
| **Publisher** | Control | Builds and pushes the immutable `Snapshot` | Snapshot version | Gateway |
| **Lab / UI / Narrator / Handoff** | Side | Load, dashboard, reports, operator cases | — | Gateway, brain |

## 4. Timing
| Loop | Period | Who | Purpose |
|---|---|---|---|
| Per request | µs–ms | Gateway | Admit, queue, route, forward |
| Telemetry | 250 ms | Gateway → brain | Per-tenant and per-project queue/latency/refusals |
| Worker stats | 1 s | Brain ← replicas | Per-tenant CPU (handler + bg), black-box docs |
| Container CPU | 1 s | Brain ← Docker | Exact project CPU (cgroup) |
| Control tick | 1 s | ControlLoop | Demand → allocate → scale → detect → policy → publish |
| Wake | Immediate | Gateway → brain | A project with 0 ready replicas got a request |
| Snapshot | Each tick + on change | Brain → gateway | Replica map, tenant registry, overrides, version, ttl |

## 5. CPU enforcement (one mode)
| Layer | Mechanism | Job | Speed |
|---|---|---|---|
| **Safety (floors)** | Per-replica `CpuShares` ∝ the project's floor (optionally inside a pool cgroup via `CgroupParent`) | Under contention every project gets at least its share; idle CPU flows to busy projects | Kernel scheduler, immediate |
| **Policy (surplus shape)** | Per-replica `NanoCpus` = min(1.0 core, max(alloc, floor)) (tenants are sticky, so the allocation is not split; see `09_AS_BUILT.md` D1) | A borrower gets no more than its weighted surplus; an idle project keeps a cap of at least its floor, so it is never capped below its floor when it wakes | One tick (1 s) |

- **Why `max(alloc, floor)`:** an idle project keeps its cap at its floor. When it wakes, the kernel shares decide at once; the next tick lowers the borrowers' caps.
- **Why ≤ 1.0 core per replica:** the worker is one Python process holding the GIL (`worker/app/work.py:1-2`). A project needing more CPU gets more replicas. *(est., measure with `worker/tools/calibrate.py` on the host)*
- **The Docker host is Docker Desktop on Windows** (`docs/PLAN.md` §2 rule 12c), so a pool cgroup slice may not be available. `CgroupParent` is therefore optional (`control.cgroup_parent`, empty by default). Without it, project shares compete with the other containers on the host at their default weight of 1024 (V4-T2 measures this).

## 6. Memory
- **Replica memory** = round_up_32(`mem_baseline_mb` 80 + `cache_mb` 8 + `tenant_mem_budget_mb` 32 × tenants in the project). These values come from `tenantguard.yaml`; the formula is new. Examples: 2 tenants → 160 MB, 3 tenants → 192 MB.
- **Floor memory** of a project = warm_min × replica memory. It is reserved even when the project is at zero, so the owner can always start.
- **Borrowed replica:** a replica above a project's own allowance uses free memory, or the reserve of a *scale-to-zero* project. It is marked `borrowed=true` and is the first to be drained when that memory is needed.
- **Data:** each replica keeps its own per-tenant SQLite files, exactly as each v1 worker did (`store.reset_and_seed` seeds demo rows on start). Rendezvous routing keeps a tenant on one replica, so its reads and writes stay together. A shared volume is not used: a new replica would reset the shared files.

## 7. Failure handling
| Failure | Effect | Reaction |
|---|---|---|
| Brain dies | No scaling, no new policy | Gateway keeps the last snapshot; ladder overrides expire by TTL; the kernel still holds floors. Zero-replica projects get 503 + Retry-After (paid tiers are never at zero). |
| Replica dies | Its in-flight requests fail | Gateway marks it down after 1 failed health probe *(est.)*; rendezvous re-hashes only its tenants; the scaler replaces it next tick |
| Docker API slow or down | Actions fail | `DockerAdapter.apply` returns failed actions; the scaler retries next tick; nothing else blocks |
| Gateway restarts | Buckets and queues are lost | It fetches the snapshot from the brain at startup (`GET /snapshot`) |
| Stale telemetry over 2 s | Wrong demand | ControlLoop holds the current allocation and replica count (no scaling) |
| Host-level contention (disk, memory bandwidth) | Projects slow while within their CPU | Detector raises `external_contention` to the operator, with no blame (§ `03_ALGORITHMS.md` 7.4) |

## 8. Security (unchanged from v1 unless noted)
- API keys are stored as SHA-256 hashes; the tenant → project mapping comes only from the key.
- The admin API and operator endpoint need the control token. **v2 change:** the admin API also creates projects and tenants.
- Only the brain container mounts `/var/run/docker.sock`. This was already the case in T12 for the prover. It is a privileged mount; document it as a known risk (08 R6).
