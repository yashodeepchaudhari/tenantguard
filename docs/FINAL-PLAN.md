# Final Plan: TenantGuard (Merged & De-risked)
**CODEASTRA 2.0 · Cloud Computing & Distributed Systems · PS 3: Multi-Tenant Isolation and Noisy-Neighbor Defense**
4 members · 24 hours

This plan merges two earlier plans:
- **Plan S:** the session plan (`docs/Architecture.pdf`): FastAPI + Redis + Postgres + Docker cgroups + graduated throttling.
- **Plan P:** the TenantGuard master plan (`enhanced-prompts/tenantguard-master-plan.md` + `tenantguard-build-prompt.md`): Go gateway + Python brain + barcode detection + relocation proof + five verdicts + many showcase features.

**Decision in one line:** use **TenantGuard's idea and architecture** (it is what wins) with **Plan S's scope discipline** (it is what finishes). Build in strict tiers, so that something complete and impressive exists at every hour.

---

## 1. Side-by-side comparison

| Dimension | Plan S (session) | Plan P (TenantGuard) | Final choice |
|---|---|---|---|
| **Core idea** | Quotas + fair queue + cgroups + detector + AIMD throttle | Prevent → rescue → name → prove → resolve → explain | **TenantGuard loop.** Plan S is what most teams will build. |
| **Detection** | Usage share vs fair share × victim impact (EWMA / z-score) | Shuffle-sharded "barcodes" (AG(2,3)) + peer ratio + relocation test | **TenantGuard barcode.** It names the culprit even when it can't be measured directly (black-box work). Keep Plan S's share-based score as a second signal. |
| **Response to a noisy tenant** | Warn → throttle → deprioritize → quarantine | "Move, don't punish" + five verdicts + decision matrix | **TenantGuard ordering**, plus Plan S's explicit throttle ladder as the "contain" path. The PS literally asks for *adaptive throttling*, so it must be visible. |
| **"Not just a rate limit" proof** | CPU-ms budget catches the "few but heavy" attack | Black-box bomb + 3-mode scorecard (OFF / Industry / TenantGuard) | **3-mode scorecard.** This is the strongest possible answer to the judges' constraint. |
| **Gateway language** | Python FastAPI | Go | **Go if M2 has shipped Go before; otherwise Python.** Hard switch rule at H3 (see §9). |
| **State store** | Redis (Lua token buckets, Streams) | In-memory in the gateway | **In-memory.** There is one gateway, so Redis adds a moving part for no demo benefit. Put Redis on the roadmap slide for multi-gateway. |
| **Data isolation** | Postgres + Row-Level Security | SQLite file per tenant + namespaced cache | **SQLite per tenant + namespaced cache.** Simpler and isolated by construction. |
| **Compute isolation** | Docker cgroups + penalty-box container | 9 workers + spare + quarantine, `docker update --cpus` | **TenantGuard's**, with a 6-worker fallback layout if the laptop can't hold 9. |
| **Observability** | Prometheus + Grafana + React | Prometheus `/metrics` + React | **React dashboard primary**, `/metrics` exposed, Grafana optional. |
| **Admin API** | Explicit tenant / quota CRUD, isolation toggle | Control API (brain → gateway), mode toggle; **no tenant / quota admin** | **Add the admin API.** It is a listed PS deliverable and TenantGuard is missing it. |
| **Showcase extras** | None | Phone play via Azure, incident narrator, billing / refunds, ₹ panel, flow animation, Scenario Director, hash chain, Event Calendar, 40-run drill | **Tiered** (§3). Most are COULD items: built only after the core is frozen and stable. |
| **Infra** | One laptop | 2 laptops + wired LAN + Azure VM | **2 laptops (cheap and valuable).** The Azure VM is a COULD item, except as a standby for the "deployment" surprise challenge. |
| **Timeline realism** | Realistic | Overloaded: H14–H18 holds about 15 features in 4 night hours | **Re-timed** (§8), with feature freeze at H17. |
| **Risk** | Low risk, low wow | High wow, **high risk of nothing working end to end** | Tiered scope removes the risk without losing the wow. |

