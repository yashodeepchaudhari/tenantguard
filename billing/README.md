# TenantGuard billing (:8600)

Fair bills and "Why was I slow?". Read-only: it never sends control commands.

## Run
```
cd billing
pip install -r requirements.txt                # tenantguard_common must already be installed
TENANTGUARD_ROOT=<repo> python -m tenantguard_billing --port 8600
python -m pytest
```
Env: `TENANTGUARD_WORKER_URLS`, `TENANTGUARD_BRAIN_URL`, `TENANTGUARD_GATEWAY_URL` (defaults from
`tenantguard.yaml services`/`workers`), `TENANTGUARD_CONTROL_TOKEN` (gateway `/admin/tenants`),
`TENANTGUARD_CORS_ORIGINS` (or `TENANTGUARD_UI_ORIGINS`). Tunables: `tenantguard.yaml billing:`.
SLA targets: `sla_p95_ms` per tenant in the layout.

## API (contracts section 7)
| Endpoint | What |
|---|---|
| `GET /bills?from=&to=` | `BillView` for every tenant (default period: since billing started) |
| `GET /bills/{tenant}?from=&to=` | one tenant's bill; 404 for an unknown id (exact match) |
| `GET /why/{tenant}?from=&to=` | `WhyView` (default: last 15 min) |
| `GET /healthz` | per-source status (workers, brain incidents + WS, gateway), counts |

## How a bill is fair
- **Metering:** every `poll_s`, each worker's cumulative `/stats` is diffed into `bucket_s` buckets per tenant and
  class. A worker restart (`started_at` changed) counts the new counters from zero. History from before billing
  started is a baseline, never billed.
- **Calm reference prices:** a bucket that no brain incident overlaps is calm. It bills measured CPU and updates
  the calm cost per request of each (tenant, class) (EWMA `ref_alpha`). Per tenant, because the gateway class
  ignores query values (a whale's search shares a class with a light one).
- **During an incident** references are frozen. A victim pays, per class, `min(measured, count x calm ref)`, and
  `contention_waived_cpu_ms` shows what was not charged. The tenant the incident is about (usage-ladder tenant or
  named culprit) pays measured.
- **SLA credits:** each 0.5 s brain snapshot where a tenant's p95 is above its `sla_p95_ms` while an incident it
  is not blamed for is open counts 0.5 s. `sla_credit_table` turns seconds into a % of the period's usage subtotal.
- **True cost:** for a culprit named with confidence `>= unmetered_min_confidence`, the black-box CPU on its
  servers from `named` to `closed`, minus those servers' calm black-box rate, is billed as an `unmetered` line,
  with the incident id and confidence as evidence.
- **Anonymous by construction:** victim text is built only from structured data (stage times, slow-server
  status, barcode counts). The brain's `detail` strings, which name tenants, are never copied. A victim sees
  incidents as opaque `ref-<8 hex>`.
