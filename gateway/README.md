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
python tools/prove_t7.py whale|storm|security   # wave 2 proof (express lane, retry storm, window, hardening)
docker compose up gateway                  # from the repo root
```

Env: `TENANTGUARD_CONTROL_TOKEN` (default `dev`; required as `X-TenantGuard-Control-Token` on every `/admin/*` and `/control/*` call, else 401), `TENANTGUARD_WORKER_URLS` (`w0=http://..,w1=..`, else `tenantguard.yaml workers`), `TENANTGUARD_CORS_ORIGINS` (comma list, default `http://127.0.0.1:5173,http://localhost:5173`), `TENANTGUARD_GATEWAY_HOST` / `TENANTGUARD_GATEWAY_PORT`, `TENANTGUARD_UPSTREAM_TIMEOUT_S` (overrides yaml `gateway.upstream_timeout_s`).

## Request path (`ANY /api/{path}`)
1. **Identity**: the SHA-256 of `X-API-Key` maps to the tenant; only the hash is kept. A missing or unknown key gets **401**, and the body is never read.
2. **Hardening**:
   - The path must provably stay under `/api/`, else **400** `bad_path`. Rejected: encoded `/` `\` NUL `#` `?`, `.` or `..` segments, `//`, control characters. The raw path is forwarded, so the worker decodes exactly what was checked.
   - `Content-Length` over `worker.body_limit_bytes` gets **413** before reading. The body is read as a capped stream, so a chunked body over the limit also gets 413.
   - `X-Request-Id` must match `^[A-Za-z0-9-]{1,64}$`, else a fresh one is generated. It is forwarded exactly once.
   - Client `X-TenantGuard-*`, `X-Admin-Token`, `X-Request-Id` and `X-API-Key` headers are never forwarded.
3. **Class**: `sha1(METHOD|path|sorted query keys|body-size bucket)[:8]`, sent to the worker as `X-TenantGuard-Class`.
4. **Retry budget** (industry, tenantguard). A request is a retry if it repeats an `Idempotency-Key`, or repeats the fingerprint (method + path + full query + body hash) of a request that **failed** (429/503/5xx) within `retry_budget.window_s`. Plain repeat traffic is not a retry. Retries are allowed while retries in the window stay ≤ max(`min_allowed`, `ratio` × first attempts in the window), else **429** reason `retry_budget`. Retries are counted in `QueueStat.retries` and `tenantguard_retries_total`.
5. **Admission** (all modes except `off`). Every dimension is checked, and a token is consumed only if all of them pass:
   - block (control);
   - token bucket (`rate_rps`, `burst`);
   - cost bucket in CPU-ms: refills at `cost_ms_per_s`, capacity `cost_ms_per_s × gateway.cost_bucket_s`. It is debited **after** the response with the worker's `X-TenantGuard-Cpu-Ms`. If the client disconnects mid-flight, the class estimate is debited instead;
   - concurrency (queued + in flight).

   A refusal returns **429** with `Retry-After`: integer seconds, rounded up, equal to the time until every refusing dimension passes. The JSON body carries `reasons` and `retry_after_ms`, plus the `RateLimit-Policy` / `RateLimit` headers.
6. **Lanes**: three weighted-DRR schedulers, served express → interactive → batch, each per-tenant fair (weight = tier weight × `weight_factor`; quantum 5 CPU-ms × weight).
   - **express**: the class reference cost is < `gateway.express_cost_ms`. It may use the whole worker window.
   - **interactive**: the default. It may use the window minus max(1, window/8), which keeps headroom for express.
   - **batch**: the client sends `X-TenantGuard-Class: batch`, or the path is in `gateway.batch_routes`. It waits up to `max_queue_wait_ms.batch` and only starts when slots < window/2. When interactive or express work is waiting and a queued batch item's servers have no interactive headroom, the batch item is **shed** (503 `shed`).
   - Reference cost: a brain `reference_costs` entry (tenantguard mode) > the measured (tenant, class) EWMA > 5 ms.
   - The bounded wait expires with **503** `queue_timeout`, and the request is never forwarded.
   - `off` mode: one FIFO, no lanes.
7. **Adaptive window per worker** (industry, tenantguard; fixed `initial_window` in `off`). Gradient limiter: `new = old × (minRTT / sampleRTT) + sqrt(old)`, smoothed (0.2), clamped to `limiter.min_window..max_window`.
   - The RTT is the worker-side **excess** latency (upstream ms − the request's own CPU ms). An expensive request is not counted as congestion; waiting inside the worker is.
   - Each tick takes the median of that tick's samples.
   - minRTT drifts up 0.1% per tick, so a permanently slower host is re-baselined.
   - The window does not grow unless at least half of it is in use.
   - The window is exposed in `LimiterStat.window` and `tenantguard_worker_window`.
8. **Routing**: weighted random among the tenant's servers that have a free slot in that lane. In `tenantguard` mode, route weights, relocation and drains apply. Drains never black-hole a tenant.
9. **Forward** with a pooled httpx client (timeout `gateway.upstream_timeout_s`). The gateway adds `X-TenantGuard-Tenant`, `X-TenantGuard-Class` and `X-Request-Id`. The response carries `X-TenantGuard-Server` and `X-TenantGuard-Lane`. An unreachable worker gives **502**. A missing or unparseable CPU header debits 0 and is counted in `tenantguard_cpu_header_missing_total`.

## Modes
| | admission | scheduler | control overrides |
|---|---|---|---|
| `off` | none | one FIFO, fixed window | stored, not applied |
| `industry` | retry budget + rate + cost + concurrency | 3 DRR lanes, adaptive window | stored, not applied |
| `tenantguard` | same + LimitsCmd/BlockCmd | same, DRR × weight_factor | applied (incl. charge, reference_costs) |

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
| `POST /control/charge` | `ChargeCmd`: debits background CPU from the cost bucket. tenantguard mode only (otherwise `ignored: mode`); idempotent by `charge_id` (the last 1024 are kept); 400 if `cpu_ms` < 0, not finite, or the tenant is unknown |
| `POST /control/reference_costs` | `ReferenceCostsCmd`: the latest table replaces the previous one; values must be finite and > 0. Applied in tenantguard mode |
| `GET /admin/reference_costs` | the pushed table plus the measured (tenant/class) EWMA |

All `/admin/*` and `/control/*` endpoints require `X-TenantGuard-Control-Token` (compared with `hmac.compare_digest`), else 401. Quotas (POST/PATCH) must be finite, > 0 and ≤ `gateway.quota_max`. Every control override needs a finite `0 < ttl_s ≤ gateway.ttl_max_s` and reverts on its own: getters check expiry and the tick sweeps. Invalid control input, including malformed JSON, gets **400** `ControlAck(ok=false)` and never crashes the gateway. `fail_static` is true when no heartbeat has arrived within `gateway.fail_static_after_s`, or none has ever arrived. While fail-static the gateway keeps its last config and overrides still expire at their TTL.

## Layout
`settings.py` (yaml → dataclass), `buckets.py`, `admission.py`, `scheduler.py`, `limiter.py` (gradient window), `retry.py` (retry budget), `security.py` (path / request-id checks), `router.py`, `overrides.py`, `tenants.py` (identity, class, registry), `telemetry.py`, `metrics.py`, `core.py` (request path, event-driven dispatch), `app.py` (FastAPI). The pure modules have no I/O and take `now` explicitly.