### Plan P's strengths (keep these)
1. **Barcode naming.** It is mathematically sound: AG(2,3) means every two tenants share at most one server, so any two culprits are named exactly. It is also what makes the demo look "magic".
2. **The relocation test** gives *proof* of the culprit, not just a guess, and it doesn't punish anyone while it runs.
3. **The black-box bomb attack** beats every rate-limiter-based solution in the room.
4. **The 3-mode scorecard** answers "isn't this just rate limiting?" with numbers.
5. **Five verdicts** (abuse vs honest growth vs faulty host) show real-world maturity.
6. **Safety design:** fail-static gateway, TTL + auto-undo, action budget, shadow mode.
7. **Calibration** comes before thresholds are set, so false positives stay low.

### Plan P's weaknesses (fix these)
1. **Scope is about 3× what four people can finish in 24 hours.** Fix: MUST / SHOULD / COULD tiers with hard gates.
2. **Two languages** add contract-drift risk. Fix: JSON schemas + shared fixture tests + a Python-gateway fallback rule.
3. **No tenant / quota admin API** (a PS deliverable). Fix: add `/admin/*`.
4. **"Adaptive throttling" is hidden behind "move, don't punish".** Fix: show an explicit throttle ladder + an adaptive concurrency limiter on the dashboard.
5. **Too many external dependencies at demo time** (Azure, phones, venue Wi-Fi). Fix: every one of them has an offline fallback, and the core demo needs none of them.
6. **Two convergence documents** (Web Handoff and Cyber Identity). Fix: build one thin `integrations/` adapter interface and pick the partner at H6.
7. **A 40-run drill needs about 2+ hours of machine time.** Fix: 20 runs, with the crash-test running unattended overnight on Laptop B.

### Plan S's strengths carried over
- A simple, explainable layered model (5 layers of isolation) for the slides.
- The explicit graduated throttle ladder (warn → throttle → deprioritize → quarantine → recover).
- A usage-share noisy score (share ÷ fair share × victim impact) as a second detection signal, so the system still works if the barcode signal is noisy.
- Jain's fairness index and utilization % as headline metrics.
- The "efficiency proof": a single tenant can burst to ~90% when nobody is hurt.

---

## 2. PS checklist → where each item lives (show this slide to the judges)

| PS expected item | Implemented by |
|---|---|
| Tenant identity | Gateway: `X-API-Key` (SHA-256) → tenant + tier; optional partner JWT |
| Resource quotas | Per-tier rate, cost (CPU-ms), concurrency, memory and payload budgets; DB per tenant |
| Rate limiting | Token bucket + cost bucket + retry budget, 429 + `Retry-After` |
| Workload scheduler | Weighted DRR per tenant, express lane, bounded wait, adaptive per-worker window |
| Usage metrics | Per (tenant, server) latency histograms; per-tenant handler / background / DB CPU; `/metrics` |
| Noisy-neighbor detector | Peer-ratio health + barcode decoder + usage-share score + relocation proof |
| Adaptive throttling | Gradient concurrency limiter + escalation ladder (limit → quarantine → block) with TTL / auto-undo |
| Admin API / dashboard | `/admin/*` (tenants, quotas, mode, actions, events) + Control Room UI |
| "Not just a rate limit" | 3-mode scorecard: OFF vs Industry (rate limit + cgroups + fair queue) vs TenantGuard |

---

## 3. Scope tiers (the most important section)

### 🟥 MUST: done and demoable by **H10** (Mentoring 2)
If only this exists, you can still place well.

1. **Layout:** 12 tenants on 9 workers (AG(2,3)) from `layouts/ag23.json`. Fallback: 6 tenants / 6 workers pair layout.
2. **Gateway:**
   - identity
   - token bucket + cost bucket + concurrency cap
   - 429 + `Retry-After`
   - weighted DRR
   - barcode routing with weights
   - telemetry stream (250 ms)
   - control API: `route_weights`, `relocate`, `limits`, `drain`, `mode`
   - fail-static
