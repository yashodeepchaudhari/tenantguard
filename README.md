# TenantGuard: Multi-Tenant Isolation & Noisy-Neighbor Defense

CodeAstra 2.0 · Cloud Computing & Distributed Systems · PS 3.

**TenantGuard gives every tenant of a shared service its fair share and names the tenant behind any slowdown, even when the expensive work is invisible to a rate limiter.**

**The problem.** In a multi-tenant SaaS, one tenant's traffic can slow everyone who shares its servers: the noisy neighbour. Rate limiters count requests, but a few cheap-looking requests can trigger huge hidden work (background jobs, a vendor indexer), which a request counter never sees. Operators then see slow servers without knowing who caused them, and punishing the wrong tenant is as bad as doing nothing.

**What TenantGuard does.**
- **Quotas on resources, not just requests:** each tenant has quotas on measured CPU and concurrency as well as request rate.
- **Fair scheduling:** queueing is fair per tenant, so a cheap request never waits behind a whale.
- **Victims first:** they are rescued by routing around slow servers.
- **The culprit is named** with a shuffle-sharded "barcode" layout.
- **Adaptive throttling:** a graduated, self-recovering ladder with TTL auto-undo.
- **Everything is explained** live on a dashboard and handed to the on-call engineer as a case.

## Architecture

```mermaid
flowchart LR
  subgraph MGMT["Management plane"]
    UI["Control Room UI<br/>React :5173"]
    ADMIN["Admin API<br/>gateway /admin/*"]
    HO["Handoff bridge :8400<br/>(Web PS3 partner)"]
  end
  subgraph DATA["Data plane"]
    CL["Tenants / traffic lab :8300"] -->|"X-API-Key"| GW
    subgraph GW["Gateway :8000"]
      direction TB
      L1["L1 identity + rate limit"] --> L2["L2 cost bucket (CPU-ms)"] --> L3["L3 fair scheduler<br/>3 DRR lanes, adaptive window"]
    end
    GW -->|"tenant barcode<br/>(3 of 9 servers)"| W["Workers w0..w8<br/>L4 cgroups 0.5 CPU / 256 MB<br/>L5 SQLite + cache per tenant"]
    GW --> SP["spare + quarantine"]
  end
  subgraph CTRL["Control plane"]
    BR["Brain :8200<br/>usage-share ladder · server health<br/>rescue · barcode decoder"]
  end
  GW -- "telemetry WS 250 ms" --> BR
  W -- "/stats 1 Hz" --> BR
  BR -- "/control/* (token, TTL)" --> GW
  BR -- "WS /ws/events" --> UI
  BR -- "incidents" --> HO
  UI --> ADMIN
```

**Isolation layers:**
- **L1:** identity (SHA-256 of `X-API-Key`), rate limit and retry budget.
- **L2:** cost bucket debited with the worker-measured CPU of each request.
- **L3:** per-tenant weighted DRR in three lanes (express / interactive / batch), behind a gradient concurrency window per worker.
- **L4:** Docker cgroup limits per worker.
- **L5:** per-tenant data (one SQLite file per tenant, a namespaced cache with a guaranteed share).

On top of these, the **adaptive loop** (the brain) watches, rescues, names and throttles.

| Component | Path | Port | What it does |
|---|---|---|---|
| Gateway | `gateway/` | 8000 | Identity, rate + cost + concurrency quotas, retry budget, 3-lane DRR, adaptive per-worker window, barcode routing, telemetry WS, admin + control API |
| Workers | `worker/` | 8100 (×9, plus `spare`, `quarantine`) | The shared SaaS app: per-tenant SQLite + cache, tenant-tagged background jobs, a black-box "vendor indexer", per-tenant CPU metering, fault injection |
| Brain | `brain/` | 8200 | Noisy score + throttle ladder; server health → rescue (route-around) → barcode decoder → naming; operator holds; live state + incident stream |
| Lab | `lab/` | 8300 | 12 Poisson tenant personas, attacks, measured scenario runs (`lab/results/*.json`) |
| UI | `ui/` | 5173 | Control Room, Tenants (quota edit), Cases / Case File, Scorecard |
| Handoff bridge | `integrations/handoff/` | 8400 | Incidents become cases in the Web PS3 "Digital Handoff" app; acknowledge and approve flow back |
| Shared | `common/`, `tenantguard.yaml`, `layouts/ag23.json` | | Contracts (Pydantic), every tunable, the AG(2,3) layout: 12 tenants on 9 servers, any two tenants share at most one server |

