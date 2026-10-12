# TenantGuard worker

The shared multi-tenant "SaaS app". Every worker container (w0..w8, spare, quarantine) runs this same image.

## Run
```
cd worker
pip install -r requirements.txt            # tenantguard_common must already be installed
SERVER_NAME=w1 python -m app --port 8101   # binds 0.0.0.0; data in ./data/w1/
python -m pytest
docker compose up w0 w1                    # from the repo root; listens on 8100 in the container
```

Env: `SERVER_NAME`, `WORKER_DATA_DIR` (Docker: `/data`), `TENANTGUARD_ADMIN_TOKEN` (default `dev`),
`CPU_LIMIT_CORES` (else cgroup `cpu.max`, else `tenantguard.yaml worker.cpus`), `TENANTGUARD_UI_ORIGINS`
(CORS, comma list, default `http://127.0.0.1:5173`).

## API
All `/api/*` calls need `X-TenantGuard-Tenant` (missing or invalid → 400). Every response carries
`X-TenantGuard-Cpu-Ms` (handler thread CPU, decimal ms; background CPU excluded). Errors are `{error, tenant, detail}`.

| Endpoint | What |
|---|---|
| `GET /api/light` | ~0.5–1 ms CPU; the victim SLO probe |
| `GET /api/read?key=k0001` | tenant-namespaced cache → tenant SQLite; 404 if absent |
| `GET /api/search?n=20000&q=` | CPU-heavy, linear in n (~0.3–0.4 µs/doc), n ≤ 2,000,000 |
| `POST /api/write {key?, value, index_rounds=200}` | DB write + background indexing job charged to the tenant (`bg_cpu_ms`) |
| `GET /api/memory?mb=&hold_ms=500` | allocate + hold; 507 if over `worker.tenant_mem_budget_mb` (held + cache bytes) |
| `GET /api/export?rows=1000&chunk=100&pause_ms=20` | batch CSV export, slow (≈ rows/chunk × pause) |
| `GET /canary`, `GET /canary/{cpu,cache,db}` | unattributed probes (see fault latency) |
| `GET /healthz` | liveness |
| `GET /stats` | `WorkerStats` (cumulative counters; `cpu_total_ms` = psutil process tree) |
| `POST/GET/DELETE /admin/fault {latency_ms, cpu_burn_pct, duration_s?}` | faulty-host simulation; header `X-Admin-Token` |

Body limit `worker.body_limit_bytes` (1 MB) → 413, also without Content-Length.

## Design
- `tenancy.py`: pure-ASGI middleware puts the tenant in a `ContextVar`; it flows into the sync handler's thread.
- `store.py`: one SQLite file per tenant, re-seeded at start; the repository has no tenant parameter.
- `cache.py`: shared LRU keyed by (tenant, key) with a guaranteed share per tenant and an eviction ledger.
- `jobs.py`: background jobs run in a copied context; their thread CPU is charged to that tenant.
- `cpuclock.py`: per-thread CPU. Linux uses `time.thread_time()`; Windows uses `QueryThreadCycleTime`,
  because `GetThreadTimes` only moves in 15.6 ms steps.
- `tools/noisy_demo.py`: measures the noisy-neighbor effect on real workers.
