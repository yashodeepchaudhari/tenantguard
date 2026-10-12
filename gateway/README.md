# TenantGuard gateway (v2)

Every tenant request enters on `:8000`. The gateway identifies the tenant and its project, admits the request against the tenant's tier quota, queues it with the project's other requests (fair per tenant), and routes it to one of the project's ready replicas. It then measures the request and obeys the brain's snapshot. Tenants, projects and replicas come only from the brain (`POST /control/snapshot`, or `GET {brain}/snapshot` at start).

## Run
```
cd gateway
pip install -r requirements.txt            # tenantguard_common must already be installed
python -m tenantguard_gateway              # binds 0.0.0.0:8000 (port from tenantguard.yaml services.gateway)
python -m pytest                           # needs pytest (requirements-dev.txt)
python tools/stub_worker.py --name w0 --port 8100   # optional echo worker; the real one is worker/
```

**Environment variables:**

| Variable | Meaning |
|---|---|
| `TENANTGUARD_CONTROL_TOKEN` | Default `dev`. Required as `X-TenantGuard-Control-Token` on every `/admin/*` and `/control/*` call, and sent to the brain. |
| `TENANTGUARD_BRAIN_URL` | Else `services.brain` |
| `TENANTGUARD_CORS_ORIGINS` | Comma list |
| `TENANTGUARD_GATEWAY_HOST` / `TENANTGUARD_GATEWAY_PORT` | Bind address |
| `TENANTGUARD_UPSTREAM_TIMEOUT_S` | Upstream request timeout |

## Request path (`ANY /api/{path}`)
1. **Identity:** the SHA-256 of `X-API-Key` is looked up in the snapshot's tenants, giving (tenant, project). A missing or unknown key gets **401**, and the body is never read.
2. **Hardening** (unchanged from v1): the path must stay under `/api/`, the body size is capped (**413**), `X-Request-Id` is validated, and gateway-owned headers are stripped.
3. **Class:** `sha1(METHOD|path|sorted query keys|body-size bucket|size bucket)[:8]`. The size bucket is `log2` of numeric `gateway.size_params` (`n`, `limit`, `pages`), so `search?n=1000` and `search?n=500000` are separate classes with separate costs.
4. **Reference cost** (`costs.py`, the only cost estimate): the measured (tenant, class) EWMA (α 0.2), else the endpoint's handler CPU from `worker/calibration.json` (`gateway.cost_seed`), else 5 ms.
5. **Retry budget** (industry, tenantguard): v1, unchanged.
6. **Admission** (all modes except `off`): block, token bucket (tier `rate_rps`, `burst` × factor), concurrency (tier × factor). A refusal returns **429** with `Retry-After` plus the `RateLimit` headers.
   - **No CPU bucket:** a project's CPU is bounded by its replicas' cgroups, and DRR charges every request its cost.
   - The cost of refused requests is reported as unmet demand (`QueueStat.refused_cost_ms`).
7. **Queues per project:** each project has its own express / interactive / batch lanes, each a DRR across its tenants (weight 1 × the ladder's `weight_factor`; quantum 5 CPU-ms).
   - A flood in one project never delays another project's queue.
   - Bounded wait gives **503** `queue_timeout`. Batch is **shed** first when the project's replicas are saturated.
8. **Wake:** if the tenant's project has no ready replica, the gateway POSTs `/control/wake` to the brain (debounced, `gateway.wake_debounce_s`). The request waits up to `gateway.cold_wait_ms`.
9. **Routing** (`router.py`): a split-test pin (tenantguard mode) if present, else rendezvous hashing over the project's `ready` replicas.
   - A tenant sticks to one replica; it spills to the next in rendezvous order only when its first choice has no free slot.
   - Adding a replica moves about 1/(n+1) of the tenants; removing one moves only its own tenants.
10. **Per-replica adaptive window:** the v1 Gradient2 limiter, one per replica. Lane caps are as in v1.
11. **Forward** with the trusted headers plus `X-TenantGuard-Replica`. The response carries `X-TenantGuard-Server` (the replica id) and `X-TenantGuard-Lane`.

## Modes
| | Admission | Scheduler | Brain overrides (limits, pins, blocks) |
|---|---|---|---|
| `off` | None | One FIFO per project, fixed window | Stored, not applied |
| `industry` | Retry budget + rate + concurrency | 3 DRR lanes per project, adaptive window | Stored, not applied |
| `tenantguard` | Same | Same, DRR × `weight_factor` | Applied |

## API
| Endpoint | |
|---|---|
| `GET /healthz` | Liveness, replicas and snapshot version |
| `WS /internal/telemetry`, `GET /internal/telemetry/latest` | `TelemetryFrame` every `tick_ms`. Cells are per (tenant, replica); queue stats include `queued_cost_ms` and `refused_cost_ms` |
| `GET /metrics` | Prometheus text |
| `GET /admin/tenants`, `GET /admin/tenants/{id}` | `TenantAdminView`: project, replicas in this tenant's rendezvous order, factors, pin. Never a key or key hash |
| `GET/POST /admin/mode` | `ModeBody` |
| `GET /admin/overrides` | `OverridesView`: limits, blocks, pins, expiries, snapshot version and age, fail-static |
| `GET /admin/costs` | Cost seed and measured EWMAs |
| `POST /control/snapshot` | `Snapshot` from the brain, swapped in atomically. 400 for factors outside (0, 1], a bad TTL, or pins to unknown tenants or replicas |
| `POST /control/block` | `BlockCmd` from the handoff bridge. `blocked=false` clears it |

- Every `/admin/*` and `/control/*` call requires the control token, else 401.
- Limits and pins in a snapshot replace the previous ones and expire at their TTL.
- `fail_static` is true when no snapshot has arrived within `gateway.fail_static_after_s`. The gateway then keeps serving with the last snapshot.

## Layout
| File | Contents |
|---|---|
| `settings.py` | Settings |
| `buckets.py` | Token bucket |
| `admission.py` | Admission checks |
| `costs.py` | Reference cost |
| `scheduler.py` | DRR |
| `limiter.py` | Gradient window |
| `retry.py` | Retry budget |
| `security.py` | Path and request-id checks |
| `router.py` | Pin + rendezvous |
| `overrides.py` | Snapshot limits and pins, blocks, fail-static clock |
| `tenants.py` | Identity, class, registry from the snapshot |
| `telemetry.py`, `metrics.py` | Telemetry and Prometheus output |
| `core.py` | Request path, per-project dispatch, wake |
| `app.py` | FastAPI |
