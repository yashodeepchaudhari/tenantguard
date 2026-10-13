# 02: Architecture

## 1. System view

```mermaid
flowchart TB
  subgraph TRAFFIC["Traffic (laptop B)"]
    LAB["Traffic lab :8300<br/>12 Poisson personas + attacks"]
    DEMO["tools/demo.py"]
  end
  subgraph DATA["Data plane (laptop A, Docker)"]
    GW["Gateway :8000<br/>identify · admit · queue · dispatch · route · observe"]
    subgraph POOL["Worker pool (0.5 CPU / 256 MB each)"]
      W["w0 .. w8<br/>shared SaaS app"]
      SP["spare (1.0 CPU)"]
      QU["quarantine (0.25 CPU / 128 MB)"]
    end
  end
  subgraph CTRL["Control plane (laptop A)"]
    BR["Brain :8200<br/>Stage 2 ladder · Stage 3 health/rescue/decoder"]
  end
  subgraph MGMT["Management plane"]
    UI["Control Room UI :5173"]
    HO["Handoff bridge :8400"]
    NA["Narrator :8700"]
    WEB["Web PS3 Digital Handoff app<br/>(partner)"]
  end
  LAB -- "X-API-Key" --> GW
  DEMO -- "/admin/mode, /lab/*" --> GW & LAB
  GW -- "X-TenantGuard-Tenant / -Class" --> W & SP & QU
  GW -- "TelemetryFrame WS 250 ms" --> BR
  W -- "/stats 1 Hz" --> BR
  BR -- "/control/* (token, TTL)" --> GW
  BR -- "StateSnapshot + IncidentEvent WS" --> UI & HO & NA
  UI -- "/admin/* (token)" --> GW
  UI -- "/lab/*" --> LAB
  HO -- "cases" --> WEB
  WEB -- "owner / decisions" --> HO
  HO -- "OperatorCmd" --> BR
  HO -- "BlockCmd" --> GW
```

## 2. The three planes

| Plane | Components | Responsibility |
|---|---|---|
| **Data plane** | gateway, `w0..w8`, `spare`, `quarantine` | Serve every tenant request: identify, admit, queue fairly, route by barcode, measure |
| **Control plane** | brain | Watch telemetry and worker stats; rescue victims; name the culprit; throttle with timers; explain every action |
| **Management plane** | admin API, UI, handoff bridge, narrator, lab, demo driver | Let people see and steer it: modes, quotas, cases, reports, attacks, scorecard |

The planes talk only through the contracts in `common/tenantguard_common/contracts.py` (Pydantic models). Every tunable value is in `tenantguard.yaml`, read by `common/tenantguard_common/config.py`.

## 3. Inside the gateway: one request

```mermaid
flowchart LR
  R["request<br/>X-API-Key"] --> ID["1 Identify<br/>SHA-256 key → tenant"]
  ID -->|unknown| E401["401"]
  ID --> PG["path guard + body cap"]
  PG -->|bad path| E400["400"]
  PG --> CL["2 Classify<br/>sha1(method, path, query keys, body bucket)"]
  CL --> RB["3a Retry budget"]
  RB --> AD["3b Admit<br/>block · token bucket · CPU-ms bucket · concurrency"]
  AD -->|refused| E429["429 + Retry-After<br/>+ RateLimit headers"]
  AD --> Q["4 Queue<br/>DRR lanes: express › interactive › batch"]
  Q -->|wait expired / shed| E503["503"]
  Q --> DS["5 Dispatch<br/>per-worker gradient window"]
  DS --> RT["6 Route<br/>barcode server (+ route weights, relocation, drain)"]
  RT --> WK["worker"]
  WK --> OB["7 Observe<br/>debit measured CPU, record latency per (tenant, server)"]
```

Files: `gateway/tenantguard_gateway/core.py` (the request path), `tenants.py`, `security.py`, `retry.py`, `admission.py`, `buckets.py`, `scheduler.py`, `limiter.py`, `router.py`, `overrides.py`, `telemetry.py`, `metrics.py`, `app.py`. See `11_FILES_gateway.md`.

## 4. The five isolation layers (+ the adaptive loop)

```mermaid
flowchart TB
  L1["L1 Request<br/>identity · token bucket · retry budget"] --> L2["L2 Cost<br/>CPU-ms bucket debited with measured CPU"]
  L2 --> L3["L3 Scheduler<br/>weighted DRR in 3 lanes · adaptive per-worker window"]
  L3 --> L4["L4 Kernel<br/>cgroup CPU / memory / pids per worker container"]
  L4 --> L5["L5 Data<br/>SQLite file per tenant · namespaced cache with guaranteed share · memory budget"]
  LOOP["Adaptive loop (brain)<br/>rescue · name · throttle · recover"] -. "TTL-bound control" .-> L1 & L2 & L3
```

| Layer | Stops | Lives in |
|---|---|---|
| L1 Request | Floods and retry storms (429 with an accurate `Retry-After`) | `gateway/.../admission.py`, `buckets.py`, `retry.py` |
| L2 Cost | Few-but-heavy requests, because the bucket is debited with the CPU the worker measured | `gateway/.../buckets.py`, `core.py` |
| L3 Scheduler | Head-of-line blocking: a cheap request never waits behind a whale; per-tenant fairness by weight | `gateway/.../scheduler.py`, `limiter.py` |
| L4 Kernel | One container eating the whole host | `docker-compose.yml` (`cpus`, `mem_limit`, `pids_limit`) |
| L5 Data | Reading another tenant's data; cache thrashing by one tenant | `worker/app/store.py`, `cache.py`, `memory.py`, `tenancy.py` |
| Adaptive loop | Harm that L1–L2 cannot see (hidden background work, a faulty host) | `brain/` |