3. **Workers:**
   - `read`, `search` (CPU), `write` (+ tagged background job), `ingest` (→ black-box indexer process), `canary`
   - per-tenant CPU metering
   - SQLite per tenant
   - `/stats`
   - `/admin/fault`
4. **Spare + quarantine containers.** The brain resizes the spare with `docker update --cpus`.
5. **Brain, primary flow:** health (peer ratio) → **rescue** (route-around) → **decoder** (barcode + stuck signal) → **relocation prover** → **3 verdicts** (costly pattern / honest growth / host fault) → action with TTL.
6. **Mode toggle:** OFF / Industry / TenantGuard.
7. **Admin API:** `GET/POST /admin/tenants`, `PATCH /admin/tenants/{id}/quota`, `POST /admin/mode`, `GET /admin/events`.
8. **Traffic lab:** 12 calm tenants (Poisson) + attacks `flood`, `whale`, `blackbox_bomb`, `faulty_server`, `honest_spike`.
9. **UI Control Room:**
   - status sentence
   - 12 tenant tiles (p95)
   - 3×3 server grid (peer ratio)
   - incident timeline
   - suspect confidence bars
   - mode toggle

**H10 gate:** the black-box bomb in Industry mode turns victims red, and in TenantGuard mode it is rescued → named → proved → resolved, end to end, 3 times in a row.

### 🟧 SHOULD: H10–H17
10. **Escalation ladder + safety:**
    - limit → quarantine → block (block only with approval)
    - action budget
    - shadow mode + Approve button
    - kill switch
    - repeat-offender memory
11. **Adaptive concurrency limiter** (gradient) per worker, with its window shown on the grid.
12. **Five verdicts:** add `over_plan` + `mitigated_no_blame`.
13. **Case File page:** barcode match, relocation chart (original servers recover / spare absorbs the harm), verdict, TTL countdown, and a **template** incident report.
14. **Scorecard:** `crashtest.py` runs 5 attacks × 3 modes × ~5 runs. The table and bar chart include victim SLO-violation-seconds, time to rescue, time to verdict, culprit correct, innocents penalised, and Jain's fairness index.
15. **Cache isolation:** namespaced cache + guaranteed share + eviction ledger + `cache_flood` attack; `retry_storm` attack + retry budget.
16. **Genuineness / importance, simplified:** 3 signals (paid share, new request class, obeys `Retry-After`) + tier, feeding the 4-cell decision matrix. Important + genuine is never throttled.
17. **Incident narrator:**
    - structured `IncidentReport` built from the evidence pack
    - victim-safe customer message
    - runs off the decision path
18. **Convergence adapter:** `integrations/handoff.py` *or* `integrations/identity.py`, picked at H6. Each is 2 calls and fails safe.

### 🟩 COULD: only after the freeze is stable, on a separate branch
19. Phone `/play` blind test (served locally on LAN first; Azure relay only if time allows).
20. Billing view: per-tenant cost, SLA credits, "Why was I slow?".
21. ₹ impact panel, live request-flow canvas, proof "smoke" animation.
22. Event Calendar, Scenario Director, "Day in the cloud" script.
23. Evidence hash chain, harm-ledger refunds.
24. Azure VM standby stack (prepare *before* the event; it covers the deployment surprise).

### ⬛ CUT (roadmap slide only)
STS(13) / AG(2,5) layouts and cells, Redis-backed multi-gateway, Postgres RLS, Envoy filter, Kubernetes operator, eBPF / PSI, GPU fairness, a 40-run drill (do 20), two convergence partners (pick one).

---

## 4. Final architecture

