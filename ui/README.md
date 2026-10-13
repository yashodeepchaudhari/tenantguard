# TenantGuard UI (Control Room)

React 18 + Vite + TypeScript + Tailwind + Recharts. Port **5173** (binds 0.0.0.0).

```
cd ui
npm install
cp .env.example .env      # set the Docker laptop's IP for brain + gateway
npm run dev               # real backends from .env
npm run dev:mock          # fixture replay in the browser, no backend (VITE_MOCK=1)
```

`?mock=1` / `?mock=0` in the URL overrides `VITE_MOCK`; `?script=0` disables the scripted whale-on-K in mock mode.

## Data
| Source | Used for |
|---|---|
| brain `WS /ws/events` (`VITE_BRAIN_URL`) | `StateSnapshot` (2 Hz) + `IncidentEvent`; reconnects with backoff (0.5 s to 8 s) |
| brain `GET /api/incidents`, `?incident_id=` | timeline backfill on load; Case File history (array or `{incidents: [...]}`) |
| gateway `/admin/mode`, `/admin/tenants`, `/admin/overrides` (`VITE_GATEWAY_URL`) | mode toggle, Tenants page (project, replicas, tier quota), limits / pins / blocks and their expiry, fail-static (snapshot age) |
| brain `GET /api/pool`, `PUT /admin/pool`, `POST/PATCH/DELETE /admin/projects`, `POST /admin/projects/{id}/tenants`, `DELETE /admin/tenants/{id}` (`VITE_BRAIN_URL`, control token) | Pool page: the pool, projects, tenants, replicas; the admin forms |
| lab `/lab/status`, `POST /lab/attack`, `DELETE /lab/attack/{id}`, `GET /lab/results` (`VITE_LAB_URL`) | attack launcher, Scorecard (`RunResult` list) |
| handoff bridge `GET /cases` (`VITE_HANDOFF_URL`, default :8400) | Cases column + Case File handoff panel (`CaseView`: case id, owner, pending actions, deadlines); hidden as "not reachable" when the bridge is down |

