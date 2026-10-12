# 03 — v2 algorithms, step by step

Every block below is one function from `04_FUNCTIONS.md`. Parameters are in `05_DATA_AND_CONFIG.md` §2. Values marked *(est.)* are estimates.

## 1. Admin setup
1. `create_pool(cpu_total, mem_total_mb, headroom_ratio)`: the admin enters the real capacity. There are no credits.
2. `create_project(name, tier, floor_cpu, cap_cpu, warm_min)` → `check_capacity`. It is rejected if:
   - Σ floor_cpu > cpu_total × (1 − headroom), or
   - Σ warm memory > mem_total × (1 − headroom), or
   - floor > cap, or cap > cpu_total.
3. `add_tenant(project_id, name)` → returns a new random 256-bit key once; only its SHA-256 is stored.
4. ControlLoop wakes, `scaler.plan` starts `warm_min` replicas, and Publisher pushes the snapshot.

**Tier** sets the project weight: enterprise 4, pro 2, free 1 (the v1 values). Inside a project, tenants share equally.

## 2. Demand estimate (`allocator.estimate_demand`)
```
raw_p   = used_cpu_p                       # cores, from cgroup CPU of all replicas (exact)
        + queued_cost_ms_p / 1000 / tick_s # work waiting in gateway queues
        + refused_cost_ms_p / 1000 / tick_s# work refused by admission (rate/concurrency)
demand_p = clip(α·raw_p + (1−α)·demand_prev_p, 0, cap_p)        α = 0.5
```
Without the queue and refused terms, a project held at its cap would never show more demand than its cap allows, and would never get surplus.

