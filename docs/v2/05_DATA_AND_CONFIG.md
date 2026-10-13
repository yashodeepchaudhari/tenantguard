# 05 — v2 data models, config, APIs, tick order

## 1. Contracts (`common/tg_common/contracts.py`, Pydantic)
```python
class Tier(str, Enum): enterprise = "enterprise"; pro = "pro"; free = "free"

class Pool(BaseModel):
    cpu_total: float          # cores the admin gives TenantGuard
    mem_total_mb: int
    headroom_ratio: float = 0.10

class ProjectSpec(BaseModel):
    id: str                   # ^[a-z0-9-]{1,32}$
    name: str
    tier: Tier
    floor_cpu: float          # guaranteed cores
    cap_cpu: float            # max cores
    warm_min: int = 1         # 0 = may scale to zero
    sla_p95_ms: int = 100     # v1 default (contracts.py:52)

class Tenant(BaseModel):
    id: str
    project_id: str
    name: str
    key_sha256: str

class Registry(BaseModel):
    pool: Pool
    projects: dict[str, ProjectSpec]
    tenants: dict[str, Tenant]
    version: int

class Alloc(BaseModel):
    base: float
    loan: float
    cap: float                # alloc = base + loan

class ReplicaState(str, Enum): starting = "starting"; ready = "ready"; draining = "draining"

class Replica(BaseModel):
    id: str                   # tg-<project>-<n>
    project_id: str
    url: str                  # http://tg-<project>-<n>:8100
    state: ReplicaState
    borrowed: bool
    nano_cpus: int
    cpu_shares: int
    mem_mb: int
    started_at: float

class Action(BaseModel):
    kind: Literal["drain", "stop", "update", "start"]
    replica_id: str
    project_id: str
    nano_cpus: int | None = None
    cpu_shares: int | None = None
    mem_mb: int | None = None
    borrowed: bool = False

class LimitsCmd(BaseModel):   # v1, unchanged
    tenant: str
    factor: float             # (0, 1]
    weight_factor: float      # (0, 1]
    ttl_s: float              # ≤ 3600

class Snapshot(BaseModel):
    version: int
    issued_at: float
    keys: dict[str, tuple[str, str]]        # key_sha256 → (tenant, project)
    tiers: dict[str, Tier]                   # project → tier (rate, concurrency)
    replicas: dict[str, list[Replica]]       # project → replicas
    overrides: list[LimitsCmd]
    blocks: dict[str, float]                 # tenant → until
    split_pins: dict[str, str]               # tenant → replica id (only during a split test)

class Finding(BaseModel):
    ts: float
    kind: Literal["noisy", "unattributed_work", "external_contention"]
    project_id: str
    tenant: str | None
    score: float | None
    evidence: dict

class ProjectTelemetry(BaseModel):          # added to the v1 GatewayTelemetry
    project_id: str
    queued_cost_ms: float
    refused_cost_ms: float
    ready_replicas: int
    p95_ms: float
```

## 2. Config (`tenantguard.yaml`, v2 section). Values marked *est.* must be measured.
```yaml
pool:                     # admin may also set this via PUT /admin/pool
  cpu_total: 5.75         # = today's compose total (9×0.5 + 1.0 + 0.25)
  mem_total_mb: 2688      # = today's compose total (9×256 + 256 + 128)
  headroom_ratio: 0.10    # est. (TenantShield default)

tiers:                    # v1 values (tenantguard.yaml:26-35)
  enterprise: {weight: 4, rate_rps: 100, burst: 200, concurrency: 16, price_rs_per_cpu_s: 0.40}
  pro:        {weight: 2, rate_rps: 50,  burst: 100, concurrency: 8,  price_rs_per_cpu_s: 0.60}
  free:       {weight: 1, rate_rps: 20,  burst: 40,  concurrency: 4,  price_rs_per_cpu_s: 0.80}

control:
  tick_s: 1.0
  demand_alpha: 0.5               # est.
  cgroup_parent: ""               # optional pool cgroup (02 §5); empty = none
  per_replica_max_cpu: 1.0        # est. GIL bound; measure
  target_util: 0.8                # est.
  busy_min_replicas: 2
  scale_up_ticks: 2               # est.
  scale_down_ticks: 30            # est.
  zero_after_ticks: 300           # est.
  drain_timeout_s: 2              # est.
  replica_mem: {baseline_mb: 80, cache_mb: 8, per_tenant_mb: 32, round_mb: 32}
  telemetry_stale_s: 2            # v1 fail_static_after_s

gateway:                  # v1 unless noted
  max_queue_wait_ms: {interactive: 1000, batch: 5000}
  cold_wait_ms: 5000              # est. new
  wake_debounce_s: 2              # new
  express_cost_ms: 5
  batch_routes: [/api/export]
  size_params: [n, limit, pages]  # new: class size bucket
  retry_budget: {ratio: 0.1, window_s: 10, min_allowed: 3}
  limiter: {min_window: 2, max_window: 64, initial_window: 8}
  cost_seed: worker/calibration.json  # new

detector:
  noisy: {}                       # v1 brain.noisy block, unchanged
  unattributed_ratio: 0.25        # est.
  unattributed_hold_s: 10         # est.
  split: {settle_s: 2, sample_s: 6, max_parallel: 1}   # settle/sample = T12 prover values
  contention: {ratio: 1.6, open_ticks: 4}             # v1 health values

worker: {}                        # v1 block unchanged (cache_mb, tenant_mem_budget_mb, ...)
```

