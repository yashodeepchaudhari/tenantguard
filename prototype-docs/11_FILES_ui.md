# 11 · Files: ui

The UI is the operator's **Control Room**: a React 18 + Vite + TypeScript + Tailwind + Recharts single-page app on port **5173** (binds `0.0.0.0`). It shows every tenant's health live, who is noisy and what TenantGuard is doing about it, and lets the operator switch modes, launch attacks and edit quotas.

**How the pieces fit**

```
main.tsx → App.tsx (router)
             └─ LiveProvider (lib/live.tsx)  ── owns one Backend + one LiveStore
                  ├─ Backend: realBackend (lib/backend.ts)  or  createMockBackend (mock/mockBackend.ts → mock/sim.ts)
                  │     brain  WS /ws/events, GET /api/incidents        gateway /admin/* (control token)
                  │     lab    /lab/status, /lab/attack, /lab/results   bridge  GET /cases
                  └─ LiveStore (lib/store.ts) ── snapshot, 60 s history, incidents, admin data, overrides, lab status, ₹ impact
             pages/ (ControlRoom, Tenants, Cases, CaseFile, Scorecard) read the store through hooks (lib/liveContext.ts)
             and render components/; all colour and threshold rules live in lib/status.ts and lib/thresholds.ts;
             all pure logic (case folding, scorecard, flow routing, impact, headline, tile reason, scenario badge)
             lives in lib/*.ts with unit tests.
```

**Design rules implemented everywhere**: colour means status only (green / amber / red, grey = no data) plus one accent (sky) for interactive and selected things; every number carries a unit (`lib/format.ts`); the Control Room fits one 1920×1080 projector screen; `p95_ms == 0` means "nothing completed" and is shown as *no traffic*, never as fast.

**Data sources** (base URLs from `ui/.env`, see `.env.example`):

| Source | Calls | Used by |
|---|---|---|
| brain `VITE_BRAIN_URL` (:8200) | `WS /ws/events` (StateSnapshot 2 Hz + IncidentEvent), `GET /api/incidents`, `GET /api/incidents?incident_id=` | every page; Case File |
| gateway `VITE_GATEWAY_URL` (:8000) | `GET/POST /admin/mode`, `GET /admin/tenants`, `PATCH /admin/tenants/{id}/quota`, `GET /admin/overrides`, all with `X-TenantGuard-Control-Token: VITE_CONTROL_TOKEN` | mode toggle, Tenants page, grid/flow/impact (via LiveProvider poller), Case File TTL |
| lab `VITE_LAB_URL` (:8300) | `GET /lab/status` (polled every 2 s by `LiveProvider`), `POST /lab/attack`, `DELETE /lab/attack/{id}`, `GET /lab/results` | scenario badge, headline, flow-view reroute hint, attack launcher, Scorecard |
| handoff bridge `VITE_HANDOFF_URL` (:8400) | `GET /cases` (with the control token) | Cases column, Case File handoff panel |

Not consumed by the UI on this branch: `StateSnapshot.impact` (brain-owned `ImpactView` in contracts; `ui/src/types.ts` does not mirror it, so the panel uses the client-side meter) and the narrator's `GET /reports` / `WS /ws/reports` (`ReportView`). The Case File shows the brain's report or a template placeholder. The Playwright API spec does check the narrator's `GET /reports` shape when the narrator answers.

---

## Project and build files

### ui/package.json
Package manifest (`tenantguard-ui`, ES modules). Pins exact versions: React 18.3.1, react-router-dom 6.26.2, Recharts 2.13.3; dev: Vite 5.4.8, TypeScript 5.6.2, ESLint 9 + typescript-eslint 8, Tailwind 3.4.13, Vitest 2.1.2, `@playwright/test` 1.47.2 (e2e suite), `ws` 8.18.0 (mock server). `puppeteer-core` is the one range (`^23.4.0`), used by `scripts/e2e.mjs` and `scripts/screenshot.mjs` against the installed Edge.

| Script | Command | What it does |
|---|---|---|
| `dev` | `vite` | dev server with real backends from `.env` |
| `dev:mock` | `vite --mode mock` | dev server with the in-browser simulation (`.env.mock` sets `VITE_MOCK=1`) |
| `build` | `tsc -b && vite build` | type-check both TS projects, then bundle to `dist/` |
| `build:mock` | `tsc -b && vite build --mode mock` | same, mock mode |
| `lint` | `eslint . --max-warnings 0` | lint; any warning fails |
| `test` | `vitest run` | unit tests (`src/**/*.test.ts`) |
| `preview` | `vite preview` | serve `dist/` on 5173 |
| `mock-ws` | `node scripts/mock-ws-server.mjs` | stand-in brain/gateway/lab/bridge over real HTTP + WS |
| `screenshot` | `node scripts/screenshot.mjs` | headless Edge screenshot |
| `e2e` | `playwright test` | Playwright suite in `e2e/` (starts the mock stack itself unless `E2E_UI_URL` is set) |

**Key decisions**
- Exact version pins (only `puppeteer-core` uses a caret range) so the build is reproducible offline.
- `puppeteer-core` and Playwright (`channel: 'msedge'`) both drive the already-installed Microsoft Edge; no browser download.

### ui/vite.config.ts
Vite + React plugin config. Server and preview bind `0.0.0.0:5173` with `strictPort` (the UI runs on one laptop while the stack runs on another). Rollup `manualChunks` splits `recharts` and `react`/`react-dom`/`react-router-dom` into their own chunks so no chunk exceeds 500 kB. Vitest runs `src/**/*.test.ts` in the `node` environment.

| Key | Value | Why |
|---|---|---|
| `server` / `preview` | host `0.0.0.0`, port 5173, strictPort | reachable from the projector laptop; fail instead of silently moving port |
| `build.rollupOptions.output.manualChunks` | `recharts`, `react` | chunk size |
| `test` | env `node`, include `src/**/*.test.ts` | pure-logic tests, no DOM |

### ui/tsconfig.json, ui/tsconfig.app.json, ui/tsconfig.node.json
Project references: `tsconfig.app.json` type-checks `src/` (ES2020, DOM, `jsx: react-jsx`, `strict`, `noUnusedLocals`, `noUnusedParameters`, `allowImportingTsExtensions`, bundler resolution, `noEmit`); `tsconfig.node.json` type-checks `vite.config.ts`, `playwright.config.ts` and `e2e/` with Node types (libs ES2023 + DOM, because the e2e specs run code inside the page). Imports use explicit `.ts`/`.tsx` extensions so the same modules (`mock/sim.ts`, `mock/handoff.ts`, `mock/results.ts`, `lib/case.ts`) also run directly in Node (type stripping) inside `scripts/`.

### ui/eslint.config.js
Flat ESLint config: `@eslint/js` recommended + `typescript-eslint` recommended for `**/*.{ts,tsx}` with browser globals, `react-hooks` rules and `react-refresh/only-export-components` (warn; constants allowed). `scripts/**/*.mjs` get the JS recommended rules with Node globals. Ignores `dist` and `node_modules`. `npm run lint` uses `--max-warnings 0`, which is why helper functions live in `lib/` rather than in component files.

