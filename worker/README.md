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
All `/api/*` calls need `X-TenantGuard-Tenant`: missing or malformed → 400, and an id that is not exactly
one of the configured tenants (`load_tenants()`) → 400 `unknown_tenant`. Every response carries
`X-TenantGuard-Cpu-Ms` (handler thread CPU, decimal ms; background CPU excluded). Errors are `{error, tenant, detail}`.

| Endpoint | What |
|---|---|
| `GET /api/light` | ~0.5–1 ms CPU; the victim SLO probe |
| `GET /api/read?key=k0001` | tenant-namespaced cache → tenant SQLite; 404 if absent |
| `GET /api/search?n=20000&q=` | CPU-heavy, linear in n (~0.3–0.4 µs/doc), n ≤ 2,000,000 |
| `POST /api/write {key?, value, index_rounds=200}` | DB write + background indexing job charged to the tenant (`bg_cpu_ms`); 503 `bg_backlog` past `worker.bg_pending` caps |
| `POST /api/ingest {docs=10}` | 202; hands docs to the black-box "vendor indexer" child (`worker.blackbox.cpu_ms_per_doc` each); CPU only in `blackbox_cpu_ms` |
| `GET /api/memory?mb=&hold_ms=500` | allocate + hold; 507 `tenant_memory_budget` (held + cache > `tenant_mem_budget_mb`) or `worker_memory` (all tenants > mem limit − `mem_baseline_mb`) |
| `GET /api/export?rows=1000&chunk=100&pause_ms=20` | batch CSV export, slow (≈ rows/chunk × pause); 422 if that estimate > `worker.max_request_s` |
| `GET /canary`, `GET /canary/{cpu,cache,db}` | unattributed probes (see fault latency) |
| `GET /healthz` | liveness + background backlog per tenant + black-box queue/drops/restarts |
| `GET /stats` | `WorkerStats` (cumulative counters; `cpu_total_ms` = psutil process tree) |
| `POST/GET/DELETE /admin/fault {latency_ms, cpu_burn_pct, duration_s?}` | faulty-host simulation; header `X-Admin-Token` |
| `GET /admin/memory`, `POST /admin/memory/trim` | where the memory is (process RSS, cgroup anon/file, allocator heap/free/mmapped, per-tenant holds, cache) and an on-demand `malloc_trim`; header `X-Admin-Token` |

Body limit `worker.body_limit_bytes` (1 MB) → 413, also without Content-Length. An unhandled error → 500 JSON,
still with the CPU header.

## Design
- `tenancy.py`: pure-ASGI middleware puts the tenant in a `ContextVar`; it flows into the sync handler's thread.
- `store.py`: one SQLite file per tenant, re-seeded at start; the repository has no tenant parameter.
- `cache.py`: shared LRU keyed by (tenant, key) with a guaranteed share per tenant and an eviction ledger.
- `jobs.py`: background jobs run in a copied context; their thread CPU is charged to that tenant.
- `cpuclock.py`: per-thread CPU. Linux uses `time.thread_time()`; Windows uses `QueryThreadCycleTime`,
  because `GetThreadTimes` only moves in 15.6 ms steps.
- `blackbox.py`: spawned child process fed by a bounded queue (drops counted), restarted with backoff if it
  dies; its CPU (psutil on the child) is reported only as `blackbox_cpu_ms`. It shares the container's CPU
  limit, so heavy ingest slows co-located tenants while every tenant meter stays near zero.
- `malloc.py`: memory goes back to the OS. With default glibc settings, large buffers freed on request threads stayed in
  per-thread arenas (glibc raises its mmap threshold after the first large free): measured +111 MB kept after one
  load burst, which filled the 256 MB container over hours. The worker pins the mmap threshold to 128 KB and caps
  arenas at 2 (`mallopt` at start; `MALLOC_*` env in the image for child processes), allocates `/api/memory` holds
  with `mmap`, trims every `worker.memory_trim_s` (30 s), caps distinct classes per tenant
  (`worker.max_classes_per_tenant`, rest counted as `other`), and bounds each SQLite connection (1 MB page cache,
  WAL ≤ 4 MB). Same load after the fix: +0.6 MB.
- `tools/memrepro.py`: stdlib reproduction of the allocator retention (`--mode bytearray|mmap`, `MALLOC_*` env to compare).
- `tools/noisy_demo.py`: measures the noisy-neighbor effect (`--attack search|ingest`).
- `tools/calibrate.py` → `calibration.json`: cost of each endpoint at default params (stdlib only, shipped in
  the image). On the Docker host, inside a real 0.5-CPU container:
  `docker compose exec -T w1 python tools/calibrate.py --where docker --out - > cal.tmp && mv cal.tmp worker/calibration.json`
  (it waits up to `--wait-s` 90 s for `/healthz`; on failure it exits non-zero and prints no JSON)
- `compose.dev.yml`: overlay that publishes w0..w2 on host ports 18100..18102 for the tools.
