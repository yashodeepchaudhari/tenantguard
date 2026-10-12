# TenantGuard gateway

Every tenant request enters on `:8000`. The gateway identifies it, admits it against multi-dimensional quotas, schedules it fairly, routes it to one of the tenant's barcode servers, measures it, and obeys the admin API and the brain's control commands.

## Run
```
cd gateway
pip install -r requirements.txt            # tenantguard_common must already be installed
python -m tenantguard_gateway              # binds 0.0.0.0:8000 (port from tenantguard.yaml services.gateway)
python -m pytest                           # needs pytest (requirements-dev.txt)
python tools/stub_worker.py --name w0 --port 8100   # optional echo worker; the real one is worker/
python tools/prove.py                      # end-to-end proof against a running gateway + workers
docker compose up gateway                  # from the repo root
```

Env: `TENANTGUARD_WORKER_URLS` (`w0=http://..,w1=..`, else `tenantguard.yaml workers`), `TENANTGUARD_CORS_ORIGINS` (comma list, default `http://127.0.0.1:5173,http://localhost:5173`), `TENANTGUARD_GATEWAY_HOST` / `TENANTGUARD_GATEWAY_PORT`, `TENANTGUARD_UPSTREAM_TIMEOUT_S` (default 30).

## Request path (`ANY /api/{path}`)
1. **Identity**: the SHA-256 of `X-API-Key` maps to the tenant; only the hash is kept. A missing or unknown key gets **401** JSON. A body over `worker.body_limit_bytes` gets **413**.
2. **Class**: `sha1(METHOD|path|sorted query keys|body-size bucket)[:8]`, sent to the worker as `X-TenantGuard-Class`.
3. **Admission** (all modes except `off`). Every dimension is checked, and a token is consumed only if all of them pass:
   - block (control);
   - token bucket (`rate_rps`, `burst`);
   - cost bucket in CPU-ms: refills at `cost_ms_per_s`, capacity `cost_ms_per_s × gateway.cost_bucket_s`. It is debited **after** the response with the worker's `X-TenantGuard-Cpu-Ms`, and requests are refused while it is negative;
   - concurrency (queued + in flight).

   A refusal returns **429** with `Retry-After`: integer seconds, rounded up, equal to the time until *every* refusing dimension passes (the concurrency dimension advertises 1 s). The JSON body carries `reasons` and `retry_after_ms`, and the response carries the `RateLimit-Policy` / `RateLimit` headers (structured-field draft, one named policy per dimension: `rate`, `cost`, `concurrency`). These headers are also on every admitted response.
4. **Scheduler**: per-tenant queues served by weighted DRR.
   - Weight = tier weight × `weight_factor`.
   - Quantum = 5 CPU-ms × weight per turn.
   - An item costs its (tenant, class) EWMA of measured CPU, default 5 ms.
   - `off` mode uses one global FIFO (same queues, ordered by arrival).
   - Bounded wait comes from `gateway.max_queue_wait_ms` (`interactive`; `batch` for `/api/export`). An expired request gets **503** `queue_timeout` and is never forwarded.
   - Dispatch is limited per worker by `limiter.initial_window` (fixed in Wave 1).
5. **Routing**: weighted random among the tenant's servers that have a free slot (equal weights by default). In `tenantguard` mode, route weights, relocation (tenant or tenant/class → `spare`/`quarantine`/`wN`) and drains apply. Drains never black-hole a tenant: if every candidate is drained, the request falls back to the barcode.
6. **Forward** with a pooled httpx client. The gateway adds `X-TenantGuard-Tenant`, `X-TenantGuard-Class` and `X-Request-Id`, and drops `X-API-Key` and any client-supplied trusted headers. The response carries `X-TenantGuard-Server`. An unreachable worker gives **502**. A response with a missing or unparseable CPU header debits 0 and is counted in `tenantguard_cpu_header_missing_total`.

## Modes
| | admission | scheduler | control overrides |
|---|---|---|---|
| `off` | none | FIFO | stored, not applied |
| `industry` | rate + cost + concurrency | weighted DRR | stored, not applied |
| `tenantguard` | same + LimitsCmd/BlockCmd | DRR × weight_factor | applied |

Bounded wait and the per-worker window apply in every mode, so the queue can never grow without limit.

## API
| Endpoint | |
|---|---|
| `GET /healthz` | liveness |
| `WS /internal/telemetry` | `TelemetryFrame` every `tick_ms` to every client; the latest frame is sent on connect, and a slow client drops its oldest frames |
| `GET /internal/telemetry/latest` | the last frame (JSON) |
| `GET /metrics` | Prometheus text: requests, admitted, rejected by reason, expired, CPU, e2e histogram, queue depth, in-flight, cost level, throttle factor, worker window, fail-static, heartbeat age, loop lag |
| `GET /admin/tenants`, `GET /admin/tenants/{id}` | `TenantAdminView` (never `api_key`). `effective_quota`, `factor`, `blocked` and `placement` reflect what is enforced in the current mode |
| `POST /admin/tenants` | `TenantConfig` → 201. 409 for a duplicate id or key; 400 for bad quota or unknown servers |
| `DELETE /admin/tenants/{id}` | remove a tenant |
| `PATCH /admin/tenants/{id}/quota` | `QuotaPatch` → 200. 400 if a value is ≤ 0. Takes effect on the next request |
| `GET/POST /admin/mode` | `ModeBody` |
| `GET /admin/overrides` | `OverridesView` (expired entries are swept first) |
| `POST /control/limits` | `LimitsCmd`: replaces the previous command and restarts the TTL. 1.0/1.0 clears it; factors outside (0, 1] get 400 |
| `POST /control/block` | `BlockCmd`: `blocked=false` clears it |
| `POST /control/route_weights` | `RouteWeightsCmd`: weights are normalized; servers must be known; each tenant needs at least one weight > 0 |
| `POST /control/relocate` | `RelocateCmd`: `target=None` clears it |
| `POST /control/drain` | `DrainCmd`: `drained=false` clears it |
| `POST /control/heartbeat` | `Heartbeat`: the gateway records its *own* receive time |

Every control command has `ttl_s > 0` and reverts on its own: getters check expiry and the tick sweeps. Invalid control input, including malformed JSON, gets **400** `ControlAck(ok=false)` and never crashes the gateway. `fail_static` is true when no heartbeat has arrived within `gateway.fail_static_after_s`, or none has ever arrived. While fail-static the gateway keeps its last config and overrides still expire at their TTL.

## Layout
`settings.py` (yaml → dataclass), `buckets.py`, `admission.py`, `scheduler.py`, `router.py`, `overrides.py`, `tenants.py` (identity, class, registry), `telemetry.py`, `metrics.py`, `core.py` (request path, event-driven dispatch), `app.py` (FastAPI). The pure modules have no I/O and take `now` explicitly.