Every gateway and brain `/admin/*` call sends `X-TenantGuard-Control-Token: $VITE_CONTROL_TOKEN` (default `dev`; must equal the gateway's `TENANTGUARD_CONTROL_TOKEN`). Noisy-bar thresholds come from `StateSnapshot.noisy_thresholds`; `VITE_NOISY_*` are only a fallback.

`src/types.ts` mirrors `common/tenantguard_common/contracts.py` exactly. The services must allow CORS from the UI origin.

## Pages
- **Control Room** (`/`):
  - **Status row:** the status sentence, the ₹ impact panel, the Jain fairness and pool-CPU gauges, and the OFF / Industry / TenantGuard toggle.
  - **Tenant tiles:** 12 tiles showing p95, rps, rejected/s, level, quota factor and project. Clicking a tile outlines its project.
  - **Charts:** p95 per tenant over the last 60 s (log scale), stacked CPU share, noisy-score bars, and the incident timeline.
  - **Replica grid:** one row per project with its tier, allocation (base + lent cores), floor marker and state. Each replica shows its status, CPU against its cap, memory and `LOAN` if borrowed. Starting or draining replicas are hatched, and split-test pins are named.
  - **Flow view** (Tiles / Flow toggle, or `?view=flow`): dots travel tenant → gateway → that tenant's project's ready replicas. Refused requests die at the gateway as red crosses. A split test draws accent pin lines, and lent capacity is named in the hint line.
  - **Attack launcher.**
- **Pool** (`/pool`): the v2 admin screen.
  - **Two bars:** guaranteed floors (each project's floor, free room, headroom) and allocated now (base solid, lent cores striped), with measured use as a white line.
  - **Project table:** tier and weight, floor, cap, warm replicas, demand, used, allocated = base + lent, replicas (cores, MB, loan), tenants (× removes one), Edit / Delete.
  - **Forms:** set the pool, create a project (a floor above the free cores is refused before sending), add a tenant. The API key is shown once.
- **Cases** (`/case`) and **Case File** (`/case/:incident_id`):
  - **Case story:** steps, suspects with confidence, verdict, last action and override TTL, the latest detail, the report as sections, and the full event log.
  - **Hash split:** for an unattributed-work verdict, the split rounds: the two halves, the unexplained CPU on each replica, and what was kept.
  - **Live replicas:** the case's project replicas, live.
- **Scorecard** (`/scorecard`): `RunResult`s grouped by scenario × mode.
- **Tenants** (`/tenants`): project and tier, replicas in the tenant's rendezvous order (and its pin during a split test), live p95 and level, the tier quota against the effective quota after the brain's factor, override expiry, revenue. Quotas come from the tier; tenants are added and removed on the Pool page.

Design rules:
- Colour means status only (green / amber / red; grey = idle), plus one accent (sky) for interactive, selected and lent things.
- Every number has a unit.
- Laid out for a 1920×1080 projector on one screen.

## Mock
- `src/mock/sim.ts`: a small simulation of the v2 platform. It emits contract-shaped messages and reacts to mode, attacks and admin changes:
  - floors first, then idle CPU lent by tier weight;
  - replicas that scale (starting → ready, draining → gone, borrowed ones marked);
  - a tenant only reaches its own project's replicas;
  - the ladder judges a tenant against its project peers.
- `src/mock/mockBackend.ts`: runs it in the browser (`VITE_MOCK=1`).
- `scripts/mock-ws-server.mjs`: the same sim behind real HTTP + WS endpoints with CORS and the control-token check (brain :18200, gateway :18000, lab :18300), to exercise the real network code: `npm run mock-ws`, then run the UI with `VITE_MOCK=0 VITE_BRAIN_URL=http://127.0.0.1:18200 VITE_GATEWAY_URL=http://127.0.0.1:18000 VITE_LAB_URL=http://127.0.0.1:18300`. Requires Node ≥ 23.6 (runs the .ts directly).

## Checks
```
npm run build      # tsc -b + vite build
npm run lint       # eslint, zero warnings
npm test           # vitest: status rules, store, quota patch, backend parsing, case folding, scorecard, mock ladder
python scripts/load.py --rps 1 --whale K           # modest calm traffic + a whale through the gateway (real-stack checks without the lab)
node scripts/screenshot.mjs [url] [out.png] [ms] [click]  # headless Edge 1920x1080 (real-time wait)
```

The e2e changes mode, launches/stops an attack and edits (then restores) a quota. Point it only at a stack you own; it skips those writes while the lab reports a scenario run in progress.

## ₹ impact (client-side until the brain owns it)
Counted since the page opened (reset button under "innocents"), integrated per snapshot (dt capped at 2 s):
- a tenant is **threatened** when it shares a project (its replicas) with a flagged tenant (level ≥ warn), or its project has a slow replica whose p95 would breach the SLO; culprits (own score ≥ warn, or named by a brain incident) never count as customers;
- **SLO breach avoided** = seconds a threatened tenant stayed within SLO (`p95 < VITE_P95_WARN_MS`) in TenantGuard mode; **suffered** = seconds any tenant was over SLO (any mode);
- **revenue protected** = Σ avoided seconds × that tenant's `monthly_revenue_rs` ÷ 30 days; **customers shielded** = tenants with avoided time;
- **innocents penalised** = tenants acted on (level ≥ throttle or factor < 1) that were never noisy this session and that no brain incident names. A culprit still recovering on the ladder is not an innocent. It must stay 0.

Proposed contract field so the brain (which has the calm baselines and the real victim set) owns these numbers:

```python
class ImpactView(BaseModel):
    since: float                        # epoch s when counting started (brain start or reset)
    revenue_protected_rs: float         # Σ victims' monthly_revenue_rs × SLO-violation seconds avoided / 2_592_000
    customers_shielded: List[str]       # threatened tenants that stayed within SLO
    slo_violation_avoided_s: float
    slo_violation_suffered_s: float
    innocents_penalised: List[str]      # acted on while their own score < warn; must stay empty

# StateSnapshot: impact: Optional[ImpactView] = None   (the UI keeps its own meter as a fallback)
```

## End-to-end suite (Playwright, `ui/e2e/`)
One command proves the running system from a user's point of view: system Edge (`channel: "msedge"`), headless, one worker.

```
npm run e2e                          # mock stack: starts scripts/mock-ws-server.mjs + Vite on :5174 itself
E2E_UI_URL=http://127.0.0.1:5174 E2E_GATEWAY_URL=http://<host>:8000 E2E_BRAIN_URL=http://<host>:8200 E2E_LAB_URL=http://<host>:8300 E2E_HANDOFF_URL=http://<host>:8400 E2E_NARRATOR_URL=http://<host>:8700 E2E_CONTROL_TOKEN=dev npm run e2e   # a real stack (start a Vite pointed at it first)
E2E_LIVE=1 ...                       # also the @live scenario: calm -> whale on K -> incident <= 60 s -> stop -> recovery <= 90 s
```

| Spec | Covers |
|---|---|
| `e2e/api.spec.ts` | gateway healthz, 401 without API key, 401 on admin without token, 200 + `X-TenantGuard-Cpu-Ms`, 12 admin tenants each in one project; brain `/api/state` shape (replicas with a project, pool), `/api/pool` invariants, brain admin 401 without token, `/api/incidents`; lab, handoff and narrator shapes (skipped cleanly when unreachable) |
| `e2e/ui.spec.ts` | 12 tiles + replica grid (one row per project) + status sentence; tile → its project outlined; mode toggle round-trip (verified via `GET /admin/mode`, restored); launcher offers exactly flood, whale, hog, memory, retry_storm; attack launch → running → Stop (verified via `/lab/status`); Pool page: create a project, an oversized floor refused, add a tenant, key shown once (verified via `GET /api/pool`, removed afterwards); timeline → Case File vs `GET /api/incidents?incident_id=`; Scorecard vs `/lab/results`; flow view paints; Cases handoff column |
| `e2e/live.spec.ts` | `@live`, host only: the real detection loop end to end |

Every UI test fails on an uncaught page error or a console error (`e2e/fixtures.ts`); only failed loads to an optional service that is known to be down are tolerated. Writes are skipped while the lab reports a scenario run in progress, and anything changed is restored.

