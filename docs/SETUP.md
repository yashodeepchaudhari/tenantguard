# TenantGuard setup

Three ways to run TenantGuard:
1. **Two laptops** (how we demo): Docker on laptop A, traffic and dashboard on laptop B.
2. **One machine with Docker:** everything in compose.
3. **Local, no Docker:** for development. Every service runs with `python -m ...`.

Ports, URLs and tunables come from `tenantguard.yaml` and env; nothing is hard-coded to `127.0.0.1` for a peer service.

## 1. Two laptops + Tailscale

```
 Laptop A (Docker host)                           Laptop B (dev + traffic)
 ┌──────────────────────────────────┐   Tailscale  ┌────────────────────────────────┐
 │ gateway :8000   brain :8200       │◄──────────►│ lab :8300   (traffic, attacks)  │
 │ w0..w8, spare, quarantine (:8100) │  100.x.y.z  │ UI  :5173   (Vite + browser)    │
 │ (handoff :8400, profile handoff)  │             │ tools/demo.py                   │
 └──────────────────────────────────┘             └────────────────────────────────┘
```

Why two laptops: 11 worker containers at 0.5 CPU each plus the brain need about 8 cores to themselves. Generating traffic on the same machine would distort the latencies we measure.

**Laptop A, one time.** Follow [DOCKER-HOST-SETUP.md](DOCKER-HOST-SETUP.md):
- Docker Desktop with WSL 2, and `.wslconfig` giving Docker 8 CPUs / 12 GB.
- The firewall rule for 8000, 8200 and 8300.
- The OpenSSH server, so laptop B can drive Docker.
- Sleep disabled while plugged in.

**Network.** Install Tailscale on both laptops and sign in to the same tailnet. Laptop A gets a stable `100.x.y.z` address, which keeps working across Wi-Fi changes and hotspots. The LAN IP is a fallback.

**Start the platform on laptop A.** Run it in one SSH session; Windows sshd resets parallel sessions, so don't start services in parallel over remote `docker --context`:
```
git archive --format=tar master | ssh <host-alias> "tar -x -f - -C C:\Users\<user>\tenantguard-deploy"
ssh <host-alias> "cd /d C:\Users\<user>\tenantguard-deploy && docker compose -p codeastra up -d --build"
```
`docker compose ps` should show 13 healthy containers. `curl http://<A>:8000/healthz` returns 200, and `curl http://<A>:8000/admin/mode` returns 401 without the token.

**Laptop B.**
```
pip install -e common
cd lab && TENANTGUARD_GATEWAY_URL=http://<A>:8000 TENANTGUARD_BRAIN_URL=http://<A>:8200 python -m tenantguard_lab serve
cd ui  && cp .env.example .env     # set VITE_BRAIN_URL / VITE_GATEWAY_URL to http://<A>:..., VITE_LAB_URL to http://127.0.0.1:8300
npm install && npm run dev         # http://127.0.0.1:5173
TENANTGUARD_GATEWAY_URL=http://<A>:8000 TENANTGUARD_BRAIN_URL=http://<A>:8200 \
TENANTGUARD_LAB_URL=http://127.0.0.1:8300 python tools/demo.py --step
```

## 2. One machine with Docker
```
docker compose up -d --build                       # gateway, w0..w8, spare, quarantine, brain
docker compose --profile lab up -d lab             # + traffic lab
docker compose --profile handoff up -d handoff     # + Web PS3 handoff bridge (set WEB_URL)
cd ui && npm install && cp .env.example .env && npm run dev
```
Inside compose, every worker listens on 8100 and the gateway and brain find them through `TENANTGUARD_WORKER_URLS`. Only the gateway (8000), brain (8200), lab (8300) and handoff (8400) are published.

## 3. Local development (no Docker)