```
             Traffic Lab (Laptop B)                       Judges / projector
   12 tenant clients · attacks · crash-test               Control Room UI (Laptop B)
                     │ HTTP (wired LAN)                          ▲ WebSocket
                     ▼                                           │
 ┌────────────────────────────── LAPTOP A: PLATFORM ────────────┼──────────────────┐
 │                                                              │                  │
 │  GATEWAY (Go | Python fallback)  ── pinned cores             │                  │
 │   1 Identify   API key → tenant, tier                        │                  │
 │   2 Fingerprint request class                                │                  │
 │   3 Admit      token · cost(CPU-ms) · concurrency · retry    │                  │
 │   4 Queue      weighted DRR · express lane · bounded wait    │                  │
 │   5 Dispatch   adaptive per-worker window (gradient)         │                  │
 │   6 Route      tenant barcode (3 servers), health weights    │                  │
 │   7 Observe    (tenant × server) histograms ── 250 ms ──┐    │                  │
 │   /admin/*  /control/*  /metrics                        │    │                  │
 │         │                                               │    │                  │
 │         ▼                                               ▼    │                  │
 │  ┌───────────── WORKERS W0..W8 (Docker, 0.5 CPU / 256 MB) ─┐ BRAIN (Python, 4 Hz)│
 │  │ tenant contextvar · SQLite per tenant · namespaced cache│  HEALTH  peer ratio │
 │  │ background jobs charged to tenant                       │  RESCUE  route-around│
 │  │ black-box indexer (unattributed CPU)                    │  NAME    barcode +   │
 │  │ /stats  /canary  /admin/fault                           │          usage share│
 │  └─────────────────────────────────────────────────────────┘  PROVE   relocate → │
 │  ┌──────── SPARE (resizable) ───┐ ┌──── QUARANTINE / HEAVY ┐   spare            │
 │  │ relocation test · burst lane │ │ contained tenants/class│  CLASSIFY 5 verdicts│
 │  └──────────────────────────────┘ └────────────────────────┘  ACT  ladder + TTL  │
 │                                                                EXPLAIN template │
 │                                     docker.sock ◄── resize ──  + incident narrator│
 │                                                                     │           │
 └─────────────────────────────────────────────────────────────────────┼───────────┘
                                                                       ▼
                                                  integrations/: partner team (H6)
```

**Planes:**
- **Data plane:** gateway + workers + spare + quarantine.
- **Control plane:** brain.
- **Management plane:** admin API + UI.

**Isolation layers:** request (L1) → cost (L2) → scheduler (L3) → kernel cgroups (L4) → data (L5), plus the **adaptive loop** on top.

---

## 5. Final tech stack

| Layer | Choice | Notes |
|---|---|---|
| Gateway | **Go 1.22+** (`net/http`, `httputil.ReverseProxy`, `nhooyr.io/websocket`) | Fallback: Python FastAPI + httpx if not routing by H3 |
| Workers | Python 3.12, FastAPI, uvicorn, SQLite, psutil, `multiprocessing` | Sync handlers, so `time.thread_time()` = request CPU |
| Brain | Python 3.12 asyncio, httpx, websockets, numpy, pydantic, PyYAML, `docker` SDK | All thresholds in `tenantguard.yaml` |
| Traffic lab | Python asyncio + httpx | Crash-test writes JSON for the Scorecard |
| UI | React 18 + Vite + TypeScript + Tailwind + Recharts | WebSocket push from the brain |
| Infra | Docker Compose, cgroup v2 (`cpus`, `mem_limit`, `cpuset`) | Pre-pulled images, vendored deps |
| Contracts | `contracts/*.schema.json` + shared fixtures | Go structs and Pydantic models tested against the same fixtures |

---

## 6. Repository layout