### ui/tailwind.config.js
Tailwind content globs (`index.html`, `src/**/*.{ts,tsx}`) and the design tokens: `ok` (#22c55e), `warn` (#f59e0b), `bad` (#ef4444), `accent` (#38bdf8) each with a `dim` shade, the `ink` grey scale (950 → 100) used for backgrounds and text, and the font stacks (Segoe UI / Inter; Cascadia Mono / Consolas).

**Key decisions**
- Only three status colours and one accent exist as named tokens, which makes "colour = status" enforceable in review.

### ui/postcss.config.js
PostCSS pipeline: `tailwindcss` then `autoprefixer`.

### ui/index.html
HTML shell: `<title>TenantGuard Control Room</title>`, favicon, dark `body` classes, `#root` mount point, loads `/src/main.tsx` as a module.

### ui/public/favicon.svg
3×3 grid icon (eight green cells, one red, one amber): the fleet with one noisy server.

### ui/.env.example
Template for `ui/.env` (which is git-ignored). Keys: `VITE_BRAIN_URL`, `VITE_GATEWAY_URL`, `VITE_LAB_URL`, `VITE_HANDOFF_URL`, `VITE_CONTROL_TOKEN` (default `dev`, must equal the gateway's `TENANTGUARD_CONTROL_TOKEN`), `VITE_MOCK` (1 = in-browser simulation), `VITE_P95_WARN_MS` / `VITE_P95_BAD_MS` (tile thresholds, 150 / 400 ms), `VITE_NOISY_WARN` / `_THROTTLE` / `_QUARANTINE` (fallback only; the brain's `StateSnapshot.noisy_thresholds` wins).

### ui/.env.mock
Loaded by `vite --mode mock`; sets `VITE_MOCK=1`.

### ui/.gitignore
Ignores the Playwright output folders `e2e-results/` and `playwright-report/`.

### ui/README.md
How to run (`npm run dev`, `dev:mock`), the data-source table, every page and component in prose, the design rules, the mock tooling, the checks (`build`, `lint`, `test`, `e2e.mjs`, `screenshot.mjs`, `load.py`), the client-side ₹ impact definitions, the `ImpactView` contract proposal (since adopted into contracts), and the Playwright end-to-end suite (how to run it against the mock and a real stack, one row per spec).

---

## Application entry and shared definitions

### ui/src/main.tsx
Mounts `<App />` inside `React.StrictMode` into `#root` and imports `index.css`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| (module) | `createRoot(document.getElementById('root')!).render(...)` | bootstraps React | `index.html` script tag |

### ui/src/App.tsx
Top-level component: wraps everything in `LiveProvider`, then a `BrowserRouter` with one layout route and five pages.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `App` (default export) | `App(): JSX.Element` | routes: `/` → `ControlRoom`, `/tenants` → `Tenants`, `/case` → `Cases`, `/case/:incidentId` → `CaseFile`, `/scorecard` → `Scorecard`, `*` → redirect to `/`; all under `Layout` | `main.tsx` |

**Key decisions**
- `/case/:incident_id` is the exact path the handoff bridge puts in `CaseContext.evidence_link`, so a case link from the partner's Web app opens the Case File directly.
- `LiveProvider` sits above the router so the live stream and the impact meter survive page navigation.

### ui/src/config.ts
Reads `import.meta.env` once and exposes a typed `config` object; trims trailing slashes from URLs; numeric env values fall back to defaults when missing or invalid; the URL query `?mock=1` / `?mock=0` overrides `VITE_MOCK`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `trim` (private) | `(u: string \| undefined, fallback: string) => string` | non-empty URL or fallback, without trailing `/` | `config` |
| `num` (private) | `(v: string \| undefined, fallback: number) => number` | positive finite number or fallback | `config` |
| `urlMock` (private const) | `string \| null` | value of `?mock=` | `config.mock` |
| `config` | `{ brainUrl, gatewayUrl, labUrl, handoffUrl, controlToken, mock, p95WarnMs, p95BadMs, historyS, noisy: { warn, throttle, quarantine } }` | runtime configuration (`historyS` = 60) | `lib/backend.ts`, `lib/live.tsx`, `lib/thresholds.ts`, components, pages |
| `wsUrl` | `(httpUrl: string, path: string) => string` | `http(s)://` → `ws(s)://` + path | `realBackend.subscribe` |

**Key decisions**
- Peer services run on another laptop, so `127.0.0.1` appears only as a development default; every URL comes from `.env`.

### ui/src/vite-env.d.ts
Ambient types for `import.meta.env`: declares every `VITE_*` key used by `config.ts` as an optional string.

### ui/src/types.ts
TypeScript mirror of `common/tenantguard_common/contracts.py`, kept field-for-field with the same units comments (epoch seconds for `ts`/`*_at`, milliseconds for `*_ms`, 0..1 fractions). Also holds UI-side constant lists derived from the contract literals.

| Type / const | Kind | What it mirrors / holds | Used by |
|---|---|---|---|
| `Mode`, `Tier`, `Level` | union types | contract literals (`off \| industry \| tenantguard`, …) | everywhere |
| `MODES`, `LEVELS` | `const` arrays | ordered literal values | `ModeToggle`, `mock/sim.ts` (escalation rank) |
| `Quota`, `TenantAdminView`, `QuotaPatch`, `ModeBody` | interfaces | gateway admin models | `Tenants`, `lib/quota.ts`, `realBackend` |
| `ControlBase`, `LimitsCmd`, `BlockCmd`, `RelocateCmd`, `DrainCmd`, `OverridesView` | interfaces | control models + `GET /admin/overrides` | `lib/flow.ts`, `CaseFile`, `Tenants`, store |
| `TenantView`, `ServerView`, `StateSnapshot` (incl. optional `noisy_thresholds`) | interfaces | brain → UI snapshot | store, all pages |
| `Stage`, `Suspect`, `IncidentEvent`, `BrainMessage` | types | incident stream | store, `lib/case.ts`, timeline |
| `AttackName`, `ATTACKS`, `INTENSITY_MIN` (0.1), `INTENSITY_MAX` (5), `DURATION_MAX_S` (600) | types / consts | lab attack names (all ten contract literals) and the 422 bounds | `AttackLauncher`, `mock/sim.ts` |
| `LAUNCHABLE_ATTACKS` | `const` array | the ten attacks the operator can launch, in menu order: `flood`, `whale`, `hog`, `memory`, `retry_storm`, `blackbox_bomb`, `background_bomb`, `cache_flood`, `honest_spike`, `faulty_server` (every contract name). `faulty_server` targets a server, not a tenant | `AttackLauncher` |
| `AttackReq`, `AttackStatus`, `LabStatus`, `RunReq`, `PhaseP99`, `RunResult` | interfaces | lab models | launcher, Scorecard |
| `Verdict`, `Severity`, `CaseStatus`, `CaseDecision`, `ActionState`, `CaseContext`, `CaseAction`, `CaseHistoryItem`, `CaseView`, `OperatorCmd` | types | handoff (contracts §6) | `useHandoff`, Cases, Case File, `mock/handoff.ts` |

**Key decisions**
- A single mirror file makes contract drift reviewable in one diff. `StateSnapshot.impact` / `ImpactView` and `ReportView` are not mirrored, because no page reads them.

### ui/src/index.css
Tailwind layers plus base styles (`color-scheme: dark`, tabular numerals for aligned numbers) and five component classes used across pages: `.panel`, `.panel-title`, `.btn` / `.btn-accent` / `.btn-ghost`, `.field` (inputs), `.unit` (small unit suffix next to a big number).

---

## Data layer (`src/lib`)

### ui/src/lib/backend.ts
The one interface for every backend call the UI makes, the real HTTP/WS implementation, and pure parsers for backend payloads. All HTTP goes through `http()` with a 5 s timeout and readable error messages; every gateway `/admin/*` (and bridge) call carries the control token.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `ConnState` | `'connecting' \| 'open' \| 'closed'` | stream connection state | store, `Layout.Connection` |
| `Backend` | interface: `kind`, `subscribe`, `incidents`, `incident`, `results`, `handoffCases`, `getMode`, `setMode`, `tenants`, `patchQuota`, `overrides`, `labStatus`, `startAttack`, `stopAttack` | contract between UI and any backend | `realBackend`, `createMockBackend`, hooks |
| `ApiError` | `class ApiError extends Error { status: number; constructor(status, message) }` | HTTP error with status (0 = unreachable) | `http`, `mockBackend.call` |
| `errorText` | `(body: unknown, fallback: string) => string` | one readable line from a FastAPI error body: string `detail`, list of strings, or list of `{loc, msg}` | `http` |
| `H_CONTROL_TOKEN` | `'X-TenantGuard-Control-Token'` | header name (contracts `H_CONTROL_TOKEN`) | `admin` |
| `admin` (private) | `(init?: RequestInit) => RequestInit` | adds the control-token header | gateway + bridge calls in `realBackend` |
| `http` (private) | `<T>(base: string, path: string, init?: RequestInit) => Promise<T>` | fetch with JSON headers, 5 s `AbortSignal.timeout`, JSON/text body parse, throws `ApiError` | every `realBackend` method |
| `json` (private) | `(method: string, data: unknown) => RequestInit` | JSON body request init | `setMode`, `patchQuota`, `startAttack` |
| `incidentList` | `(body: unknown) => IncidentEvent[]` | accepts a bare list or `{incidents: [...]}`, keeps `type === 'incident'` | `realBackend.incidents/incident` |
| `resultList` | `(body: unknown) => RunResult[]` | accepts a list or `{results: [...]}`, drops entries without `run_id`/`scenario`/`victim_p99_ms` | `realBackend.results` |
| `caseViewList` | `(body: unknown) => CaseView[]` | accepts a list or `{cases: [...]}`, requires `case_id` + `incident_id`, defaults `actions`/`history` to `[]` | `realBackend.handoffCases` |
| `parseBrainMessage` | `(raw: string) => BrainMessage \| null` | parses a WS frame; only `type` `state` / `incident` | `realBackend.subscribe` |
| `realBackend.subscribe` | `(onMessage, onConn) => () => void` | opens `WS /ws/events`; on close reconnects with backoff 0.5 s → 8 s (doubling, capped); returns an unsubscribe that stops reconnecting | `LiveProvider` |
| `realBackend.incidents` | `() => Promise<IncidentEvent[]>` | `GET /api/incidents`; `[]` on any error (backfill is optional) | `LiveProvider`, `Cases` |
| `realBackend.incident` | `(id: string) => Promise<IncidentEvent[]>` | `GET /api/incidents?incident_id=`; filters by id again and sorts oldest first | `CaseFile` |
| `realBackend.results` | `() => Promise<RunResult[]>` | `GET /lab/results` | `Scorecard` |
| `realBackend.handoffCases` | `() => Promise<CaseView[]>` | `GET {handoff}/cases` with token | `useHandoff` |
| `realBackend.getMode` / `setMode` | `() => Promise<Mode>` / `(mode) => Promise<Mode>` | `GET` / `POST /admin/mode` | `ModeToggle` |
| `realBackend.tenants` | `() => Promise<TenantAdminView[]>` | `GET /admin/tenants` | `LiveProvider` poller, `Tenants` |
| `realBackend.patchQuota` | `(id, patch) => Promise<TenantAdminView>` | `PATCH /admin/tenants/{id}/quota` | `Tenants.EditRow` |
| `realBackend.overrides` | `() => Promise<OverridesView>` | `GET /admin/overrides` | `LiveProvider` poller, `Tenants`, `CaseFile` |
| `realBackend.labStatus` / `startAttack` / `stopAttack` | lab calls | `GET /lab/status`, `POST /lab/attack`, `DELETE /lab/attack/{id}` | `AttackLauncher` |

**Key decisions**
- Parsers tolerate both a bare list and a wrapped object, so the UI did not have to wait for each service's exact response shape.
- `incidents()` swallows errors (backfill is a nicety); every other call surfaces a readable error to the operator.

### ui/src/lib/store.ts
Framework-free live state. Applies brain messages, keeps a rolling 60 s chart history, de-duplicates incidents (WS + REST backfill), holds the gateway admin data and overrides from the poller, and integrates the ₹ impact meter on every snapshot.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `LAYOUT_BARCODES`, `LAYOUT_REVENUE` (private) | `Record<string, string[]>`, `Record<string, number>` | ag23 layout fallback (from `mock/fixture.ts`) until `/admin/tenants` answers | `LiveStore` constructor |
| `HistoryPoint` | `{ ts, p95: Record<tenant, ms>, cpu: Record<tenant, 0..1> }` | one chart sample | `Charts` |
| `LiveState` | interface: `conn, snapshot, receivedAt, history, incidents, pendingMode, admin, overrides, barcodes, revenue, impact, lab, labReachable` | everything pages render; `lab` = latest `GET /lab/status` (null until it answers or while unreachable), `labReachable` = null (not asked yet) / false (lab did not answer) | hooks |
| `StoreOptions` | `{ historyS?, sloMs?, warnFallback? }` | constructor options | `LiveProvider` |
| `MAX_INCIDENTS` (private) | `300` | incident list cap | `addIncidents` |
| `eventKey` (private) | `(e: IncidentEvent) => string` | `incident_id\|stage\|ts\|level` identity | `addIncidents` |
| `LiveStore` | `class LiveStore` | the store | `LiveProvider` |
| `constructor` | `(opts: StoreOptions \| number = {})` | defaults: 60 s history, SLO 150 ms, warn 1.5 | `LiveProvider`, tests |
| `getState` | `() => LiveState` | snapshot for `useSyncExternalStore` | `useLive`, components |
| `subscribe` | `(fn: () => void) => () => void` | change listener | `useLive` |
| `set` (private) | `(patch: Partial<LiveState>) => void` | immutable update + notify | all mutators |
| `setConn` | `(conn: ConnState) => void` | stream state (no-op if unchanged) | `LiveProvider` |
| `setPendingMode` | `(mode: Mode \| null) => void` | optimistic mode while `POST /admin/mode` is in flight | `ModeToggle` |
| `setAdmin` | `(admin: TenantAdminView[]) => void` | stores admin list and derives `barcodes` + `revenue` (ignores empty lists) | `LiveProvider` poller |
| `setOverrides` | `(overrides: OverridesView \| null) => void` | latest overrides (`null` = gateway unreachable) | `LiveProvider` poller |
| `setLab` | `(lab: LabStatus \| null) => void` | latest lab status; `labReachable = lab !== null` | `LiveProvider` poller, `AttackLauncher.refresh` |
| `resetImpact` | `() => void` | restarts the impact counters | `ImpactPanel` |
| `apply` | `(m: BrainMessage, receivedAt?: number) => void` | dispatch to `applyState` or `addIncidents` | `LiveProvider` (WS) |
| `applyState` (private) | `(s: StateSnapshot, receivedAt: number) => void` | appends a history point, drops points older than `historyS`, resets history on a backwards clock jump, clears `pendingMode` once the snapshot confirms it, runs `updateImpact` with barcodes/revenue/warn/SLO/suspected tenants | `apply` |
| `addIncidents` | `(events: IncidentEvent[]) => void` | de-dupes, sorts newest first (ties: newest arrival first), caps at 300, trims the seen-set | `apply`, `LiveProvider` backfill |

**Key decisions**
- No framework inside the store, so the hard parts (history window, dedupe, optimistic mode, impact integration) are unit tested in Node.
- "Suspected" tenants for the impact meter come from the incident log (`IncidentEvent.tenant`), i.e. the brain's own accusation.

### ui/src/lib/live.tsx
`LiveProvider`: creates the backend (real or mock) and the store once, subscribes to the brain stream, back-fills incidents, polls the gateway for admin data (every 30 s) and overrides (every 2 s), and polls the lab status (every 2 s) for the scenario badge, the headline and the attack launcher.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `LiveProvider` | `({ children }: { children: ReactNode }) => JSX.Element` | `useMemo` → `{ backend: config.mock ? createMockBackend({ script }) : realBackend, store: new LiveStore({...}) }` (`?script=0` disables the mock's scripted whale); effect: `backend.incidents()` → `store.addIncidents`, `backend.subscribe(store.apply, store.setConn)`, intervals for `tenants()` → `setAdmin`, `overrides()` → `setOverrides` and `labStatus()` → `setLab` (`null` on error); cleans everything up on unmount | `App` |

### ui/src/lib/liveContext.ts
React context and hooks over the provider (kept separate from `live.tsx` so that file exports only a component).

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `LiveCtx` | `{ backend: Backend; store: LiveStore }` | context value | `LiveProvider` |
| `LiveContext` | `createContext<LiveCtx \| null>(null)` | the context | `LiveProvider`, `useCtx` |
| `useCtx` (private) | `() => LiveCtx` | throws if used outside the provider | hooks below |
| `useBackend` | `() => Backend` | the active backend | `ModeToggle`, `AttackLauncher`, pages, `useHandoff` |
| `useStore` | `() => LiveStore` | the store (for mutators) | `ModeToggle`, `ImpactPanel` |
| `useLive` | `() => LiveState` | subscribes via `useSyncExternalStore` | most components/pages |
| `tickSubscribe`, `sub1s`, `nowSeconds` (private) | timer helpers | 1 s ticking subscription | `useNow` |
| `useNow` | `() => number` | current epoch second, re-rendering every second | countdowns, staleness, "x s ago" |

### ui/src/lib/status.ts
The single place that decides green / amber / red. Also server-cell status, gauge statuses, palette constants and display labels.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Status` | `'ok' \| 'warn' \| 'bad' \| 'idle'` | status vocabulary | everywhere |
| `Thresholds` | `{ p95WarnMs; p95BadMs }` | tile latency thresholds | `tenantStatus` |
| `levelStatus` | `(level: Level) => Status` | normal → ok, warn → warn, throttle/deprioritize/quarantine/blocked → bad | `tenantStatus`, `LevelBadge`, `NoisyBars`, timeline |
| `tenantStatus` | `(t: Pick<TenantView,'level'\|'p95_ms'\|'rps'>, th) => Status` | bad if acted on; `p95_ms <= 0` → idle (warn if level warn); otherwise worst of level and latency (≥ bad → bad, ≥ warn → warn) | tiles, charts, flow view, `Tenants`, `lib/headline.ts` |
| `RANK`, `rank` (private) | ordering idle < ok < warn < bad | comparison | `tenantStatus`, `worst` |
| `worst` | `(statuses: Status[]) => Status` | most severe status | `buildHeadline` (headline tone) |
| `FLEET` | `['w0'..'w8']` | the 3×3 fleet order | `ServerGrid`, `FlowView` |
| `serverStatus` | `(s: ServerView \| undefined) => Status` | drained/missing → idle, slow → bad, `cpu_pct >= 85` → warn, else ok | `ServerGrid`, `FlowView` |
| `fairnessStatus` | `(f: number) => Status` | Jain ≥ 0.9 ok, ≥ 0.75 warn, else bad | fairness gauge |
| `utilizationStatus` | `(u: number) => Status` | < 0.7 ok, < 0.85 warn, else bad | fleet CPU gauge |
| `STATUS_HEX`, `ACCENT_HEX`, `NEUTRAL_HEX` | colour constants | canvas/SVG colours matching the Tailwind tokens | charts, canvas views |
| `MODE_LABEL`, `LEVEL_LABEL` | label maps | `OFF / Industry / TenantGuard`, level names | toggle, badges, Scorecard |

### ui/src/lib/thresholds.ts
Noisy-score thresholds for the bars: the brain's `StateSnapshot.noisy_thresholds` win per key; `VITE_NOISY_*` are only a fallback.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `NoisyThresholds` | `{ warn; throttle; quarantine }` | thresholds | `NoisyBars` |
| `ok` (private) | `(v: unknown) => v is number` | positive finite number | `noisyThresholds` |
| `noisyThresholds` | `(snap, fallback = config.noisy) => NoisyThresholds` | per-key merge brain → fallback | `ControlRoom` |

### ui/src/lib/format.ts
Every number on screen gets a unit here.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `ms` | `(v) => string` | `– ms`, `x.x ms` under 10, integer ms under 1000, `x.xx s` / `x.x s` above | tiles, charts, case file, scorecard |
| `rps` | `(v) => string` | `x.x rps` (integer ≥ 100) | tiles |
| `pct` | `(frac, digits = 0) => string` | 0..1 → `x %` | not used by any component on master |
| `times` | `(v, digits = 1) => string` | `x.x ×` | not used by any component on master |
| `clock` | `(ts: number) => string` | epoch s → `HH:MM:SS` (en-GB, 24 h) | timeline, case file, cases |
| `ago` | `(ts, now?) => string` | `x s ago` / `x min ago` / `x h ago` | `Layout.Connection`, `Tenants` |
| `rupees` | `(v: number) => string` | `₹` + Indian digit grouping | `Tenants` |

### ui/src/lib/quota.ts
Quota edit helpers for the Tenants page.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `QuotaKey` | `keyof Quota` | field names | `Tenants` |
| `FIELDS` | array of `{ key, label, unit, int }` | Rate (rps), Burst (req, int), CPU cost (ms/s), Concurrency (in-flight, int) | `Tenants` |
| `fmt` | `(v: number, int: boolean) => string` | integer or one-decimal display | `Tenants.QuotaCells` |
| `buildPatch` | `(current: Quota, draft: Record<QuotaKey, string>) => { patch } \| { error }` | validates every field (number > 0, whole numbers for burst/concurrency) and returns only the changed fields | `Tenants.EditRow.save` |

### ui/src/lib/case.ts
Case File logic: folds an incident's events into one case, reads the brain's barcode-decoder report, computes the barcode match and turns any report dict into displayable sections.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `CASE_STEPS` | `Stage[]` | `detected, rescued, named, proving, verdict, resolved, explained` (the story order) | `CaseFile.Stepper`, `Placeholder` |
| `NAMING` (private) | `Set<Stage>` | stages whose suspect list is the investigation's answer | `summarizeCase` |
| `CaseSummary` | interface: `id, tenant, openedAt, lastAt, closed, reached, suspects, verdict, action, detail, report, decoder, level, events` | one folded case | Case File, Cases |
| `DecoderView` | `{ pSlow, ranking[{label, kind, value}], hostSuspects, culprits }` | decoder output | Case File |
| `num` (private) | type guard | finite number | `decoderView` |
| `decoderView` | `(report) => DecoderView \| null` | reads `{p_slow, confidence, host_suspects, culprits}`; maps `host_fault` → "host fault", `none` → "nobody"; sorts confidence descending | `summarizeCase` |
| `summarizeCase` | `(id: string, events: IncidentEvent[]) => CaseSummary` | sorts the incident's events oldest first; first time per stage; suspects from the last naming-stage event (later stages may carry a post-recovery score); latest verdict/action/detail/report/level; keeps the latest decoder report; `closed` when `resolved` or `closed` was reached | `CaseFile`, `caseIndex`, `mock/handoff.ts` |
| `stepTime` | `(c, step) => { ts, via } \| null` | when a step was reached; `closed` also completes the Resolved step (the Wave 1 brain closes incidents without sending `resolved`) | `CaseFile` |
| `barcodeMatch` | `(barcode: string[], slow: string[]) => { matched, missing, score }` | how many slow servers lie inside the suspect's barcode | `CaseFile` |
| `caseIndex` | `(events: IncidentEvent[]) => CaseSummary[]` | one summary per incident id, newest first | `Cases` |
| `ReportSection` | union `text \| list \| table` | rendered report part | `CaseFile.Report` |
| `label`, `scalar` (private) | key → words (ids like `w3`, `K` stay as they are); value → string | display helpers | `reportSections` |
| `reportSections` | `(report: Record<string, unknown>) => ReportSection[]` | long strings → paragraphs, arrays → lists, objects → key/value tables, loose scalars gathered into a leading "Facts" table | `CaseFile` |

### ui/src/lib/scorecard.ts
Scorecard aggregation: `RunResult` files grouped by scenario × mode with medians.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `SCORE_MODES` | `['off','industry','tenantguard']` | column order | `Scorecard` |
| `Cell` | interface: `scenario, mode, runs, baseline, attack, after, detect, recover, attacker429, slowdown` | one scenario × mode cell | `Scorecard` |
| `ScenarioRow` | `{ scenario, attacker, cells: Record<Mode, Cell> }` | one scenario | `Scorecard` |
| `median` | `(xs: (number \| null \| undefined)[]) => number \| null` | median ignoring missing values | `groupResults` |
| `groupResults` | `(results: RunResult[]) => ScenarioRow[]` | groups, takes medians of victim p99 baseline/attack/after, time to detect/recover, attacker 429s; `slowdown = attack / baseline`; sorted by scenario | `Scorecard` |
| `slowdownStatus` | `(x: number \| null) => Status` | ≤ 1.5× ok, ≤ 3× warn, worse bad, none idle | `Scorecard` bars + table |

### ui/src/lib/flow.ts
Routing model for the flow view: where the gateway would send a tenant's next request.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Route` | `{ server; weight }` (normalized 0..1) | one route | `FlowView` |
| `tenantRoutes` | `(tenant, barcode, ov) => Route[]` | whole-tenant relocation first (class-only relocations do not move the tenant); else `route_weights[tenant]`; else equal barcode weights; drained servers removed unless every candidate is drained; normalized | `routeTable` |
| `routeTable` | `(tenants, barcodes, ov) => Record<string, Route[]>` | routes for all tenants | `FlowView` frame loop |
| `pickServer` | `(routes: Route[], u: number) => string \| null` | weighted pick for `u` in [0, 1) | `FlowView` dot spawning |
| `RerouteHint` | `{ kind: 'rerouted' \| 'refused' \| 'off'; text }` | one hint line | `FlowView` |
| `rerouteHint` | `(ov, attacking: boolean, mode: Mode, servers: ServerView[]) => RerouteHint \| null` | why reroute lines are (or are not) on screen. Tenants with route weights or a relocation: `↷ N tenants rerouted around w4` (servers with weight 0, else the slow servers). No steering and no attack: `null`. Attack in OFF: "mode OFF: no limits and no rerouting, the attack reaches the servers". Attack in a protected mode: "no rerouting needed: the excess is refused at the gateway (red ✕)" | `FlowView` |

**Key decisions**
- Mirrors the gateway's documented routing rules (relocation, route weights, drains never black-hole a tenant) so the animation shows what the gateway actually does.
- The reroute hint exists because during a whale or flood the gateway limits usually refuse the excess before any server gets slow, so no blue reroute lines appear. Without the hint that looks broken.

### ui/src/lib/scenario.ts
What traffic is running right now, from lab `GET /lab/status`. Feeds the scenario badge.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `ScenarioKind` | `'attack' \| 'run' \| 'calm' \| 'idle' \| 'unknown'` | badge kind | `StatusBar` (badge colour) |
| `Scenario` | `{ kind, label, attacks: AttackStatus[] }` | badge text and the running attacks, newest first | `buildHeadline`, `StatusBar`, `FlowView` |
| `mmss` | `(seconds: number) => string` | `m:ss` countdown (never negative) | `scenarioOf` |
| `scenarioOf` | `(lab: LabStatus \| null, reachable: boolean, now: number) => Scenario` | unreachable or no status: `Lab offline: traffic unknown`. Running attacks not yet past `ends_at`: `Attack: whale on K (0:42 left)` (`+N more` when several). Else a scenario run: `Scenario run <id> in progress`. Else calm traffic: `Calm traffic · no attack`. Else `No traffic` | `StatusBar`, `FlowView` |

### ui/src/lib/headline.ts
The Control Room headline, built from structured data instead of the brain's long `status_sentence`. Short templates so it is always shown in full (never an ellipsis); the brain sentence stays on the details line and in its tooltip.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Headline` | `{ tone: Status; text: string; details: string }` | headline colour, text, small second line | `StatusBar` |
| `plural` (private) | `(n, word) => string` | `1 tenant` / `2 tenants` | `buildHeadline` |
| `ACTION` (private) | level → verb | warn → watched, throttle → throttled, deprioritized, quarantined, blocked | `actionText` |
| `actionText` (private) | `(t: TenantView) => string` | verb plus `NN%` when the factor is below 100 % (not for warn) | `buildHeadline` |
| `buildHeadline` | `(s, scenario, th, barcodes, overrides, opts?: { includeAttack? }) => Headline` | parts joined by ` · `: the attack (`whale on K`, left out with `includeAttack: false` because the badge already names it); one flagged tenant (`K throttled 50%`) or `N acted on: A, B, C +n`; with slow servers and nobody flagged: `w4 slow`, `N rerouted` (route-weight tenants, else tenants on the slow servers), `no one blamed`; nothing to say: `No attack` / `No attack detected` (lab offline) / `No traffic`; then `N slowed` (hurt non-flagged tenants), `N protected` or `N tenants healthy`. `Defences OFF · ` prefix in OFF mode. Tone: worst status of the other tenants when someone is hurt (or an attack runs in OFF), warn while acting, idle with no traffic, else ok. Details: mode, fleet CPU %, fairness, tenants without traffic | `StatusBar` |

Examples (from the file header): `No attack · 12 tenants healthy`, `whale on K · K throttled 50% · 11 protected`, `K watched · 11 protected`, `w4 slow · 4 rerouted · no one blamed`.

### ui/src/lib/reason.ts
The short "why" line on every tile whose level is not normal. A level badge is never shown without a reason.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `TileReason` | `{ text; detail; incidentId: string \| null }` | visible text, tooltip (latest event detail), case link | `TenantTiles` |
| `rank` (private) | `(l: Level \| null \| undefined) => number` | index in `LEVELS` (-1 when missing) | `tileReason` |
| `latestByTenant` | `(events: IncidentEvent[]) => Map<string, IncidentEvent>` | latest event per tenant from the newest-first log | `ControlRoom` |
| `tileReason` | `(t: TenantView, latest?: IncidentEvent) => TileReason \| null` | normal: `null`. Warn: `using X.X× its fair share · neighbours slower` (or `back within its fair share · still watched` when the score is below 1). Recovering (level stepping down, or the detail starts with relax / recover / cleared / back to), calm, or not limited: `recovering · limits back to NN%` / `recovering · back to its full plan`. Else `used X.X× its fair share · limits cut to NN%`. Without an event the tooltip falls back to the tenant's level and noisy score | `TenantTiles` |

**Key decisions**
- Only a detail that *starts* with a recovery verb counts as recovering, so a verdict sentence such as "until it calms down" on a quarantined attacker does not.

### ui/src/lib/impact.ts
The client-side ₹ impact meter, integrated per snapshot (dt capped at 2 s).

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `MONTH_S` | `2_592_000` | seconds in 30 days | revenue pro-rating |
| `ImpactContext` | `{ barcodes, revenue, warn, sloMs, suspected }` | inputs per snapshot | `LiveStore.applyState` |
| `ImpactState` | `{ since, lastTs, avoidedS, sufferedS, innocents, noisy, revenueRs }` | accumulated counters | store |
| `emptyImpact` | `() => ImpactState` | zeroed state | store constructor, `resetImpact` |
| `ACTED` (private) | levels throttle, deprioritize, quarantine, blocked | "acted on" | `updateImpact` |
| `updateImpact` | `(prev, s: StateSnapshot, ctx) => ImpactState` | records tenants seen noisy (own score ≥ warn); a tenant is threatened when it shares a server with a flagged tenant or sits on a slow server whose p95 ≥ SLO, and is not itself a culprit (noisy or named by a brain incident); avoided seconds = threatened and within SLO in TenantGuard mode (adds pro-rated revenue); suffered seconds = any tenant over SLO; innocents = acted on but never a culprit; first snapshot or a backwards clock only starts the clock | `LiveStore.applyState` |
| `ImpactSummary` | `{ revenueRs, customers, customersMonthlyRs, avoidedS, sufferedS, innocents, sinceS }` | display figures | `ImpactPanel` |
| `summarizeImpact` | `(st, revenue) => ImpactSummary` | totals and the shielded customer list | `ImpactPanel` |

**Key decisions**
- Two corrections came from a run on the real Docker host: a culprit still recovering on the ladder (factor < 1, score back to 0) is not an innocent, and a server merely slower than its peers (e.g. 20 ms vs 13 ms) threatens nobody unless its p95 would breach the SLO.
- The contract also carries a brain-owned `StateSnapshot.impact` (`ImpactView`); the UI does not read it on this branch and shows this meter.

### ui/src/lib/useBarcodes.ts
| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `useBarcodes` | `() => Record<string, string[]>` | tenant → barcode servers from the store (gateway `/admin/tenants`, layout fallback) | `ControlRoom`, `CaseFile` |

### ui/src/lib/useHandoff.ts
| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `HandoffState` | `{ cases: CaseView[] \| null; error: string \| null }` | `null` cases = bridge unreachable | Cases, Case File |
| `useHandoff` | `() => HandoffState` | polls `backend.handoffCases()` every 5 s | `Cases`, `CaseFile.HandoffPanel` |
| `pendingActions` | `(c: CaseView) => CaseAction[]` | pending actions, nearest deadline first | `Cases`, `CaseFile.HandoffPanel` |

---

## Components (`src/components`)

### ui/src/components/Layout.tsx
App shell: header with logo, navigation tabs, the brain-connection indicator and the mode toggle; pages render in `<Outlet />`. The shell uses normal page scroll (no fixed one-screen height), so panels are never squeezed to fit the window.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Connection` (private) | `() => JSX.Element` | dot + text: connecting / brain offline · retrying / brain degraded (`brain_ok == false`) / waiting for state / stale (> 3 s since the last snapshot) / live; `MOCK` badge in mock mode | `Layout` |
| `tab` (private) | `({ isActive }) => string` | NavLink class names | `Layout` |
| `Layout` | `() => JSX.Element` | header (Control Room, Tenants, Cases, Scorecard) + `<Outlet />` | `App` router |

### ui/src/components/StatusBar.tsx
The fixed-height headline at the top of the Control Room: what traffic is running, and what is happening, one line each.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `TONE` (private) | `Record<Status, string>` | headline text colour | `StatusBar` |
| `BADGE` (private) | `Record<ScenarioKind, string>` | badge colours: attack red, scenario run accent, calm green, idle / unknown grey | `StatusBar` |
| `StatusBar` | `({ snapshot }) => JSX.Element` | status dot (headline tone); scenario badge from `scenarioOf(lab, reachable, now)` (`data-testid="scenario-badge"`, `data-kind`); headline from `buildHeadline(..., { includeAttack: false })` (`status-sentence`, wraps to at most two lines, never an ellipsis); details line (`status-details`, truncated) with the brain's own `status_sentence` appended and as tooltip | `ControlRoom` status row |

**Key decisions**
- The badge names the attack, so the headline does not repeat it. This keeps the line short at 1366 px.

### ui/src/components/ModeToggle.tsx
OFF / Industry / TenantGuard segmented control (radio group). The active mode is the optimistic pending mode, else the snapshot's mode, else `GET /admin/mode` while no snapshot has arrived.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `HINT` (private) | `Record<Mode, string>` | tooltip per mode | buttons |
| `ModeToggle` | `() => JSX.Element` | renders the toggle; shows a red error line if the POST fails | `Layout` |
| `choose` (inner) | `(mode: Mode) => Promise<void>` | sets the pending mode, `backend.setMode`, clears pending after 3 s if no snapshot confirmed it, reverts on error | button click |

### ui/src/components/StatusDot.tsx
| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `DOT` (private) | `Record<Status, string>` | classes; red pulses | `StatusDot` |
| `StatusDot` | `({ status, className? }) => JSX.Element` | coloured status dot with `aria-label` | `Layout`, `StatusBar`, `Tenants` |

### ui/src/components/LevelBadge.tsx
| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `STYLE` (private) | per-status classes | badge colours | `LevelBadge` |
| `LevelBadge` | `({ level, className? }) => JSX.Element` | upper-case level name coloured by `levelStatus` | tiles, timeline, Tenants, Cases, Case File |

### ui/src/components/Gauge.tsx
| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Gauge` | `({ label, value (0..1 \| null), display, unit?, status, hint? }) => JSX.Element` | SVG semicircle; arc length = value, arc colour = status, big value + unit inside | `ControlRoom` (fairness, fleet CPU) |

### ui/src/components/TenantTiles.tsx
Twelve tenant tiles (six per row at projector width), each a fixed 132 px high. Each tile is a `role="button"` div with `tabIndex` 0 (Enter or Space selects it), so the case link inside it is valid HTML; selecting one outlines its barcode on the server grid.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `FRAME`, `NUMBER` (private) | per-status classes | tile border/background and number colour | `Tile` |
| `Tile` (private) | `({ t, latest?, selected, onSelect? }) => JSX.Element` | id, name, tier, p95 (or "no traffic"), rps and rejected/s, level badge, `NN % quota` when the rounded factor is below 100, placement when relocated; bottom line `why: …` from `tileReason` (`data-testid="why-<id>"`, tooltip = full detail) with a `case →` link to `/case/:incident_id` (`why-link-<id>`); normal tiles say `within its fair share` or `no requests in the last 5 s` | `TenantTiles` |
| `onKey` (inner) | `(e: KeyboardEvent) => void` | Enter / Space select the tile | `Tile` |
| `TenantTiles` | `({ tenants, latest?: Map<string, IncidentEvent>, selected?, onSelect? }) => JSX.Element` | responsive grid of tiles; `latest` comes from `latestByTenant` | `ControlRoom` |

### ui/src/components/Charts.tsx
Recharts line and area charts over the store's 60 s history.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `AXIS`, `TOOLTIP`, `LOG_TICKS`, `GREYS` (private) | style constants | axis/tooltip styling, log ticks 10 ms … 3 s | charts |
| `lineColour` (private) | `(t: TenantView) => string` | neutral grey for healthy tenants, status colour otherwise | both charts |
| `useRows` (private) | `(history, key: 'p95' \| 'cpu') => rows` | x = seconds before the newest point; cpu → percent; p95 ≤ 0 → gap | both charts |
| `P95Chart` | `({ history, tenants }) => JSX.Element` | log-scale p95 per tenant with dashed warn/bad lines; legend lists up to 6 tenants out of SLO ("+N more") | `ControlRoom` |
| `CpuShareChart` | `({ history, tenants }) => JSX.Element` | stacked CPU-share area (greys for healthy, status colour for flagged); header shows the top consumer | `ControlRoom` |

### ui/src/components/NoisyBars.tsx
| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `NoisyBars` | `({ tenants, thresholds }) => JSX.Element` | one bar per tenant sorted by `noisy_norm` (bar length), raw `noisy_score` as `x.x ×` text, warn/throttle markers at `threshold / quarantine`; bar colour = level status | `ControlRoom` (both views) |

### ui/src/components/ServerGrid.tsx
The 3×3 fleet (w0..w8) plus a text line for spare/quarantine.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `FRAME`, `TEXT`, `BAR`, `HATCH` (private) | per-status classes; hatched background | cell styling; drained servers are hatched | `Cell` |
| `Cell` (private) | `({ sv, s, lit, compact, note? }) => JSX.Element` | name, status, peer ratio `x.x × peers`, CPU bar + `%`, optional note (e.g. `P(slow) 53 %`); accent outline when `lit`. Minimum height 58 px compact / 84 px full; text truncates instead of overflowing. Compact cells put the peer ratio on the name line and drop the word `healthy` (the colour already says it; slow / drained / no data keep the word) | `ServerGrid` |
| `ServerGrid` | `({ servers, highlight?, highlightLabel?, compact?, title?, annotate? }) => JSX.Element` | grid with highlighted barcode servers; `data-testid="server-grid"`, and per cell `server-<id>`, `data-status`, `data-lit` for tests | `ControlRoom`, `CaseFile` |

### ui/src/components/IncidentTimeline.tsx
Newest-first incident log; every row links to `/case/:incident_id`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `STAGE_LABEL` (private) | `Record<Stage, string>` | stage names | rows |
| `stageColour` (private) | `(e: IncidentEvent) => string` | resolved/closed/rescued green, detected amber, level changes by new level | rows |
| `Transition` (private) | `({ prev?, next? }) => JSX.Element \| null` | `prev → next` level badges | rows |
| `IncidentTimeline` | `({ events, max = 40 }) => JSX.Element` | header with open-case and event counts; rows with time, tenant, stage, verdict, action, detail | `ControlRoom` |

### ui/src/components/AttackLauncher.tsx
Launch and stop lab attacks. Reads the lab status from the store (`LiveProvider` polls it every 2 s) and refreshes it at once after its own launch or stop.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `FALLBACK_TENANTS`, `FALLBACK_SERVERS`, `ABOUT` (private) | constants | tenant ids (A–L) and server ids (`w0`–`w8`) before the first snapshot; one-line description for each of the ten attacks (for example `blackbox_bomb`: "cheap ingest calls, heavy hidden indexer CPU"; `faulty_server`: "a server turns slow by itself (no tenant)") | form |
| `AttackLauncher` | `() => JSX.Element` | attack select over `LAUNCHABLE_ATTACKS` (all ten); a target select that is a tenant picker (default K), or a server picker (default `w4`, every server in the brain snapshot) when `faulty_server` is chosen; duration (≤ 600 s), intensity slider 0.1–5×; the button reads "Launch on <target>" (for example "Launch on K", "Launch on w4"); running attacks with countdown and Stop; "lab unreachable" state when `labReachable === false` | `ControlRoom` |
| `refresh` (inner) | `() => Promise<void>` | `backend.labStatus()` → `store.setLab` (`null` on error) | after launch / stop |
| `launch` (inner) | `() => Promise<void>` | builds `AttackReq` (`tenant` set and `server: null`, or for `faulty_server` `tenant: null` and `server` set) and `backend.startAttack` | Launch button |
| `stop` (inner) | `(id: string) => Promise<void>` | `backend.stopAttack` | Stop button |

**Key decisions**
- The faulty-server scene is launched from the UI like any other attack: the lab injects the fault on the chosen server and clears it when the attack stops.
- One form serves both kinds of target. Only the target select changes, so the operator never sends a tenant with `faulty_server` or a server with a tenant attack.

### ui/src/components/ImpactPanel.tsx
| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `rs`, `lakh`, `dur` (private) | formatters | `₹x.xx` / grouped ₹; `₹x.x L` / `₹x.xx Cr`; `x s` / `x.x min` | `ImpactPanel` |
| `TIP` (private) | string | the definitions shown on hover | `ImpactPanel` |
| `ImpactPanel` | `() => JSX.Element` | revenue protected, customers shielded (+ ₹/month), SLO breach avoided / suffered, innocents penalised (green at 0, red otherwise) with a reset button | `ControlRoom` status row |

### ui/src/components/FlowView.tsx
Live request flow on a `<canvas>`: dots travel tenant → gateway → server, sampled from snapshot rps and routed with `lib/flow.ts`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Dot` (private) | `{ tenant, to (server \| null = refused), born, life, colour, lane }` | one animated request | frame loop |
| `TRAVEL_S`, `MAX_DOTS_PER_S`, `EXTRA` (private) | 1.6 s, 140 dots/s, `['spare','quarantine']` | animation constants | frame loop |
| `FlowView` | `({ snapshot, overrides, barcodes }) => JSX.Element` | canvas + legend; latest props reach the loop through a ref, so the loop is created once. Above the canvas a hint line from `rerouteHint` (`data-testid="reroute-hint"`, `data-kind`): accent when rerouting, red in OFF, white when the gateway refused the excess; "attacking" comes from `scenarioOf(lab, …)` | `ControlRoom` (Flow view) |
| `resize` (inner) | `() => void` | sizes the canvas for `devicePixelRatio`; driven by a `ResizeObserver` | effect |
| `frame` (inner) | `() => void` | per animation frame: layout (tenant column, gateway bar, 3×3 + spare/quarantine boxes), spawn dots per tenant from rps and rejected rps (rate-limited, deterministic PRNG), draw route lines for steered tenants (width = weight), servers coloured by status (drained dashed), tenant labels fitted with `measureText` (↷ when rerouted), dots on a curve through the gateway; refused requests end as red crosses at the gateway | `requestAnimationFrame` |

### ui/src/components/ProofAnimation.tsx
The relocation proof on a `<canvas>`: red smoke drifts from the suspect's barcode servers to the spare.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Puff` (private) | `{ from, born, life, drift, size }` | one smoke particle | frame loop |
| `REPLAY_S` (private) | 8 | replay length (s) | Replay button |
| `ProofAnimation` | `({ tenant, servers, target, live }) => JSX.Element` | runs while `live` (proving stage or an active relocation) or during a replay; `data-running` for tests; explanatory sentence | `CaseFile` |
| `resize` / `frame` (inner) | canvas sizing / drawing | source boxes fade as smoke leaves, target box fills; puffs follow a drifting curve with radial gradients | effect |

---

## Pages (`src/pages`)

### ui/src/pages/ControlRoom.tsx
The dashboard. Panels have fixed heights and the page scrolls normally, so nothing is squeezed, collapses or overlaps at any window size (1366×768 and 1920×1080 are tested by `e2e/layout.spec.ts`). Rows, top to bottom: status row (92 px: `StatusBar`, ₹ impact, fairness and fleet-CPU gauges shown from 1536 px wide (`2xl`; below that their numbers stay on the details line), Tiles/Flow toggle) · tiles plus a one-line legend (p95, THROTTLE, fair share, "click a tile to see its 3 servers") · p95 chart + server grid + noisy bars (270 px) · incident timeline + CPU share + attack launcher (300 px). In Flow view the tiles and the chart row are replaced by the flow canvas (560 px) next to the noisy bars. Every panel carries `data-panel` (status, tiles, p95, servers, noisy, flow, timeline, cpu, launcher) for the layout tests.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `View`, `VIEW_KEY` (private) | `'tiles' \| 'flow'`, localStorage key | view state | toggle |
| `initialView` (private) | `() => View` | `?view=` wins, then localStorage (wrapped in try/catch), else tiles | `useState` initializer |
| `ViewToggle` (private) | `({ view, onChange }) => JSX.Element` | Tiles / Flow radio buttons | status row |
| `ControlRoom` | `() => JSX.Element` | "Waiting for the first state snapshot…" / "Brain unreachable" until the first snapshot; `latestByTenant(incidents)` feeds the tile reasons; the grid highlights the picked tile's barcode, otherwise the noisiest flagged tenant | router `/` |
| `setView` (inner) | `(v: View) => void` | state + best-effort localStorage write | `ViewToggle` |

### ui/src/pages/Tenants.tsx
Tenants & quotas table with inline quota editing; polls `GET /admin/tenants` and `GET /admin/overrides` every 3 s.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `QuotaCells` (private) | `({ q, eff, factor }) => JSX.Element` | configured quota per field with unit; `eff` value (red when reduced) under it | table rows |
| `EditRow` (private) | `({ t, onCancel, onSaved }) => JSX.Element` | four inputs, Enter saves, Escape cancels; error line on validation/HTTP failure | table rows |
| `save` (inner of `EditRow`) | `() => Promise<void>` | `buildPatch`; nothing changed → cancel; else `backend.patchQuota` | Save / Enter |
| `Tenants` | `() => JSX.Element` | header with heartbeat / fail-static and active-limit count; table: tenant, tier · weight (× weight factor), servers (→ placement), live p95 + level, four quota fields, override (factor + expiry countdown, BLOCKED), Edit button; panel of all override keys with time left | router `/tenants` |

### ui/src/pages/Cases.tsx
Index of all incidents (REST backfill + live) with an optional handoff column.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Cases` | `() => JSX.Element` | `backend.incidents()` once + live incidents → `caseIndex`; bridge status line; table: incident link, tenant, opened, open/resolved, level, verdict, handoff (case id · owner, pending count and next deadline) when the bridge answers, latest detail | router `/case` |

### ui/src/pages/CaseFile.tsx
The story of one incident at `/case/:incident_id`. Fetches `GET /api/incidents?incident_id=` every 3 s and `GET /admin/overrides` every 2 s, and merges live events from the stream.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `STEP_LABEL`, `key` (private) | labels; `stage\|ts\|level` identity | display, de-dupe | page |
| `Stepper` (private) | `({ c }) => JSX.Element` | seven steps with time and `+x.x s` from detection; "(closed)" when Resolved was completed by `closed` | page |
| `Suspects` (private) | `({ c }) => JSX.Element` | confidence bars from the brain's suspects; for server incidents the decoder ranking (host fault / nobody / tenants) | page |
| `Report` (private) | `({ sections }) => JSX.Element` | renders `reportSections` (paragraphs, lists, tables) | page |
| `Placeholder` (private) | `({ c, name }) => JSX.Element` | template text from the events when the incident has no report yet | page |
| `HandoffPanel` (private) | `({ incidentId }) => JSX.Element` | bridge case: id, status, severity, delivered, owner since (IST), actions with state and deadline countdown; or "not reachable" / "no case" | page |
| `ttlFor` (private) | `(ov, tenant) => { key, expires } \| null` | latest override expiry for the tenant (`<kind>:<tenant>` keys) | page |
| `CaseFile` | `() => JSX.Element` | header (status, suspect or "Server incident · host …", level, duration); stepper; verdict + last action + override TTL; suspects; latest detail; proof animation (when the case reached proving or the tenant has a relocation); live grid with the barcode (or host suspects / culprits' barcodes) outlined and P(slow) notes; barcode-match sentence; report; handoff panel; full event log | router `/case/:incidentId`, timeline links |

### ui/src/pages/Scorecard.tsx
OFF / Industry / TenantGuard comparison from lab `GET /lab/results` (refreshed every 10 s).

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `AXIS`, `NEUTRAL`, `NEUTRAL_LIGHT`, `secs`, `count` (private) | styling and formatters | `x.x s`, grouped counts | page |
| `ScenarioChart` (private) | `({ row }) => JSX.Element` | bar chart per scenario: baseline (grey), attack (coloured by `slowdownStatus`, labelled), after (light grey), x = mode | page |
| `Scorecard` | `() => JSX.Element` | FIXTURE banner in mock mode ("invented numbers, not measured"), error state when the lab is unavailable, empty state, chart grid and a scenario × mode table (runs, p99 baseline/attack/after, slowdown, time to detect/recover, attacker 429s) | router `/scorecard` |

**Key decisions**
- Numbers on slides must come only from measured `lab/results`; mock fixtures are always visibly marked.

---

## Mock backend (`src/mock`)

### ui/src/mock/fixture.ts
Copy of `layouts/ag23.json` tenants (id, name, tier, barcode servers, monthly revenue, protected, `sla_p95_ms`) and of `tenantguard.yaml` tiers and `brain.noisy`, so the UI and its simulation run with no backend. It follows the rebalanced layout (A pro, F enterprise, G free, L enterprise) and the 5 s warn hold. It only drives the mock and the store's barcode/revenue fallback; against a real stack the gateway's `/admin/tenants` replaces it within the first poll.

| Function / class | Signature | What it does | Used by |
|---|---|---|---|
| `FixtureTenant` | interface | layout tenant, including `sla_p95_ms` | `TENANTS` |
| `SERVERS` | `['w0'..'w8']` | fleet | `sim.ts` |
| `TENANTS` | `FixtureTenant[]` (12) | A Aarogya Health … L LokVani News | `sim.ts`, `store.ts` fallback |
| `TIERS` | weight + quota per tier | enterprise 4 / pro 2 / free 1 | `sim.ts` |
| `NOISY` | thresholds and hold times | warn 1.5, throttle 2, quarantine 4, … | `sim.ts` |

### ui/src/mock/sim.ts
`MockPlatform`: a small, seeded simulation of gateway + workers + brain + lab that emits contract-shaped messages and reacts to mode, attacks and quota edits. Written in erasable TypeScript so Node runs it directly (`scripts/`). It is a plausible stand-in, not the real detector.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `SERVER_CAP_MS`, `BASE_LAT_MS`, `CPU_PER_REQ_MS`, `BASE_RPS`, `TTL_S` (private) | 500, 6, 7, per tier, 60 | model constants | `tick` |
| `AttackProfile`, `PROFILES` (private) | per attack: rps, CPU per request, bypasses the cost bucket, legit | attack shapes (whale = few 400 ms requests; blackbox bypasses the cost bucket; honest_spike is legit). The simulation keeps a profile for all ten contract names, and the UI can launch all ten. `faulty_server` needs a server `w0`..`w8` (422 otherwise) and slows that server | `tick` |
| `SimError` | `class SimError extends Error { status }` | HTTP-like error (404/422) | validators; mapped to `ApiError` / HTTP status |
| `TState` (private) | per-tenant state (quota, level, factor, weight factor, smoothed p95/rps/rejected, score, hold timers, incident id, override expiry, story) | simulation state | methods |
| `mulberry32` (private) | `(seed: number) => () => number` | seeded PRNG (stable screenshots) | constructor |
| `clamp` (private) | `(x, lo, hi) => number` | clamp | everywhere |
| `MockPlatform` | `class` | the simulation | `mockBackend.ts`, `mock-ws-server.mjs`, `sim-smoke.mjs`, tests |
| `constructor` | `(seed = 7)` | 12 tenants at tier quotas | — |
| `setMode` | `(mode: Mode) => { mode }` | validates; leaving TenantGuard clears brain overrides | backends |
| `tenantsAdmin` / `adminView` | `() => TenantAdminView[]` / `(id) => TenantAdminView` | admin views with effective quota (`max(1, round(base × factor))` for ints) | backends |
| `patchQuota` | `(id, patch) => TenantAdminView` | validates > 0, rounds ints | backends |
| `overrides` | `(now) => OverridesView` | rescue route weights (TenantGuard mode: weight 0 on slow servers for tenants with a healthy alternative), active limits with expiry | backends |
| `startAttack` / `stopAttack` / `labStatus` | lab behaviour | validates like the lab (intensity 0.1–5, duration (0, 600], tenant or server) | backends |
| `expireAttacks` (private) | `(now) => void` | running → done at `ends_at` | `labStatus`, `tick` |
| `tick` | `(now) => { snapshot, incidents }` | one 2 Hz step: demand per tenant (attacks added), admission caps in industry/TenantGuard (rate × factor, cost × factor), per-server load and latency (queueing + head-of-line behind heavy requests), smoothed tenant views, calm baselines, noisy score (`usage × (1 + victim impact)`), ladder, server peer ratios (slow at ≥ 1.6× and ≥ 8 ms), utilization and Jain fairness | backends (timer) |
| `ladder` (private) | `(id, s, now, hurting, calm, events, dt) => void` | normal → warn → throttle (×0.5) → deprioritize (weight 0.25) → quarantine (0.1/0.1) with hold times; observe-only outside TenantGuard; recovery +0.1 per second after the recover hold; emits `detected` / `level_change` | `tick` |
| `recover` (private) | `(s, now, events, name) => void` | back to normal; emits `level_change`, `resolved`, and `explained` with a report dict when actions were taken | `ladder` |
| `barcode` / `coTenants` (private) | `(id) => string[]` | tenant's servers / tenants sharing a server | story, report |
| `storyStep` (private) | `(s, level, prev, action, now, events, name) => void` | the brain's case stages while escalating in TenantGuard mode: `named` at throttle, `proving` at deprioritize, `verdict` (`costly_pattern`) at quarantine; records peaks for the report | `ladder.set` |
| `ev` (private) | `(s, stage, now, extra) => IncidentEvent` | builds an event | ladder/story |
| `snapshot` | `(now) => StateSnapshot` | contract snapshot incl. `noisy_thresholds` | `tick` |
| `sentence` (private) | `(tenants) => string` | status sentence per mode | `snapshot` |

### ui/src/mock/mockBackend.ts
In-browser fixture replay: `MockPlatform` behind the `Backend` interface.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `TICK_MS`, `now` (private) | 500 ms; epoch seconds | timing | `createMockBackend` |
| `createMockBackend` | `(opts?: { script?: boolean }) => Backend` | ticks the simulation every 500 ms while subscribed, keeps the last 500 incidents, scripted whale on K (intensity 2, 45 s) 4 s after start unless `script: false`; every call is async (60 ms) and returns a structured clone; `SimError` → `ApiError`; `handoffCases` via `mockCases`, `results` via `RESULTS_FIXTURE` | `LiveProvider` when `config.mock` |

### ui/src/mock/results.ts
`RESULTS_FIXTURE`: **invented** `RunResult`s (4 scenarios × 3 modes × 2 runs) for developing the Scorecard. Never measured; the Scorecard labels them as fixtures.

| Function / class | Signature | What it does | Used by |
|---|---|---|---|
| `VICTIMS`, `Shape`, `SHAPES`, `MODES`, `T0` (private) | fixture shapes | per scenario and mode: attack/after p99, detect/recover times, attacker 429s | `RESULTS_FIXTURE` |
| `phase` (private) | `(base, attack, after, j) => PhaseP99` | p99 triple with small jitter | `RESULTS_FIXTURE` |
| `RESULTS_FIXTURE` | `RunResult[]` | the fixture list | `mockBackend`, `mock-ws-server.mjs`, `scorecard.test.ts` |

### ui/src/mock/handoff.ts
Mock handoff bridge: derives `CaseView`s from the incident stream the way the bridge turns incidents into partner cases.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `DECISION_WINDOW_S`, `OWNER` (private) | 120 s; owner label | deadline, owner | `mockCases` |
| `ist` (private) | `(ts) => 'HH:MM:SS'` | IST display string | `mockCases` |
| `mockCases` | `(events, now, uiOrigin?) => CaseView[]` | one case per incident once TenantGuard acted (throttle+ or verdict), `INC-000n` ids, owner after 8 s, one pending action with deadline (resolved/expired later), history, `evidence_link` = `{uiOrigin}/case/{incident_id}` | `mockBackend`, `mock-ws-server.mjs` |

---

## Tests (`src/**/*.test.ts`, Vitest)

76 unit tests in 12 files (`npm test`).

| File | Tests | What it proves | Exercises |
|---|---|---|---|
| `src/lib/status.test.ts` | 6 | level → colour; a hurt victim goes amber then red by latency; a throttled tenant is red even when fast; `p95 0` is idle (no data), warn stays warn; server cells (slow red, hot amber, drained grey); `worst`, gauge statuses | `status.ts` |
| `src/lib/store.test.ts` | 6 | 60 s window; history reset on a backwards clock; incident dedupe + newest-first; same-timestamp events newest-arrival first; optimistic mode cleared by a confirming snapshot; subscribers notified until unsubscribed | `LiveStore` |
| `src/lib/quota.test.ts` | 3 | only changed fields are sent; zero/negative/empty/non-numeric rejected; whole numbers for burst/concurrency | `buildPatch` |
| `src/lib/backend.test.ts` | 3 | only state/incident WS frames parse; FastAPI `{loc,msg}` errors and gateway list-of-strings `detail` become readable lines | `parseBrainMessage`, `errorText` |
| `src/lib/thresholds.test.ts` | 2 | brain thresholds win; per-key fallback for missing/invalid values | `noisyThresholds` |
| `src/lib/case.test.ts` | 7 | folding (order, first stage times, suspects from naming stages, latest verdict/action/level/detail); `closed` completes Resolved; open vs closed; case index order; barcode match; real decoder report shape (ranking, P(slow), host suspects kept from an earlier report); report sections incl. ids kept as-is | `case.ts` |
| `src/lib/scorecard.test.ts` | 4 | median ignores nulls; grouping with medians and slowdown; slowdown colours; fixtures cover every mode | `scorecard.ts`, `results.ts` |
| `src/lib/flow.test.ts` | 7 | equal barcode weights; route weights normalized; whole-tenant relocation vs class-only; drains skipped but never all; weighted pick; reroute hint names the servers routed around; an attack with no rerouting says the gateway refused the excess (OFF says no limits; no attack → no hint) | `flow.ts` |
| `src/lib/impact.test.ts` | 8 | neighbour kept within SLO while K is flagged, pro rata on revenue; OFF protects nobody and counts suffered time; a recovering culprit is not innocent; a culprit is never a shielded customer; a mid-recovery page uses the incident log; an acted-on non-culprit is innocent; only SLO-breaching slow servers threaten; dt cap and clock jump | `impact.ts` |
| `src/lib/clarity.test.ts` | 21 | scenario badge (4): running attack with countdown; extra attacks counted, stopped/expired ignored; calm, scenario run, no traffic, lab offline; `m:ss`. Headline (10): calm and healthy; attack on K with K throttled and neighbours protected; attack not repeated next to the badge; victims slowed in OFF; slow server with no culprit; a tenant back at 100 % is not shown "throttled"; several tenants listed briefly; lab offline says what it observes; stays short in the busiest case; no traffic at all. Tile reason (7): normal has none; throttled with the over-share, the cut and the incident link; warn; recovering after an attack; a quarantined attacker whose verdict says "until it calms down" is not recovering; a badge always has a reason before the log loads; latest event per tenant | `scenario.ts`, `headline.ts`, `reason.ts` |
| `src/mock/sim.test.ts` | 7 | 12 contract-shaped tenants; OFF hurts victims more than Industry; TenantGuard walks throttle → quarantine and recovers to factor 1; the case story is told once in order ending with `explained`; rescue route weights in TenantGuard only; no innocent flagged; validation like the lab and gateway | `MockPlatform` |
| `src/mock/handoff.test.ts` | 2 | one case per acted-on incident with owner, pending action, IST deadline and evidence link; a plain warn opens no case | `mockCases` |

**Key decisions**
- Each behaviour test was also shown to fail when the rule it protects was deliberately broken (e.g. mapping throttle to amber, dropping the naming-stage rule, dropping the incident-evidence rule for innocents).

---

## End-to-end suite (`ui/e2e`, Playwright)

Proves the running system from a user's point of view. System Edge (`channel: 'msedge'`), headless, one worker, 1920×1080 by default. Every effect is checked through the backend's own API, not only the UI.

How to run (from `ui/README.md`):

```
npm run e2e                     # mock stack: Playwright starts scripts/mock-ws-server.mjs and Vite on :5174 itself
E2E_UI_URL=http://127.0.0.1:5174 E2E_GATEWAY_URL=http://<host>:8000 E2E_BRAIN_URL=http://<host>:8200 \
  E2E_LAB_URL=http://<host>:8300 E2E_HANDOFF_URL=http://<host>:8400 E2E_NARRATOR_URL=http://<host>:8700 \
  E2E_CONTROL_TOKEN=dev npm run e2e     # a real stack (start a Vite pointed at it first)
E2E_LIVE=1 ...                  # also the @live scenario (host only)
```

On a real stack, state-changing tests (mode, quota, attacks) are skipped unless `E2E_ALLOW_WRITES=1`, and they are also skipped while the lab reports a scenario run in progress. Anything changed is restored. `E2E_SCREENSHOTS=1` also writes the layout screenshots to `ui/screenshots/` (optional `E2E_SCREENSHOT_PREFIX`, e.g. `mock` or `host`).

### ui/playwright.config.ts
| Key | Value | Why |
|---|---|---|
| `testDir` / `outputDir` | `e2e` / `e2e-results` (git-ignored) | |
| `workers`, `fullyParallel`, `retries` | 1, false, 0 | the laptop running it has little free memory; no hidden flakiness |
| `timeout`, `expect.timeout` | 60 s, 10 s | |
| `reporter` | `list` + JSON to `e2e-results/results.json` | |
| `grepInvert` | `/@live/` unless `E2E_LIVE=1` | the live scenario runs only on the host |
| `use` | `channel: 'msedge'`, headless, `baseURL` = UI URL, 1920×1080, trace and screenshot on failure | |
| `webServer` (mock only) | `node scripts/mock-ws-server.mjs` (`MOCK_QUIET=1`, ready on gateway `/healthz`) and `npx vite --port 5174 --strictPort` with `VITE_MOCK=0` and every `VITE_*_URL` pointed at the mock ports | process env wins over `ui/.env`, so the UI talks to the mock stack in real (non-mock) mode |

### ui/e2e/env.ts
| Function / class | Signature | What it does | Used by |
|---|---|---|---|
| `MOCK_PORTS` | `{ ui: 5174, gateway: 18000, brain: 18200, lab: 18300, handoff: 18400, narrator: 18700 }` | mock stack ports | config, `URLS` |
| `MOCK` | `boolean` | true when `E2E_UI_URL` is not set (Playwright starts the mock stack) | config, `ALLOW_WRITES` |
| `URLS` | `{ ui, gateway, brain, lab, handoff, narrator, token }` | `E2E_*_URL` / `E2E_CONTROL_TOKEN` or the mock defaults (token `dev`), trailing `/` stripped | every spec |
| `TOKEN_HEADER` | `{ 'X-TenantGuard-Control-Token': token }` | control-token header | specs, fixtures |
| `ALLOW_WRITES`, `WRITES_OFF` | `boolean`, skip message | writes allowed on the mock, or on a real stack with `E2E_ALLOW_WRITES=1` | ui, live specs |
| `apiKey` | `(tenant) => string` | demo key `tk_<ID>_demo` | api spec |
| `probe` | `(url, headers?) => Promise<{ status, body } \| null>` | GET with a 4 s timeout; `null` when unreachable | fixtures |
| `until` | `<T>(what, fn, timeoutMs = 10000, everyMs = 500) => Promise<T>` | poll until truthy or throw | specs |

### ui/e2e/fixtures.ts
| Function / class | Signature | What it does | Used by |
|---|---|---|---|
| `Services` | `{ lab, handoff, narrator: boolean; labRun: string \| null }` | which optional services answer, and a lab scenario run in progress | specs |
| `test.services` | worker-scoped fixture | probes `/lab/status`, `/cases` (with token) and `/reports` once per worker | specs, `consoleGuard` |
| `test.consoleGuard` | automatic fixture | fails every test on an uncaught page error or a console error; only failed loads to an optional service known to be down are tolerated; problems are attached to the report | every spec |
| `expect` | re-export | | specs |

### Specs
| File | Tests | Covers |
|---|---|---|
| `e2e/api.spec.ts` | 10 | gateway: healthz; 401 without an API key; 401 on admin without the token; 200 with `X-TenantGuard-Cpu-Ms`; 12 admin tenants with barcodes and quotas. Brain: `/api/state` is a valid `StateSnapshot`; `/api/incidents` is a list of `IncidentEvent`s. Optional services: lab status, handoff `CaseView`s, narrator `ReportView`s (skipped cleanly when unreachable) |
| `e2e/ui.spec.ts` | 10 | 12 tiles + 3×3 grid + status sentence; tile → barcode highlight (vs `GET /admin/tenants`); mode toggle round-trip (vs `GET /admin/mode`, restored); launcher offers exactly the ten attacks, switches to a server picker (w0–w8 present, no tenant) and "Launch on w4" for `faulty_server`, and back to tenants and "Launch on K" for whale; attack launch → running → Stop (vs `/lab/status`); quota PATCH and client-side validation (restored); timeline → Case File (vs `GET /api/incidents?incident_id=`); Scorecard vs `/lab/results` (or says why not); flow view paints and toggles back; Cases handoff column (or "not reachable") |
| `e2e/layout.spec.ts` | 6 (3 × 2 sizes) | at 1366×768 and 1920×1080: panels never overlap and nothing spills out of its box (no sideways page scroll); the status bar keeps its height and the headline is shown in full (at most two lines, not clipped, no ellipsis, badge not empty); every tile with a non-normal badge shows `why`, and the screen is captured |
| `e2e/live.spec.ts` | 1 (`@live`) | real detection loop on the host: calm → whale on K → a K incident within 60 s → stop → K back to normal at full quota within 90 s. Skipped without writes allowed, without the lab, or during a lab scenario run |

The specs declare 27 tests: 26 run by default and the `@live` one only with `E2E_LIVE=1`.

---

## Scripts (`ui/scripts`)

### ui/scripts/mock-ws-server.mjs
Stand-in brain + gateway + lab + handoff bridge + narrator driven by `src/mock/sim.ts`, speaking the real wire contracts over HTTP and WebSocket with CORS. Lets the UI run in real mode (`VITE_MOCK=0`) without the services, exercises the actual fetch/WebSocket code, and is the default target of the Playwright suite. Ports: brain 18200, gateway 18000, lab 18300, bridge 18400, narrator 18700 (flags `--brain`, `--gateway`, `--lab`, `--handoff`, `--narrator`, `--no-script`); bind host from `MOCK_HOST` (default `0.0.0.0`); `MOCK_QUIET=1` silences the request and incident log. Like the gateway, the control token (`X-TenantGuard-Control-Token`, env `TENANTGUARD_CONTROL_TOKEN`, default `dev`) is required only on `/admin` and `/control` paths (401 without it). Requires Node ≥ 23.6 (runs the `.ts` modules directly).

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `arg` | `(name, def) => number` | CLI port flags | module |
| `send` | `(res, status, body, extra = {}) => void` | JSON response with CORS headers (incl. the token header) plus extra headers | `serve` |
| `Reply` | `class Reply { status; body; headers }` | lets a route return a status other than 200 or extra headers | gateway `/api/*`, `serve` |
| `readJson` | `(req) => Promise<object>` | body parse; invalid JSON → `SimError(422)` | `serve` |
| `serve` | `(name, port, routes, { tokenPaths = [] })` | tiny router: OPTIONS, token check on the given path prefixes, regex routes (method or `ANY`), `Reply` or 200 JSON, `SimError` → status, 404 | brain/gateway/lab/bridge/narrator |
| brain routes | `GET /api/state`, `GET /api/incidents?incident_id=&tenant=&limit=`, `GET /healthz`, `WS /ws/events` | snapshot, filtered incidents, health, stream (latest snapshot on connect) | UI |
| gateway routes | `GET/POST /admin/mode`, `GET /admin/tenants`, `GET /admin/tenants/{id}`, `PATCH /admin/tenants/{id}/quota`, `GET /admin/overrides`, `GET /healthz`, `ANY /api/*` | admin API (token required); tenant traffic needs `X-API-Key: tk_<A-L>_demo` (401 otherwise) and answers 200 with `X-TenantGuard-Cpu-Ms` and `X-TenantGuard-Server: w0` | UI, e2e api spec |
| lab routes | `GET /lab/status`, `POST /lab/attack`, `DELETE /lab/attack/{id}`, `GET /lab/results` | lab API (results = fixture) | UI |
| bridge routes | `GET /cases`, `GET /cases/{case_id}` | `mockCases` over the incident log | UI |
| `reportFor` | `(id) => ReportView \| null` | template `ReportView` from the incident's events once it reached named / verdict / closed / resolved / explained (headline, timeline, cause, evidence points, action taken, operator recommendation, customer message) | narrator routes |
| narrator routes | `GET /healthz`, `GET /reports`, `GET /reports/{incident_id}` (404 when none) | report list newest first, one report | e2e api spec |
| 2 Hz tick | `setInterval(..., 500)` | scripted whale on K after 4 s, `sim.tick`, logs incidents, broadcasts incidents then the snapshot to every WS client | module |

### ui/scripts/e2e.mjs
End-to-end check of the operator actions against whatever backend the UI points at (mock server or the real stack), driving Edge with `puppeteer-core` and verifying every effect through the backend's **own** API, not the UI. Args: `uiUrl gatewayUrl labUrl brainUrl handoffUrl`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `getJson` | `(url) => Promise<any>` | fetch with the control token | checks |
| `sleep`, `until` | helpers | wait / poll a condition with timeout | checks |
| `check` | `(name, ok, extra?)` | prints PASS/FAIL, counts failures | checks |
| checks (in order) | — | 12 tiles from the stream; status sentence; impact panel and innocents = 0; flow canvas actually paints, toggle back; 3×3 grid and tile → barcode highlight (vs `GET /admin/tenants`); timeline row → `/case/:id`, steps and full log vs `GET /api/incidents?incident_id=`, report or placeholder, proof replay when present; Cases bridge count and case id/owner vs `GET /cases` (or "not reachable"); Scorecard charts/table vs `GET /lab/results` (or error state); mode toggle and back (vs `GET /admin/mode`); attack launch + Stop (vs `GET /lab/status`); inline quota PATCH (vs `GET /admin/tenants`), confirmation, client-side rejection of invalid input, original quota restored; no uncaught page errors | `node scripts/e2e.mjs …` |

**Key decisions**
- On a shared stack the script never disturbs a measured run: mode toggles, attacks and quota edits are skipped while the lab reports a scenario run in progress, and any quota it edits is restored.

### ui/scripts/screenshot.mjs
Headless Edge screenshot at 1920×1080 via `puppeteer-core` with a real-time wait (args: `url out.png waitMs [clickSelector]`). Real time is used because `--virtual-time-budget` stalls on pages that keep polling a remote host.

### ui/scripts/sim-smoke.mjs
Runs `MockPlatform` headless through calm → whale on K → calm in each mode and prints the ladder, victim p95 and the status sentence every 10 s; a quick sanity check of the simulation in Node.

### ui/scripts/load.py
Modest traffic for checking the UI against a real stack before the lab existed: Poisson calm traffic for all 12 tenants (70 % `/api/light`, 25 % `/api/read`, 5 % `/api/search`) with the demo keys `tk_<ID>_demo`, plus an optional whale (`/api/search?n=…`, configurable concurrency) on one tenant.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `calm` | `async (client, tenant, rps, stop, stats) -> None` | exponential inter-arrival times, fire-and-forget requests | `main` |
| `one` | `async (client, path, headers, stats, tenant) -> None` | one request, counts status codes / errors | `calm`, `whale` |
| `whale` | `async (client, tenant, start, end, n, conc, stats) -> None` | heavy searches in `conc` loops between `start` and `end` | `main` |
| `main` | `async () -> None` | CLI args (`--gateway --rps --duration --whale --whale-at --whale-for --whale-n --whale-conc`), prints response-code totals every 10 s | `__main__` |

---

## Screenshots (`ui/screenshots`)

PNG captures used in the docs and slides. `mock-*` come from the mock stack, `host-*` and `*-host*` / `*-real-*` from the real Docker host stack.

| File | Shows |
|---|---|
| `host-control-room-1366x768.png`, `host-control-room-1920x1080.png` | Control Room on the host at both tested sizes (written by `e2e/layout.spec.ts` with `E2E_SCREENSHOTS=1`, prefix `host`) |
| `mock-control-room-1366x768.png`, `mock-control-room-1920x1080.png` | the same on the mock stack (prefix `mock`) |
| `mock-flow-view-whale-1920x1080.png` | Flow view during a whale on K (mock) |
| `control-room-mock.png`, `control-room-off-mode.png`, `control-room-host-load.png`, `control-room-real-brain.png`, `control-room-real-brain-idle.png`, `control-room-real-path.png` | earlier Control Room captures: mock, OFF mode, host under load, real brain (busy and idle), real request path |
| `flow-view-mock.png`, `flow-view-host-load.png` | Flow view on the mock and on the host under load |
| `case-file-mock.png`, `case-file-proving-mock.png`, `case-file-real-brain.png`, `case-file-server-incident-host.png` | Case File: mock, mock at the proving stage, real brain, a server incident on the host |
| `cases-handoff-mock.png` | Cases page with the handoff column (mock) |
| `tenants-real-path.png` | Tenants page on the real path |
| `scorecard-fixture.png` | Scorecard on the invented fixture (marked FIXTURE) |