You need Python 3.10+ and Node 20+.
```
pip install -e common                   # once; every service imports tenantguard_common
cd worker  && pip install -r requirements.txt && SERVER_NAME=w0 python -m app --port 8100
cd gateway && pip install -r requirements.txt && python -m tenantguard_gateway
cd brain   && pip install -r requirements.txt && python -m tenantguard_brain
cd lab     && pip install -r requirements.txt && python -m tenantguard_lab serve
cd integrations/handoff && pip install -r requirements.txt && python -m tenantguard_handoff
cd ui && npm install && npm run dev     # or npm run dev:mock: no backend at all
```
- Local worker ports are w0..w8 → 8100..8108, spare → 8109, quarantine → 8110.
- Without the real gateway and workers, `brain/tools/fake_gateway.py` streams a scripted whale scenario, and `gateway/tools/stub_worker.py` is a minimal echo worker.
- Tests: `python -m pytest` in each service directory; `npm test` in `ui/`.

## Ports
| Service | Port | Exposed in compose |
|---|---|---|
| gateway | 8000 | yes |
| workers w0..w8 | 8100 (compose) / 8100..8108 (local) | no |
| spare / quarantine | 8100 (compose) / 8109, 8110 (local) | no |
| brain | 8200 | yes |
| lab | 8300 | yes (profile `lab`) |
| handoff bridge | 8400 | yes (profile `handoff`) |
| UI (Vite) | 5173 | runs outside Docker |

## Environment variables
| Variable | Used by | Meaning |
|---|---|---|
| `TENANTGUARD_ROOT`, `TENANTGUARD_CONFIG`, `TENANTGUARD_LAYOUT` | all Python | where `tenantguard.yaml` and the layout live |
| `TENANTGUARD_WORKER_URLS` | gateway, brain | `w0=http://..,w1=..,spare=..` (overrides yaml `workers`) |
| `TENANTGUARD_GATEWAY_URL`, `TENANTGUARD_BRAIN_URL`, `TENANTGUARD_LAB_URL` | brain, lab, handoff, `tools/demo.py` | peer URLs (default from yaml `services`) |
| `TENANTGUARD_CONTROL_TOKEN` | gateway, brain, lab, handoff, demo | shared secret for `/admin/*`, `/control/*` and brain `/api/operator` (header `X-TenantGuard-Control-Token`; default `dev`) |
| `TENANTGUARD_CORS_ORIGINS` / `TENANTGUARD_UI_ORIGINS` | gateway, brain, handoff / worker, lab | allowed UI origins (default `http://127.0.0.1:5173`) |
| `TENANTGUARD_UPSTREAM_TIMEOUT_S` | gateway | overrides yaml `gateway.upstream_timeout_s` |
| `SERVER_NAME`, `WORKER_DATA_DIR`, `CPU_LIMIT_CORES` | worker | identity, per-tenant SQLite dir, CPU limit used for `cpu_pct` |
| `TENANTGUARD_ADMIN_TOKEN` | worker | `X-Admin-Token` for worker `/admin/fault` (default `dev`) |
| `TENANTGUARD_LAB_PERSONAS`, `TENANTGUARD_LAB_RATE_SCALE`, `TENANTGUARD_LAB_RESULTS` | lab | persona file, global rate multiplier, results dir |
| `WEB_URL`, `TENANTGUARD_UI_URL`, `HANDOFF_STATE_PATH`, `HANDOFF_PARTNER_TOKEN` | handoff | partner app, evidence-link host, case-id store, optional inbound token |
| `VITE_BRAIN_URL`, `VITE_GATEWAY_URL`, `VITE_LAB_URL`, `VITE_CONTROL_TOKEN`, `VITE_MOCK` | UI (`ui/.env`) | backends, token, mock mode |

## Tokens
- **Control token** (`TENANTGUARD_CONTROL_TOKEN`). The gateway requires it on every `/admin/*` and `/control/*` call, and the brain on `/api/operator`; missing or wrong gives 401 (compared in constant time). The brain, lab, UI, handoff bridge and demo driver send it. Set a non-default value anywhere other than a closed demo network.
- **Tenant API keys.** Each tenant's key is in `layouts/ag23.json` (demo keys `tk_A_demo`…). The gateway keeps only their SHA-256.
- **Worker admin token** (`TENANTGUARD_ADMIN_TOKEN`). Only for fault injection on a worker, which is never reachable through the gateway: the gateway's path guard keeps every request under `/api/`.
