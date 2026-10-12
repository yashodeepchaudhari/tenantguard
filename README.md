# Padosi: Multi-Tenant Isolation & Noisy-Neighbor Defense

CodeAstra 2.0 · Cloud Computing & Distributed Systems · PS 3.

Padosi is a fair-share control plane for a multi-tenant service. It identifies every tenant, enforces quotas on resources (not just request counts), schedules fairly, measures each tenant's latency and resource use, detects noisy neighbors and throttles or relocates them adaptively. An admin API and a live dashboard sit on top.

## Layout
| Path | What |
|---|---|
| `common/` | Shared contracts (`padosi_common.contracts`) and config loader. Install it with `pip install -e common` |
| `padosi.yaml` | Every tunable value |
| `layouts/ag23.json` | 12 tenants on 9 servers (AG(2,3) barcodes) |
| `gateway/` | Identity, quotas, rate + cost limits, fair scheduler, routing, telemetry, admin + control API (:8000) |
| `worker/` | The shared SaaS app; per-tenant metering and data isolation (:8100..8110) |
| `brain/` | Detector, throttle ladder, Padosi loop, event stream for the UI (:8200) |
| `lab/` | Tenant traffic, attacks, measured scenario runs (:8300) |
| `ui/` | React dashboard (:5173) |
| `docs/PLAN.md` | Coordinator plan: how we work, rules, ownership, waves, briefs |

## Run (Docker)
```
docker compose up --build              # platform
docker compose --profile lab up lab    # traffic lab
cd ui && npm install && npm run dev     # dashboard
```

## Run (local, no Docker)
```
pip install -e common
# each service README gives its uvicorn command; ports are in padosi.yaml
```