## 5. The barcode layout (why naming works)

`layouts/ag23.json` places 12 tenants on 9 servers so that **any two tenants share at most one server** (an AG(2,3) design). Each tenant's 3 servers are its "barcode":

| Tenant | Servers | Tenant | Servers | Tenant | Servers |
|---|---|---|---|---|---|
| A | w0 w1 w2 | E | w1 w4 w7 | I | w2 w3 w7 |
| B | w3 w4 w5 | F | w2 w5 w8 | J | w0 w5 w7 |
| C | w6 w7 w8 | G | w0 w4 w8 | K | w1 w3 w8 |
| D | w0 w3 w6 | H | w1 w5 w6 | L | w2 w4 w6 |

If the slow servers are exactly w1, w3 and w8, only one tenant's barcode covers them: K. If one server is slow on its own, no tenant's barcode explains it, so it is a host fault and no tenant is blamed. The brain's decoder (`brain/tenantguard_brain/decoder.py`) turns this into a confidence. Victims are rescued first: the gateway is told to route their traffic to their other, healthy servers (`rescue.py`).

## 6. Control loop

```mermaid
flowchart LR
  T["Telemetry 250 ms<br/>+ worker stats 1 Hz"] --> S2["Stage 2<br/>noisy score = usage share ÷ fair share × victim impact"]
  T --> S3["Stage 3<br/>server health by peer ratio"]
  S3 --> RES["Rescue<br/>RouteWeightsCmd (TTL)"]
  S3 --> DEC["Decoder<br/>name culprit or host fault"]
  S2 --> LAD["Throttle ladder<br/>warn → throttle → deprioritize → quarantine"]
  LAD --> LIM["LimitsCmd (TTL)"]
  RES & LIM --> GW["Gateway applies, auto-reverts at TTL"]
  DEC & LAD --> EV["IncidentEvent stream<br/>UI · handoff · narrator"]
  OP["Operator decision<br/>(handoff case)"] --> HOLD["OperatorCmd<br/>extend / release / resume"] --> LAD
```

The ladder acts only when harm persists after the rescue. Every action carries a TTL and is renewed only while it is still needed. If the brain stops sending heartbeats, the gateway keeps its last configuration (fail-static) while TTLs still expire. Relocation proof and the five verdicts are **in progress** (T12).

## 7. Deployment: two laptops over Tailscale

```mermaid
flowchart LR
  subgraph A["Laptop A: Docker host"]
    direction TB
    CA["docker compose (project codeastra)<br/>gateway · w0..w8 · spare · quarantine · brain<br/>(+ profiles: handoff, narrator, billing, lab)"]
  end
  subgraph B["Laptop B: dev + traffic"]
    direction TB
    LB["lab :8300"]
    UB["UI :5173 (Vite)"]
    DB["tools/demo.py"]
  end
  B <-- "Tailscale 100.x.y.z<br/>(LAN IP as fallback)" --> A
```

- **Why two laptops:** 11 worker containers at 0.5 CPU each, plus the brain, need about 8 cores to themselves. Generating traffic on the same machine would distort the latencies being measured.
- Laptop B drives laptop A's Docker over a single SSH session: `git archive` of `master` is shipped, then `docker compose up -d --build` runs on the host. Details: `docs/SETUP.md`, `docs/DOCKER-HOST-SETUP.md`.
- Host ports are configurable (`GATEWAY_PORT`, `BRAIN_PORT`, `LAB_PORT`, `HANDOFF_PORT`, `NARRATOR_PORT`, `BILLING_PORT`) and so is the project name (`COMPOSE_PROJECT_NAME`), so a frozen demo stack can run beside a development stack.

## 8. Components and ports

| Component | Path | Port | Exposed by compose | Built with |
|---|---|---|---|---|
| Gateway | `gateway/` | 8000 | yes (`GATEWAY_PORT`) | Python 3.12, FastAPI, httpx, uvicorn |
| Workers `w0..w8` | `worker/` | 8100 in compose (local 8100–8108) | no | Python, FastAPI (sync handlers), SQLite, psutil |
| `spare` / `quarantine` | `worker/` | 8100 in compose (local 8109 / 8110) | no | same image; spare 1.0 CPU, quarantine 0.25 CPU / 128 MB |
| Brain | `brain/` | 8200 | yes (`BRAIN_PORT`) | Python asyncio, websockets, httpx, Pydantic; Docker socket mounted |
| Lab | `lab/` | 8300 | yes, profile `lab` | Python asyncio + httpx |
| Handoff bridge | `integrations/handoff/` | 8400 | yes, profile `handoff` | Python, FastAPI, websockets |
| Billing | `billing/` | 8600 | profile `billing` (code **in progress**, T17) | n/a |
| Narrator | `narrator/` | 8700 | yes, profile `narrator` | Python, FastAPI, websockets |
| UI | `ui/` | 5173 | runs outside Docker | React 18, Vite, TypeScript, Tailwind, Recharts |
| Web PS3 app (partner) | n/a | `WEB_URL` (mock: 8500) | n/a | `integrations/handoff/tools/mock_web.py` for demos |

## 9. Cross-cutting rules

- **One schema:** every cross-service message is a model in `common/tenantguard_common/contracts.py`.
- **One config:** every tunable is in `tenantguard.yaml`; peer URLs come from yaml or env and never assume `127.0.0.1`.
- **Every service binds `0.0.0.0`** and allows CORS only from the UI origin.
- **Security:** a shared control token on all `/admin/*` and `/control/*` endpoints and on the brain's `/api/operator`. Tenants are identified only by API key at the gateway; workers trust only gateway-set headers.
- **Safety:** every control action is TTL-bound; the gateway is fail-static; control input is validated to 400 and never crashes a service.
