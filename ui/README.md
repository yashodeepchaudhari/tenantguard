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
| brain `GET /api/incidents` | timeline backfill on load (array or `{incidents: [...]}`) |
| gateway `/admin/mode`, `/admin/tenants`, `PATCH /admin/tenants/{id}/quota`, `/admin/overrides` (`VITE_GATEWAY_URL`) | mode toggle, Tenants page, quota edit, override expiry and fail-static |
| lab `/lab/status`, `POST /lab/attack`, `DELETE /lab/attack/{id}` (`VITE_LAB_URL`) | attack launcher |

`src/types.ts` mirrors `common/tenantguard_common/contracts.py` exactly. The services must allow CORS from the UI origin.

## Pages
- **Control Room** (`/`): status sentence; 12 tenant tiles (p95, rps, rejected/s, level, quota factor); p95 per tenant over the last 60 s (log scale); stacked CPU share; noisy-score bars (`noisy_norm` bar, raw score as text, warn/throttle markers); incident timeline; Jain fairness and fleet CPU gauges; OFF / Industry / TenantGuard toggle; attack launcher (attack, tenant or server, intensity 0.1 to 5, duration up to 600 s) with running attacks and Stop.
- **Tenants** (`/tenants`): configured quota vs effective quota (after the brain's factor), weight factor, blocked, placement, override expiry, revenue, live p95/level; inline quota edit (PATCH sends changed fields only; validated client-side).

Design rules: colour means status only (green / amber / red; grey = idle) plus one accent (sky) for interactive and selected things. Every number has a unit. Laid out for a 1920×1080 projector on one screen.

Tile colour: red if the tenant is throttled or above, or p95 ≥ `VITE_P95_BAD_MS`; amber if under watch (warn) or p95 ≥ `VITE_P95_WARN_MS`.

## Mock
- `src/mock/sim.ts`: a small simulation of gateway + workers + brain + lab that emits contract-shaped messages and reacts to mode, attacks and quota edits (OFF: victims about 1.3 s; Industry: about 220 ms; TenantGuard: ladder warn → throttle → deprioritize → quarantine, then recovery).
- `src/mock/mockBackend.ts`: runs it in the browser (`VITE_MOCK=1`).
- `scripts/mock-ws-server.mjs`: the same sim behind real HTTP + WS endpoints with CORS (brain :18200, gateway :18000, lab :18300), to exercise the real network code: `npm run mock-ws`, then run the UI with `VITE_MOCK=0 VITE_BRAIN_URL=http://127.0.0.1:18200 VITE_GATEWAY_URL=http://127.0.0.1:18000 VITE_LAB_URL=http://127.0.0.1:18300`. Requires Node ≥ 23.6 (runs the .ts directly).

## Checks
```
npm run build      # tsc -b + vite build
npm run lint       # eslint, zero warnings
npm test           # vitest: status rules, store, quota patch, backend parsing, mock ladder
node scripts/e2e.mjs [ui] [gateway] [lab]          # clicks mode / attack / stop / quota edit, verifies via the backend APIs
node scripts/screenshot.mjs [url] [out.png] [ms]   # headless Edge 1920x1080
```