```
tenantguard/
  docker-compose.yml           docker-compose.lab.yml      tenantguard.yaml
  layouts/ag23.json            layouts/pairs6.json          # fallback
  contracts/*.schema.json      contracts/fixtures/*.json
  gateway/   cmd/gateway  internal/{identity,fingerprint,admission,sched,limiter,router,telemetry,control,admin}
  worker/app/  main.py routes.py tenancy.py cache.py store.py jobs.py blackbox.py meter.py stats.py
  brain/tenantguard_brain/  main.py ingest.py health.py rescue.py decoder.py prover.py classifier.py
                       actions.py policy.py events.py docker_ctl.py narrator.py profile.py
  brain/tests/
  integrations/  handoff.py  identity.py            # pick one at H6
  lab/  tenants.py attacks/ crashtest.py scenarios.yaml
  ui/src/pages/  ControlRoom.tsx CaseFile.tsx Scorecard.tsx   (Billing.tsx, Play.tsx = COULD)
  docs/
```

---

## 7. Team roles

| Member | MUST (H0–H10) | SHOULD (H10–H17) |
|---|---|---|
| **M1: Brain lead** | ingest, health, rescue, decoder, prover, 3 verdicts, actions + TTL | ladder + safety, 5 verdicts, genuineness matrix, narrator |
| **M2: Gateway** | identity, admission, DRR, router, telemetry, control API, fail-static, **admin API** | adaptive limiter, retry budget, express lane, Industry-mode parity |
| **M3: Workers + infra + lab** | worker endpoints, metering, black-box, SQLite, Compose + cgroups, spare / quarantine, calm traffic + 5 attacks | cache isolation + ledger, cache_flood / retry_storm, crash-test runner, convergence adapter |
| **M4: UI + story** | Control Room (tiles, grid, timeline, confidence, mode toggle) | Case File, Scorecard, deck, demo script, rehearsal lead |

Pairing points:
- **M1↔M2 at H0:** control + telemetry contracts.
- **M2↔M3 at H1:** headers + routing.
- **M1↔M4 at H4:** incident event contract.
- **M1↔M3 at H8:** relocation via `docker_ctl`.

---

## 8. 24-hour timeline (Day 1 10:30 → Day 2 10:30)