## 3. Allocation (`allocator.allocate` + `allocator.water_fill`)
```
usable = cpu_total × (1 − headroom)
base_p = min(demand_p, floor_p)
surplus = usable − Σ base_p
want_p = max(0, min(demand_p, cap_p) − base_p)
loan   = water_fill(surplus, want, weight=tier_weight)
alloc_p = base_p + loan_p
```
```python
def water_fill(surplus, wants, weights):
    out = {p: 0.0 for p in wants}
    active = {p for p, w in wants.items() if w > 0}
    while surplus > 1e-9 and active:
        tw = sum(weights[p] for p in active)
        share = {p: surplus * weights[p] / tw for p in active}
        capped = {p for p in active if wants[p] - out[p] <= share[p]}
        if not capped:
            for p in active: out[p] += share[p]
            return out
        for p in capped:
            surplus -= wants[p] - out[p]; out[p] = wants[p]
        active -= capped
    return out
```
- Complexity is O(P²) with P ≤ tens of projects.
- **Example data:** pool 5.75 CPU (same total as today's compose), headroom 10% → usable 5.175. Projects as in 05 §3.

### 3.1 Worked example: two projects idle
| Project (tier, floor, cap) | demand | base | want | weight | loan | **alloc** |
|---|---|---|---|---|---|---|
| health (ent, 1.25, 3.0) | 3.0 | 1.25 | 1.75 | 4 | 0.69 | **1.94** |
| commerce (ent, 1.25, 3.0) | 3.0 | 1.25 | 1.75 | 4 | 0.69 | **1.94** |
| mobility (pro, 0.75, 2.0) | 2.0 | 0.75 | 1.25 | 2 | 0.345 | **1.095** |
| media (pro, 0.75, 2.0) | 0.1 | 0.1 | 0 | 2 | 0 | **0.1** |
| free (free, 0.5, 1.0) | 0.1 | 0.1 | 0 | 1 | 0 | **0.1** |

The surplus is 5.175 − 3.45 = 1.725, and Σ alloc = 5.175.

### 3.2 Worked example: media wakes and asks for 2.0
| Project | base | want | weight | loan | **alloc** |
|---|---|---|---|---|---|
| health | 1.25 | 1.75 | 4 | 0.358 | **1.608** |
| commerce | 1.25 | 1.75 | 4 | 0.358 | **1.608** |
| mobility | 0.75 | 1.25 | 2 | 0.179 | **0.929** |
| media | 0.75 | 1.25 | 2 | 0.179 | **0.929** |
| free | 0.1 | 0 | 1 | 0 | **0.1** |

The surplus is 5.175 − 4.10 = 1.075.

- **Between the wake and the next tick,** the kernel already gives media ≥ its floor share, because the weights are proportional to floors (§4).
- **The tick then reshapes the caps.** No borrower holds anything media must wait for.

## 4. Kernel share math (why floors hold without a control loop)
- Each project's replicas get `cpu.weight` that sums to `floor_p ÷ Σ floors` of the pool cgroup.
- When every project is busy, project p gets `floor_p / Σ floors × cpu_total`. That is ≥ floor_p, because Σ floors ≤ cpu_total.
- **Example:** health gets 1.25 / 4.5 × 5.75 = 1.597 cores, which is ≥ 1.25.
- When some projects are idle, their weight drops out and busy projects share the rest. The per-replica `cpu.max` caps from §3 then decide *how* the surplus is split.

**Docker mapping:**
- `CpuShares` per replica = clamp(round(10240 × floor_p ÷ replicas_p), 2, 262144): 10240 shares per core of floor, inside the Docker range. Example: health 1.25 ÷ 3 replicas → 4267; free 0.5 ÷ 1 → 5120.
- `NanoCpus` = min(1.0, alloc_p ÷ replicas_p) × 1e9.
- `CgroupParent` = `tg-pool.slice`, created once on the host with `cpu.max` = cpu_total.

## 5. Scaling (`scaler.replicas_needed`, `scaler.plan`)
### 5.1 How many replicas
```
per_replica_max = 1.0 core                          # GIL bound (est., measure)
need  = ceil(demand_p / (per_replica_max × target_util))      target_util = 0.8 (est.)
low   = warm_min_p if not idle_long else (0 if allow_zero_p else warm_min_p)
want  = clamp(need, low, max_replicas_p)            max_replicas_p = ceil(cap_p / per_replica_max)
if busy_p: want = max(want, 2)                      # no single point of failure while busy
```
**Hysteresis** *(estimates)*:
- Scale **up** at once when `want > current` for 2 ticks.
- Scale **down** by 1 when `want < current` for 30 ticks.
- Go to **zero** after 300 ticks fully idle, and only when `allow_zero`.
- `busy_p` means demand > 0.5 × per_replica_max *(est.)*.

### 5.2 Ordered action plan
`plan()` returns actions in a safe order, so memory and CPU limits are never exceeded mid-change:
1. **Drain** the replicas to remove. Prefer borrowed ones, then the youngest. The gateway stops routing to them at once (via the snapshot).
2. **Stop** drained replicas once they have no in-flight work, or after `drain_timeout_s` = 2 *(est.)*.
3. **Lower** `NanoCpus` for projects whose allocation fell.
4. **Start** new replicas, but only if Σ memory after the start is ≤ the usable memory (I5). Otherwise mark the request `deferred`.
   - If the start is for a waking scale-to-zero owner, also drain one borrowed replica that uses its reserve.
5. **Raise** `NanoCpus` and update `CpuShares` for all changed projects.

### 5.3 Example replica counts for §3.1
| Project | alloc | Replicas = ceil(alloc ÷ (1.0 × 0.8)) | Memory |
|---|---|---|---|
| health | 1.94 | 3 | 3 × 160 = 480 |
| commerce | 1.94 | 3 | 480 |
| mobility | 1.095 | 2 | 2 × 192 = 384 |
| media | 0.1 | 1 (warm) | 160 |
| free | 0.1 | 1 (warm) | 192 |

Total 1696 MB ≤ usable 2419 MB (from 2688 × 0.9). This is computed from the formulas, not measured.

## 6. Gateway per request
1. `identify(key)` → (tenant, project) from the snapshot key index. Unknown key → 401.
2. `classify(request)`: as v1 (`tenants.py:26-29`), **plus** `log2` buckets of numeric size parameters (`n`, `limit`, `pages`), so a big search is a different class from a small one.
3. `retry_budget.check`: v1, unchanged.
4. `admit(tenant)`:
   - block override?
   - token bucket (tier rate × ladder factor)?
   - concurrency (tier concurrency × ladder factor)?
   - Refusal → 429 listing every dimension that refused (v1 headers).
   - **There is no cost bucket:** a project that over-spends only fills *its own* queue and replicas.
5. `enqueue(project, lane, tenant, item)`, where `item.cost = costs.reference_cost(tenant, class)`. The project's queue set has lanes (express < 5 ms, interactive, batch) and DRR among tenants with weight `1 × ladder weight_factor`.
6. `pump(project)`: strict lane priority. Pick a tenant by DRR. Then `route(tenant, project)`:
   ```
   ready = [r for r in snapshot.replicas[project] if r.state == READY and window_free(r, lane)]
   pin   = snapshot.split_pins.get(tenant)          # only during a split test (§7.3)
   if pin and pin in ready: return pin
   return max(ready, key=lambda r: hash64(tenant, r.id))   # rendezvous; tenant sticks to a replica
   ```
   - If the sticky replica has no free window, take the next replica in rendezvous order (bounded spill).
   - If nothing is ready, keep the item queued.
7. `forward` → worker, then `observe(response)`:
   - read `X-TenantGuard-Cpu-Ms`;
   - update `costs` (EWMA α 0.2);
   - update the replica's Gradient2 window (v1 `limiter.py`).
8. **Expiry:** an item waits up to 1000 ms (interactive) or 5000 ms (batch), then 503. For a project with 0 ready replicas, the wait is `cold_wait_ms` = 5000 *(est.)*, and the **first** such request calls `wake(project)` (debounced to once per 2 s).

**Why rendezvous hashing:**
- When replicas go from n to n+1, only about 1/(n+1) of the tenants move, so per-tenant cache warmth survives scaling.
- There is no ring to maintain.
- A dead replica moves only its own tenants.

## 7. Detection inside a project
### 7.1 Attribution (`detector.attribute`)
```
container_ms_p   = Σ cgroup CPU of p's replicas over the window        (exact)
metered_ms_t     = handler_ms_t + bg_ms_t                              (worker meters)
overhead_ms_p    = container_ms_p − Σ metered_ms_t − blackbox_ms_p
tenant_ms_t      = metered_ms_t + overhead_ms_p × req_t / Σ req       (framework overhead by request share)
                 + blackbox_ms_p × docs_t / Σ docs                      (new worker counter: docs handed per tenant)
unattributed_p   = max(0, container_ms_p − Σ tenant_ms_t)             (should be ≈ 0; large ⇒ hidden work)
```
Splitting framework overhead by request count is justified by the calibration data: process CPU is above handler CPU for every endpoint (light 0.95 vs 0.40 ms).

### 7.2 Score and ladder (v1 formula, project scope)
```
cpu_share_t   = tenant_ms_t / container_ms_p
fair_share_t  = weight_t / Σ weights of active tenants in p          (equal → 1/k)
impact_t      = mean over other active tenants u in p of clip(p95_u / baseline_u − 1, 0, 3)
score_t       = max(cpu_share_t, queue_share_t) / fair_share_t × (1 + impact_t)
```
- `ladder.step(t, score_t)` runs **v1 unchanged**: thresholds 1.5 / 2.0 / 4.0, holds, AIMD recovery, TTLs.
- **The effect is scoped:** a throttled tenant only loses share *inside its own project*. Other projects never feel it either way.

### 7.3 Hash split (`detector.split_step`), only when needed
**Trigger** (all must hold):
- `unattributed_p` > 25% of `container_ms_p` for 10 s *(est.)*;
- a tenant in p is hurt (impact ≥ 0.2, v1 `min_victim_impact`);
- p has ≥ 2 active tenants;
- no split is running anywhere (budget: 1 at a time).

**Steps:**
```
round r = 0; suspects S = active tenants of p
loop while |S| > 1 and r < ceil(log2 k):
    ensure p has 2 replicas (scaler gets a temporary +1, borrowed=true, if memory allows; else abort as "no capacity")
    pins: t → R0 if bit(hash64(t, salt=r)) == 0 else R1      for t in S
    settle 2 s, sample 6 s                                     (T12 prover values)
    heavier = replica with larger unattributed_ms
    S = suspects pinned to heavier;  r += 1
finding(t*, kind="unattributed_work", evidence = per-round unattributed_ms of R0/R1)
clear pins; temporary replica drained
```
- **Duration:** about 8 s per round *(est.)*. For k = 8, that is 3 rounds ≈ 24 s.
- **What happens next:** the finding goes to `policy.apply_finding` (ladder evidence). It does *not* skip ladder steps.

### 7.4 Contention outside the project (no blame)
**Trigger:** project p95 > 1.6 × baseline for 4 s (v1 `slow_peer_ratio`, `open_after_ticks` reused), while **every** project's container CPU ≤ its allocation.

**Result:** a `Finding(kind="external_contention", project=*)` goes to the operator and the narrator. No tenant is penalised.

## 8. Billing (T17 logic, new inputs)
- Same calm-reference pricing and SLA credit table.
- **Inputs:** `tenant_ms_t` from §7.1 instead of per-server polling, and the tier price of the tenant's project.
- **New line:** `borrowed_cpu_s` = CPU used above the project's floor while a project floor was lent. It is priced at the tier price (no surcharge by default; it can be configured).