## 3. Example registry (same 12 tenants, grouped into 5 projects)
| Project | Tier | floor_cpu | cap_cpu | warm_min | Tenants | Replica memory |
|---|---|---|---|---|---|---|
| health | enterprise | 1.25 | 3.0 | 1 | A, D | 160 MB |
| commerce | enterprise | 1.25 | 3.0 | 1 | J, B | 160 MB |
| mobility | pro | 0.75 | 2.0 | 1 | G, K, E | 192 MB |
| media | pro | 0.75 | 2.0 | 1 | H, L | 160 MB |
| free | free | 0.50 | 1.0 | 1 (0 allowed) | C, F, I | 192 MB |

**Checks:**
- Σ floor = 4.5 ≤ 5.175 usable.
- Warm memory = 160 + 160 + 192 + 160 + 192 = **864 MB**, against 2688 MB of v1 compose limits. Both are computed from limits; actual RSS is not measured.

## 4. Tick order (`ControlLoop.tick`)
```
 1. views   = collect.views(now);  if views.stale: publish(hold) and return
 2. stats   = collect.poll_workers(replicas);  cpu = docker_adapter.cpu_ms(replicas)
 3. for p: demand[p] = allocator.estimate_demand(demand[p], sample(p), α, cap)
 4. allocs  = allocator.allocate(pool, projects, demand)
 5. actions = scaler.plan(state, allocs, demand, cfg)
 6. results = docker_adapter.apply(actions);  state.replicas = merge(results)
 7. for p: att[p] = detector.attribute(view(p))
 8. for t: s = detector.score(t, p, baselines); Ladder.step(s, now); update_baseline(...)
 9. split   = detector.split_step(split, observation)   # no-op unless triggered
10. f = detector.external_contention(views)              # may be None
11. for each finding: policy.apply_finding(f, ladders, log)
12. snap = publisher.build(registry, state.replicas, policy.overrides(...), split.pins, v+1)
13. publisher.push(snap)
```

## 5. HTTP APIs
| Service | Method + path | Purpose | Auth |
|---|---|---|---|
| brain | `PUT /admin/pool` | Set capacity | control token |
| brain | `POST /admin/projects`, `PATCH /admin/projects/{id}`, `DELETE /admin/projects/{id}` | Project CRUD | control token |
| brain | `POST /admin/projects/{id}/tenants` → `{tenant_id, api_key}` (key shown once) | Add a tenant | control token |
| brain | `DELETE /admin/tenants/{id}` | Remove a tenant | control token |
| brain | `GET /snapshot` | Gateway startup / recovery | control token |
| brain | `POST /control/wake {project}` | Wake | control token |
| brain | `POST /api/operator` | v1 release / extend / resume | control token |
| brain | `GET /api/state` | UI: allocations, replicas, ladders, findings | read |
| gateway | `POST /control/snapshot` | Receive a snapshot | control token |
| gateway | `ANY /api/*` | Tenant traffic (`X-API-Key`) | tenant key |
| worker | v1 endpoints + `/stats` (adds `docs_by_tenant`) | Work | gateway header |

## 6. Stored data
| Store | Content | Lifetime |
|---|---|---|
| `registry.json` (brain volume) | Pool, projects, tenants (key hashes) | Permanent |
| `events.jsonl` (brain volume) | Findings, ladder transitions, scaling actions, operator actions | Permanent (audit) |
| Replica filesystem | Per-tenant SQLite (v1 `store.py`, seeded on start) | Life of the replica (as v1 workers) |
| Memory (brain) | Demand, allocs, replicas, ladders, baselines, 60 s history | Volatile; replicas are rebuilt from Docker labels on start |
| Memory (gateway) | Snapshot, buckets, queues, windows, cost EWMAs | Volatile; the snapshot is refetched on start |
