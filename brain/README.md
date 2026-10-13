# TenantGuard brain (v2)

The control plane runs on **:8200**. It owns three things:

| What it owns | Module |
|---|---|
| **Who exists:** the admin-set pool → projects (floor, cap, tier, warm_min) → tenants | `registry.py` |
| **Where they run:** allocation, replica counts and CPU settings; replicas started and stopped through a backend | `control.py` |
| **How they behave:** per-tenant scores inside each project, the throttle ladder, incidents | `core.py` |

It publishes everything the gateway needs as one `Snapshot`.

The design is in `docs/v2/`. Values marked *estimate* in `tenantguard.yaml` must be measured on the Docker host.

## Run
```
pip install -r brain/requirements.txt          # tenantguard_common is already installed
cd brain
python -m tenantguard_brain                     # binds 0.0.0.0:8200
python tools/local_stack.py                     # gateway + brain locally; replicas as local processes (no Docker)
python -m pytest
```

| Env | Default | Meaning |
|---|---|---|
| `TENANTGUARD_CONTROL_TOKEN` | `dev` | Required on `/admin/*`, `/control/*`, `/snapshot`, `POST /api/operator`; sent to the gateway |
| `TENANTGUARD_GATEWAY_URL` | `services.gateway` | Where the snapshot goes and the telemetry comes from |
| `TENANTGUARD_BACKEND` | `control.backend` (`docker`) | `docker` = Engine API over `/var/run/docker.sock`; `process` = local worker processes |
| `COMPOSE_PROJECT` | `tenantguard` | Stack label `tg.stack`: the only containers the brain lists, starts, updates or removes |
| `TENANTGUARD_WORKER_IMAGE` / `TENANTGUARD_DOCKER_NETWORK` | yaml | Image and network of new replicas |
| `TENANTGUARD_REGISTRY` | `control.registry_path` | Registry file (the yaml seeds it on first start) |
| `TENANTGUARD_PROCESS_BASE_PORT` | `control.process_base_port` | First port for process replicas |

## Every control tick (1 s, or at once on a wake or a registry change)
1. **Readiness:** each `starting` replica's `/healthz` is checked; a `ready` replica joins routing. A replica that is not healthy within `start_timeout_s` is stopped.
2. **Attribution** (`detector.attribute`, the one source of per-tenant CPU). For each replica, the container's cgroup CPU is split into:
   - each tenant's metered CPU (handler + bg);
   - black-box CPU, split by documents handed over;
   - framework overhead, split by requests up to `overhead_ms_per_request`.
   
   What remains is **unattributed**. A split-test culprit is charged with it.
3. **Demand** per project: used cores + queued work + refused work (EWMA).
   - A project at ≥ 90% of its caps cannot show more than it gets, so its demand is probed ×1.25 and grows into idle capacity.
4. **Allocation** (`allocator.allocate`): floors first, then the surplus water-filled by tier weight.
   - `industry` mode: static floor. `off` mode: cap.
5. **Targets** (`scaler`):
   - Replicas = `ceil(min(demand, alloc) / (1 core × 0.8))`, at least `warm_min`. At least 2 while busy, if each would still get ≥ 0.5 cores. Hysteresis: 2 ticks up, 30 down, 300 to sleep (`warm_min` 0 only).
   - Each replica: `NanoCpus = min(1 core, max(alloc, floor))`, `CpuShares ∝ floor`.
   - Memory is fixed per replica and never shrunk.
6. **Plan:** drain → stop → lower CPU → start → raise CPU, never above usable memory. An owner below its own allowance reclaims a borrowed replica first.
7. **Backend:** the actions are applied. The backend never raises; failures are retried next tick.
8. **Detection** (tenantguard mode):
   - **Hash split:** when a project is hurt and its CPU stays unexplained for `unattributed_hold_s`. Each round pins half of the suspects to each of two replicas, then settles and samples. After ⌈log₂ k⌉ rounds one tenant remains, and it is charged the unexplained CPU.
   - **No-blame external contention:** when every project is slow while none is above its allocation.
9. **Snapshot** to the gateway: tenants (key hashes, quotas), replicas, the ladder's limits, split pins.

## The ladder (every 250 ms)
This is the v1 ladder, unchanged:
- **Steps:** warn → throttle (×0.5 down to 0.25) → deprioritize → quarantine, with AIMD recovery and operator holds.
- **Scope:** a tenant's neighbours are the other tenants of its project. A slowed tenant's in-flight requests do not count toward its own slot usage (they are a symptom).
- **Publishing:** limits travel in the snapshot. A change publishes at once, otherwise every control tick (which renews the TTL).

## API
| Endpoint | |
|---|---|
| `GET /healthz` | Liveness, telemetry, snapshots sent, backend, replicas |
| `GET /api/state` | `StateSnapshot`: tenants, replicas as `servers`, and `pool` (`PoolView`) |
| `GET /api/pool` | `PoolView`: floors, demand, use, base + loan, replicas per project |
| `GET /api/incidents` | Incident events (`?tenant=`, `?incident_id=`) |
| `POST /api/operator` | `OperatorCmd` release / extend / resume (token) |
| `GET /api/debug` | Scores, ladders, baselines, attribution, pool |
| `WS /ws/events` | State on connect, recent incidents, then live events |
| `GET /admin/registry` | Pool, projects and tenants (no key hashes) |
| `PUT /admin/pool` | `PoolSpec` |
| `POST /admin/projects`, `PATCH /admin/projects/{id}`, `DELETE /admin/projects/{id}` | 422 when an invariant would break (sum of floors, memory, floor ≤ cap); 409 when the project still has tenants |
| `POST /admin/projects/{id}/tenants` | `TenantCreate` → `TenantCreated`; the key is returned once and only its hash is stored |
| `DELETE /admin/tenants/{id}` | Remove a tenant |
| `GET /snapshot` | The gateway pulls this at start |
| `POST /control/wake` | `WakeCmd` from the gateway: a project with no ready replica got a request |

## Files
`registry_store.py` keeps the registry in one JSON file, written atomically. Incidents and replica actions are appended to `control.events_path`, the audit log.

## Layout
| File | Contents |
|---|---|
| `settings.py` | Settings |
| `registry.py`, `registry_store.py` | Registry and its store |
| `allocator.py` | Allocation |
| `scaler.py` | Replica targets and the plan |
| `backends.py` | Docker / process |
| `detector.py` | Attribution, hash split, contention |
| `control.py` | The single writer |
| `core.py` | Ladder and incidents |
| `ladder.py`, `score.py`, `incidents.py`, `fairness.py` | From v1 |
| `runtime.py` | I/O loops |
| `main.py` | FastAPI |
| `sim.py` | Simulator for tests |