| Time | Hour | Goal | Gate (done when) |
|---|---|---|---|
| 10:30–11:30 | H0–H1 | Repo, 5 contracts + fixtures, Compose with 9 workers, `ag23.json`, UI wireframe | `docker compose up` runs on both laptops |
| 11:30–13:30 | H1–H3 | **Walking skeleton:** gateway routes by barcode → workers → lab calm traffic → tiles show live p95 | Live tiles from real data |
| 13:30 | H3 | **Language checkpoint:** Go gateway routing + streaming telemetry? If not, switch to the Python gateway now | Decision made |
| 13:30–14:15 | H3–H4 | **Calibration:** 15 min of calm traffic → thresholds into `tenantguard.yaml`; run a quiet test | No false incidents in 5 calm minutes |
| 14:15–16:30 | H4–H6 | Admission + DRR + Industry mode; attacks flood, whale, blackbox_bomb; admin API | Industry stops flood / whale, **fails on the black-box bomb** (that's the point) |
| 15:30 | H5 | Mentoring 1 | Notes captured |
| 16:30 | H6 | **Convergence:** pick the partner, agree on the 2 calls, send sample payloads | Contract agreed |
| 16:30–20:30 | H6–H10 | Brain: health → rescue → decoder → prover → 3 verdicts → actions; faulty_server, honest_spike | **H10 gate (§3)** |
| 20:30 | H10 | Mentoring 2: live demo of the primary flow | |
| 20:30–22:30 | H10–H12 | Ladder + safety + adaptive limiter; Case File (template report); convergence joint test | Ladder visible on the dashboard |
| 22:30–00:30 | H12–H14 | **Surprise challenge** | Shipped |
| 00:30–03:30 | H14–H17 | Scorecard + crash-test; cache flood + retry storm; genuineness matrix; narrator; 5 verdicts | Scorecard shows real numbers |
| 03:30 | **H17** | **Feature freeze.** Tag `v1-demo`. COULD items only on a branch, merged only if they pass the demo script | |
| 03:30–06:30 | H17–H20 | 20-run drill (unattended on Laptop B), threshold re-check, bug fixes; COULD items in parallel by whoever is free; **record the backup video** | 10 clean demo runs in a row |
| 06:30–08:30 | H20–H22 | Deck with real numbers, 3 timed rehearsals, Q&A drill | Pitch < 5 min, demo < 4 min |
| 08:30–09:30 | H22–H23 | Submission: repo, README, architecture diagram, setup guide | Submitted |
| 09:30–10:30 | H23–H24 | Buffer / rest | |

**Sleep rule:** two people sleep 2 hours each in shifts between H17 and H20, so the crash-test keeps running.

---

## 9. Hard rules (scope control)

1. **No new feature after H17.** Bugs only.
2. **Each hour, each owner says one sentence:** "my gate is green / red". Red for 2 hours means you cut the next feature in your column.
3. **Go fallback:** if the Go gateway isn't routing + streaming telemetry at H3, rewrite it in Python (FastAPI + httpx) — that takes about 1 hour, and the contracts stay the same.
4. **9 → 6 workers:** if calibration shows a peer-ratio spread > 1.3 at calm, switch to `pairs6.json`.
5. **Never claim a number you didn't measure.** Every slide number comes from the crash-test JSON.
6. **Offline-first:** the core demo needs no internet. The narrator, Azure and phones are bonuses with fallbacks.

---

## 10. Demo script (4 min)

1. **Calm (15 s):** 12 green tiles, 3×3 grid, utilization ~30%, fairness index ~0.99.
2. **OFF (30 s):** flood → everyone red. "This is the noisy-neighbor problem."
3. **Industry (45 s):** the flood is handled ✅. Now the **black-box bomb**: the rate limiter sees nothing and the victims turn red. "This is where every rate-limiter solution stops."
4. **TenantGuard (75 s):** same bomb.
   - Victims recover in about 1 s (rescue).
   - The barcode lights up and the suspect is named with a confidence %.
   - It moves to the spare and the slowdown follows it (proof).
   - Verdict: costly pattern. Only the guilty request class goes to the heavy lane.
5. **Faulty server (20 s):** drained, "host fault, no tenant blamed".
6. **Honest spike (20 s):** an Enterprise tenant on sale day gets a burst lane, with no throttling.
7. **Scorecard + explain (25 s):** the OFF / Industry / TenantGuard table, then the incident report.
8. **Close (10 s):** "Every tenant gets its fair share. Every slowdown gets a name."

Backups: the recorded video; the 6-worker layout; the Python gateway; the template reports.

---

## 11. Top risks & mitigations

| Risk | Mitigation |
|---|---|
| Laptop noise causes false detections | Calibration, peer ratio (not absolute), `open_after_ticks`, quiet-run gate, pinned cores |
| Go ↔ Python contract drift | JSON schemas + shared fixtures tested on both sides at H1 |
| Docker Desktop resource limits | `.wslconfig` gives most cores and ≥ 12 GB RAM; 6-worker fallback |
| Brain bug mid-demo | Fail-static gateway; TTL auto-undo; kill switch |
| Venue Wi-Fi down | Wired LAN between laptops; own hotspot; narrator template; local `/play` |
| Scope creep at night | H17 freeze, COULD branch, hourly gate check |
| Judges say "it's just rate limiting" | The Industry-mode column of the scorecard *is* rate limiting, and it loses |

---

## 12. Before the event (setup only)

- [ ] Decide who is M2 and whether they are fluent in Go (this decides §9 rule 3 in advance).
- [ ] Laptop A: Docker Desktop + `.wslconfig`, Go, Python 3.12; pre-pull `python:3.12-slim`, `golang:1.22-alpine`, `node:20-alpine`.
- [ ] Laptop B: Python 3.12, Node 20, Chrome. Ethernet cable / switch. Hotspot tested.
- [ ] Practise: Go `ReverseProxy`, FastAPI WebSocket, Recharts, Docker SDK `container.update`.
- [ ] Paper copies of the contract templates and the UI wireframe; deck template.
- [ ] Optional: Azure VM with Docker + Caddy ready as the deployment standby.