Deeper reading: [docs/architecture.html](docs/architecture.html) ([PDF](docs/Architecture.pdf)), [docs/SETUP.md](docs/SETUP.md), [docs/SUBMISSION.md](docs/SUBMISSION.md).

## Quick start

**Docker (one machine with ≥ 8 cores / 12 GB for Docker):**
```
docker compose up -d --build                    # gateway, 9 workers, spare, quarantine, brain
docker compose --profile lab up -d lab          # traffic lab (calm traffic starts automatically)
cd ui && npm install && cp .env.example .env && npm run dev     # http://127.0.0.1:5173
```

**Two laptops** (how we run it): Docker on laptop A; lab, UI and demo driver on laptop B over Tailscale. See [docs/SETUP.md](docs/SETUP.md).

**Local, no Docker** (Python 3.10+, for development):
```
pip install -e common
cd worker  && SERVER_NAME=w0 python -m app --port 8100      # repeat for the workers you need
cd gateway && python -m tenantguard_gateway                  # :8000
cd brain   && python -m tenantguard_brain                    # :8200
cd lab     && python -m tenantguard_lab serve                # :8300
```

Every service has `python -m pytest` in its directory. All `/admin/*` and `/control/*` calls need the header `X-TenantGuard-Control-Token` (env `TENANTGUARD_CONTROL_TOKEN`, default `dev`).

## Run the demo
```
python tools/demo.py --dry-run      # the plan and every HTTP call, nothing sent
python tools/demo.py --step         # the 4-minute demo, Enter before each step
```
The steps: calm → OFF + flood → Industry (flood refused, then black-box bomb) → TenantGuard (same bomb) → faulty server → honest spike → scorecard. Each step prints one narration line and live numbers from the brain and the lab. Targets come from `TENANTGUARD_{GATEWAY,BRAIN,LAB}_URL`.

## "Not just a rate limit": the evidence

**Hidden work is real and large.** Measured on the Docker host (`worker/calibration.json`, one worker limited to 0.5 CPU, 200 sequential requests per endpoint):

| Endpoint | CPU the request handler reports | Background job CPU | Black-box indexer CPU | Whole-process CPU per request |
|---|---|---|---|---|
| `GET /api/light` | 0.40 ms | 0 | 0 | 0.95 ms |
| `GET /api/search` | 3.83 ms | 0 | 0 | 4.67 ms |
| `POST /api/write` | 0.19 ms | 1.43 ms | 0 | 3.1 ms |
| **`POST /api/ingest`** | **0.07 ms** | 0 | **20.25 ms** | 23.5 ms |

An `ingest` call looks 6× cheaper than a `light` call to anything that meters the request, but it makes the worker burn about 300× more CPU in the black-box indexer. A rate limiter, and even a per-request CPU quota, lets it through. That gap is why TenantGuard watches *server health* and decodes the culprit from the barcode, instead of trusting request counters.

**3-mode scorecard** (OFF vs Industry vs TenantGuard, victim p99 per attack): the lab writes one `lab/results/*.json` per measured run (`POST /lab/run`), and the UI Scorecard and the demo read those files. **No runs are committed yet: scorecard numbers are to be measured** on the Docker host, and the table will be filled only from those files.

## Repository map
```
common/                 shared contracts + config loader (pip install -e common)
tenantguard.yaml        every tunable (tiers, quotas, ladder thresholds, TTLs, ports)
layouts/ag23.json       12 tenants x 9 servers, AG(2,3) barcodes
gateway/                tenantguard_gateway: admission, scheduler, limiter, router, telemetry, admin/control API
worker/                 app: tenancy, store, cache, jobs, blackbox, meter, fault; calibration.json
brain/                  tenantguard_brain: score, ladder, health, rescue, decoder, prover (interface), incidents
lab/                    tenantguard_lab: personas, attacks, scenario runner, results
ui/                     React 18 + Vite + Tailwind + Recharts dashboard
integrations/handoff/   Web PS3 convergence bridge + mock web app
tools/demo.py           one-command demo driver
docs/                   architecture (HTML/PDF), SETUP, SUBMISSION, plan, convergence samples
docker-compose.yml      the platform (+ profiles: lab, handoff)
```
