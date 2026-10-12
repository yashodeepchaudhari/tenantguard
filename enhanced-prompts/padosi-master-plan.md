# Padosi 2.0: Master Plan
**CODEASTRA 2.0 · Cloud Computing & Distributed Systems · PS 3: Multi-Tenant Isolation and Noisy-Neighbor Defense**
Team VisionGuardians · 4 members · 24 hours · Go + Python + React · 2 laptops + Azure VM

> **One line:** Padosi is a fair-share control plane for multi-tenant clouds. Every tenant gets its fair share, every slowdown is fixed in about a second, and every slowdown gets a name, a proof and a plain-English explanation.

---

## 1. The story in 30 seconds

Think of a housing society with one water tank. When one flat runs a huge motor, everyone's taps run dry.

Clouds work the same way: thousands of customers (**tenants**) share the same servers. Today's platforms stop only the obvious abuser, the one sending too many requests. Padosi handles the whole problem:

| Step | What Padosi does | Time |
|---|---|---|
| **1. Prevent** | Fair, cost-based limits for every tenant, including hidden background work | Always on |
| **2. Rescue** | Victims' traffic moves away from slow servers at once | ≈ 1 s |
| **3. Name** | The pattern of slow servers points to the tenant causing it (the "barcode") | ≈ 3 s |
| **4. Prove** | The suspect is moved to its own spare server. The slowdown follows it, and the victims stay healthy | ≈ 8 s |
| **5. Resolve** | The right action for the right cause: limit, isolate a request type, add capacity, or drain a faulty server | Instant |
| **6. Explain** | Evidence pack, fair billing with refunds, "Why was I slow?", and an AI-written incident report | ≈ 5 s |

---

## 2. Why Padosi is ahead of the industry

| Capability | Typical API gateway | Observability tools | Padosi |
|---|---|---|---|
| Per-tenant rate + **cost** limits | Rate only | ✗ | ✅ Cost-based, background work included |
| Fair scheduling + adaptive concurrency | Partial | ✗ | ✅ Weighted DRR + gradient limiter |
| Victim rescue in ~1 s | ✗ | ✗ | ✅ Shard route-around |
| Names the culprit automatically | ✗ | Manual | ✅ Barcode + ledger + confidence score |
| Proves it without slowing anyone | ✗ | ✗ | ✅ Relocation test ("move, don't punish") |
| Separates abuse / honest growth / faulty host | ✗ | Manual | ✅ Five verdicts |
| Victims never billed for the attack | ✗ | ✗ | ✅ Protected billing + refunds |
| Explains itself in plain English | ✗ | ✗ | ✅ Grounded AI incident narrator |
| Tenant data isolation | Partial | ✗ | ✅ Namespaced cache, DB per tenant, memory budgets |

**Built on proven engineering:** Google SRE (overload handling, criticality), AWS (shuffle sharding, load shedding, fairness), Microsoft 2DFQ, Stripe limiters, Netflix adaptive concurrency, Google CPI² (antagonist detection), group-testing theory. Padosi's contribution is the complete loop: **prevent → rescue → name → prove → resolve → explain**.

---

## 3. Physical setup (2 laptops + Azure VM)

```
     Judges' phones (QR)                 Public dashboard viewers
            │ HTTPS                               │
            ▼                                     ▼
   ┌──────────────────────── AZURE VM ─────────────────────────┐
   │  Caddy (auto-HTTPS)  →  Relay hub (WebSocket)             │
   │  /play page · public read-only dashboard                  │
   │  Standby: full Padosi stack (same Compose file)           │
   └───────────────────────────▲───────────────────────────────┘
                               │ outbound WebSocket (no inbound ports needed)
   ┌──────────── LAPTOP A: "PLATFORM" ──────────┐   wired LAN   ┌──────── LAPTOP B: "TRAFFIC LAB" ────────┐
   │ Go Gateway (pinned cores)                   │◄─────────────►│ 12 tenant traffic clients               │
   │ 9 Workers (AG(2,3)) + Spare pool + Quarantine│              │ 8 attack modes · Crash-test runner      │
   │ Python Brain (pinned core)                  │               │ Control Room dashboard (big screen)     │
   └─────────────────────────────────────────────┘               └─────────────────────────────────────────┘
```

- **Laptop A** runs only the platform. Gateway and brain are pinned to their own CPU cores, so the platform's measurements stay clean and precise.
- **Laptop B** generates all traffic and shows the dashboard on the projector, so load generation never competes with the platform.
- **Wired LAN** (a short Ethernet cable or a small switch) between the laptops gives stable, low-latency traffic.
- **Azure VM** provides a public HTTPS URL for the judges' phones and the public dashboard. Laptop A connects *out* to the VM, so venue firewalls don't matter. The VM also runs a full standby copy of the stack, which covers the "deployment" surprise challenge.

---

## 4. Architecture

### 4.1 Components

| Component | Language | Owner | Job |
|---|---|---|---|
| **Gateway** | Go | M2 | Identity, admission, fair scheduling, shard routing, telemetry, control API |
| **Workers W0–W8** | Python (FastAPI) | M3 | The shared "SaaS app": tenant-aware cache, DB per tenant, background jobs, black-box indexer |
| **Spare pool + Quarantine** | Same worker image | M3 | Relocation target, burst lane, heavy lane |
| **Brain** | Python | M1 | Detect → rescue → name → prove → classify → act → explain |
| **Narrator** | Python + Claude API | M1 | Grounded plain-English incident reports |
| **Traffic Lab** | Python asyncio | M3 | 12 tenant clients, 8 attacks, crash-test runner |
| **UI** | React + Vite + TS + Tailwind + Recharts | M4 | Control Room, Case File, Scorecard, Billing, /play |
| **Relay** | Python (FastAPI WebSocket) + Caddy on VM | M3 | Phones → VM → Laptop A |

### 4.2 Request path (data plane)

```
Client ──► GATEWAY (Go)
            1. Identify      API key → tenant + tier (+ JWT via convergence partner)
            2. Fingerprint   method + route template + param shape + size bucket → request class
            3. Admit         token bucket (rate) · cost bucket (reference cost of the class)
                             · per-tenant concurrency cap · retry budget (Idempotency-Key + body hash)
            4. Classify      interactive (default) or batch (header / route)
            5. Queue         per-tenant queues, deficit round robin weighted by tier and cost,
                             express lane for cheap requests, deadline-aware, bounded wait
            6. Dispatch      adaptive per-worker concurrency window (gradient limiter),
                             so the queue lives at the gateway and fairness is exact
            7. Route         tenant's 3 servers from its barcode, health-weighted
            8. Observe       per (tenant, server) latency histograms → brain every 250 ms
           ──► WORKER
            · tenant context set for the request and copied into every background job
            · cache keys namespaced by tenant; guaranteed share per tenant + shared pool
            · SQLite file per tenant; per-tenant memory and payload budget
            · handler CPU + background CPU + DB time reported per tenant
```

### 4.3 Control plane (brain loop, 4 Hz)

```
 Inputs: passive latency matrix (12 tenants × 9 servers) · representative canaries
         · worker stats (paid CPU, background CPU, black-box CPU, cache, evictions) · harm ledger
   │
   ▼
 HEALTH     each server compared with the median of its peers (peer ratio) + its own baseline
   │
   ▼
 RESCUE     slow server? → gateway shifts every tenant's traffic to its healthy shard members
            (capacity-guarded) · interactive first, batch shed on slow servers
   │
   ▼
 NAME       barcode decoder: tenants whose whole barcode is slow, plus "stuck" tenants who
            cannot route away, plus ledger evidence → ranked suspects with confidence %
            · servers no candidate explains → host suspects
   │
   ▼
 PROVE      relocation test: suspect moves to the spare (sized 1.25× its footprint)
            ✔ its old servers recover   ✔ the slowdown appears on the spare   → confirmed
            host suspects: drained, re-measured idle vs peers                 → host fault
   │
   ▼
 CLASSIFY   five verdicts (see 4.4)
   │
   ▼
 RESOLVE    action with TTL + auto-undo · action budget · kill switch · shadow mode
   │
   ▼
 EXPLAIN    evidence pack (hash chain) · billing adjustments · "Why was I slow?" · AI narrator
```

### 4.4 The five verdicts

| Verdict | How Padosi recognises it | Action |
|---|---|---|
| **Over-plan** | Metered usage above the plan | Interactive: fast reject + accurate `Retry-After` + `RateLimit` headers. Batch: queued at the plan rate within its wait budget. Upsell notice. |
| **Costly pattern** (hidden cost) | Confirmed suspect whose paid work explains < 35% of the load it caused, or heavy black-box / eviction evidence | Find the exact request class (class-level relocation) → move **only that class** to the heavy lane. Tenant's other traffic untouched. Bill the true cost. |
| **Honest growth** | Confirmed suspect whose own paid work explains the load | Keep it on the spare as a **burst lane** + add a worker. No penalty. Upgrade suggestion. |
| **Host fault** | Slow server that no tenant explains, still > 1.5× slower than peers when idle | Drain it, blame no tenant, open a host ticket. |
| **Mitigated, no blame** | Evidence below the confidence bar | Victims already rescued; incident logged with full data for review. |

### 4.4a Genuine? Important? (mentor feedback 1 and 3)

Every confirmed noisy tenant gets **two separate scores**, shown in the Case File.

**Genuineness score (0–100%): is it honest work?**

| Signal | Genuine | Not genuine (abuse / bug / stolen key) |
|---|---|---|
| Paid share: its own paid work explains the load | Yes | No, hidden work |
| Request types | Same classes as its own history, just more | New, unusual class appears |
| Growth shape | Gradual or at a predictable time | Sudden jump from nothing |
| Obeys `Retry-After` | Slows down | Retries harder |
| Sources | Usual sources/apps | New unknown sources (possible stolen key) |
| Event Calendar | Pre-announced ("Sale Friday 6–9 PM") | No notice |

**Importance level (Low / Medium / High / Critical): how much does it matter to the business?**
Signals: plan tier, monthly revenue (₹), SLA level, "protected account" flag, number of end users served.

**Event Calendar:** tenants pre-announce big events through `POST /events/calendar`. Padosi pre-allocates burst capacity for that window, and traffic inside the window counts as genuine.

**Decision matrix: the noisy tenant is never downscaled if it is important. It is moved and scaled.**

|  | **Important** | **Not important** |
|---|---|---|
| **Genuine** | 🟢 **VIP lane:** move to dedicated capacity (the spare), add a worker, alert the account manager, offer an upgrade. **Never throttled.** | 🟡 **Fair share:** exactly its plan; spare capacity if free; upgrade suggestion |
| **Not genuine** | 🟠 **Surgical fix:** isolate only the bad request class to the heavy lane, the rest keeps flowing, alert their engineers; **no full block without human approval** | 🔴 **Contain:** limit → quarantine → block, each with a timer |

**How the problem gets solved (mentor feedback 2): protect first, fix second, punish last.**
1. Rescue victims (always, ~1 s)
2. Give capacity (genuine growth)
3. Isolate only the bad request class (bug / costly pattern)
4. Shape batch work at the plan rate (over plan)
5. Limit with `Retry-After` (repeated overuse)
6. Quarantine with a timer (clear abuse)
7. Block, only with human approval (confirmed attack / stolen key)

Every step has a timer + auto-undo, and the tenant always receives the reason and a suggested fix. Each company can tune the matrix in `padosi.yaml` (example: "Enterprise: never throttle, only relocate").

### 4.5 Barcode layout: AG(2,3), 9 servers, 12 tenants

Every pair of tenants shares **at most one** server, so the union of any two tenants' barcodes never contains a third tenant's barcode. **Any two simultaneous culprits are named exactly.**

| Tenant | Servers | Tier | Tenant | Servers | Tier |
|---|---|---|---|---|---|
| A | W0 W1 W2 | Enterprise | G | W0 W4 W8 | Enterprise |
| B | W3 W4 W5 | Pro | H | W1 W5 W6 | Pro |
| C | W6 W7 W8 | Free | I | W2 W3 W7 | Free |
| D | W0 W3 W6 | Enterprise | J | W0 W5 W7 | Enterprise |
| E | W1 W4 W7 | Pro | K | W1 W3 W8 | Pro |
| F | W2 W5 W8 | Free | L | W2 W4 W6 | Free |

Tier weights: Enterprise 4 · Pro 2 · Free 1.

**Tenant personas (fictional businesses with realistic behaviour):**

| Tenant | Business | Tier | Behaviour |
|---|---|---|---|
| A | Aarogya Health (hospital app) | Enterprise | Morning peak, many small reads |
| B | BazaarKart (shopping) | Pro | Evening peaks, search-heavy |
| C | Chitra Studio (photo app) | Free | Uploads, bursty |
| D | DhanSetu Payments (UPI-style) | Enterprise | Steady, very latency-sensitive |
| E | Ekalavya Learn (ed-tech) | Pro | Class-time spikes |
| F | FitPadosi (fitness) | Free | Early-morning bursts |
| G | GaadiGo (ride booking) | Enterprise | Rush-hour peaks |
| H | HisaabBooks (accounting) | Pro | Month-end batch exports |
| I | IdliExpress (food delivery) | Free | Lunch and dinner peaks |
| J | JhatpatMart (quick commerce) | Enterprise | Huge sale-day spikes (uses the Event Calendar) |
| K | KaamKaro (jobs portal) | Pro | Bulk-write imports |
| L | LokVani News | Free | Breaking-news spikes |
Scaling layouts: STS(13) → 13 servers / 26 tenants · AG(2,5) → 25 servers / 30 tenants · AG(2,7) → 49 servers / 56 tenants. Beyond one layout, Padosi runs in **cells**, each cell an independent gateway + workers + brain (the cell architecture used by AWS and Shopify).

### 4.6 Why the rescue step also names the culprit

When a server slows down, every tenant shifts traffic to its other, healthy servers, and the victims recover within about a second. **Only the culprit has all three of its servers slow, so it has nowhere to go.** The rescue step therefore narrows down the culprit as it happens: mitigation and identification are the same action.

### 4.7 Fair billing

- Every request is charged its class's **reference cost** (learned during calm periods), not its contended cost. Victims never pay for an attack.
- Background work is tagged with its tenant and charged to that tenant.
- During an incident, victims' cost-bucket usage caused by measured queue wait is refunded live (Harm Ledger).
- Verdicts produce **SLA credits** for victims and a **true-cost line** for costly patterns.
- "Why was I slow?" gives each victim a clear answer, with the offender anonymised ("another tenant").

### 4.8 Tenant data isolation

- Cache keys always prefixed `tenant:{id}:`, enforced in the cache API itself.
- One SQLite file per tenant per worker (database-per-tenant pattern). The data-access layer opens only the caller's file.
- Per-tenant memory budget inside each worker (cache bytes + in-flight bodies) and a 1 MB body limit.
- API keys stored as SHA-256 hashes. Each tenant's billing view shows only its own data.

### 4.9 Padosi's own safety

- **Fail-static:** if the brain goes quiet for 2 s, the gateway keeps the last known-good policy and traffic keeps flowing.
- **Action budget:** ≤ 6 relocations/min, ≤ 1 quarantine per 30 s, ≤ 1 drained server at a time (when peers are < 75% busy) without human approval.
- **Every action has a TTL** and auto-undo, followed by probation.
- **Shadow mode + Approve button**, a kill switch, and a policy YAML (for example: Enterprise is relocated only, never throttled without approval).
- **Repeat-offender memory:** the same barcode firing 3× in 10 min → immediate relocation at the next onset.

---

## 5. Tech stack

| Layer | Choice |
|---|---|
| Gateway | Go 1.22+, `net/http` + `httputil.ReverseProxy`, `golang.org/x/time/rate` for reference, HDR-style histograms, `gorilla/websocket` or `nhooyr.io/websocket` |
| Workers | Python 3.12, FastAPI, uvicorn (1 process, thread pool), SQLite, `psutil`, `multiprocessing` for the black-box indexer |
| Brain | Python 3.12, asyncio, httpx, websockets, numpy, Docker SDK (`docker` package), Pydantic, PyYAML |
| Narrator | `anthropic` Python SDK, model `claude-opus-5-5`, structured output via Pydantic schema, prompt caching |
| Traffic Lab | Python asyncio + httpx (HTTP/1.1 keep-alive pools) |
| UI | React 18 + Vite + TypeScript + Tailwind + Recharts, WebSocket push |
| Infra | Docker Compose, cgroup v2 limits (`cpus`, `mem_limit`, `cpuset`), Azure VM (Ubuntu 22.04, Docker, Caddy) |
| Observability | Prometheus `/metrics` on gateway and workers (also feeds the convergence partner) |

---

## 6. Attack lab (8 modes) and the 3-mode scorecard

| Attack | What it does | OFF | Industry standard | Padosi |
|---|---|---|---|---|
| Flood | 10× request rate | All slow | ✅ Rate limit | ✅ Rate limit |
| Whale | Few, very heavy requests | All slow | Partial | ✅ Cost bucket + fair queue + express lane |
| Hog | Holds many workers | All slow | ✅ Concurrency cap | ✅ + adaptive window |
| Retry storm | Blocked clients retry harder | Collapse | Partial | ✅ Retry budget + `Retry-After` |
| Background bomb (tagged code) | Writes that trigger heavy background jobs | Victims slow | Misses it | ✅ **Prevented:** charged to the tenant |
| Black-box bomb | Triggers the uninstrumented vendor indexer | Victims slow | Misses it | ✅ Rescue + name + prove + heavy lane |
| Cache flood | Evicts everyone's cached data | Victims slow | Misses it | ✅ Guaranteed share + ledger names the evictor |
| Faulty server | Injected slow host | 4 tenants hit | Misses the cause | ✅ Drained, "host fault, no tenant blamed" |
| Honest spike | Enterprise sale day, within plan | All slow | Throttles the customer | ✅ Burst lane + new worker, no penalty |

**Modes (one toggle on the dashboard):**
- **OFF**: shared pool, FIFO, no limits.
- **Industry standard**: token bucket + concurrency caps + cgroups + fair queue + load shedding.
- **Padosi**: the full loop.

**Scorecard metrics (live, per attack, per mode):**
- victim SLO-violation-seconds
- time to rescue
- time to verdict
- culprit named (✔/✘)
- innocents penalised
- victim p99
- gateway overhead p50

---

## 7. Showcase features

### 7.1 Blind phone whodunit
A judge scans the QR code, secretly picks a tenant and an attack type, and presses **Start**. The phone talks to the VM, which forwards to Laptop A's Traffic Lab. Padosi rescues the victims, names the tenant with a confidence %, proves it on the spare, and shows the verdict. Then the judge's phone reveals their pick next to Padosi's answer.

### 7.2 Three-mode scorecard
The same attack is run in OFF, Industry and Padosi modes and shown side by side as live charts and a table. This answers "isn't this just rate limiting?" with numbers.

### 7.3 Fair billing + "Why was I slow?"
- Per-tenant cost (CPU, DB, background), SLA credits received, true-cost lines, refunds.
- Victim view: *"Between 14:02:10 and 14:02:12, another tenant's workload slowed 1 of your 3 servers. Your traffic was moved to healthy servers in 0.9 s. A 4% service credit has been applied."*

### 7.3a Top-notch simulation (mentor feedback 4)

1. **"Real system, simulated customers":** real containers, real limits, real servers. Only the customers are simulated. Say this sentence in every demo.
2. **Realistic traffic:** the 12 personas (§4.5) with random (Poisson) arrival gaps, a real mix of light and heavy requests, and daily peaks.
3. **"A day in the cloud" in 4 minutes:** a compressed 24-hour story: morning rush → lunch peak → JhatpatMart sale (Event Calendar) → a bug at 3 PM → a server failing at night.
4. **Live flow view:** dots (requests) travel from tenants to servers. Slow servers turn red, and the dots visibly move away during the rescue.
5. **Proof animation:** when the suspect moves to the spare, the red "smoke" follows it.
6. **₹ impact panel:** revenue protected, SLA credits avoided, customers saved from slowdown, innocents punished (0).
7. **Scenario Director:** judges combine events (for example, 2 attackers + a broken server) and watch Padosi handle them live.
8. **Statistics:** a 40-run blind drill before the final, with the real results on the slide.
9. **Fair comparison:** the same attack in OFF / Industry / Padosi, side by side.

### 7.4 AI incident narrator (Claude)
When a verdict is reached, the brain sends the evidence pack (JSON) to Claude (`claude-opus-5-5`) and gets back a structured report:
- headline
- timeline
- cause
- evidence points
- action taken
- operator recommendation
- a separate victim-safe customer message, generated from the anonymised evidence

Every tenant ID, server ID and number in the output is checked against the evidence before it is shown (**grounded narration**). A deterministic template report appears instantly, and the AI report replaces it within seconds.

---

## 8. UI

- **Control Room:**
  - One status sentence at the top ("All tenants healthy" / "Tenant J is slowing 3 servers, proving now, 92% confidence").
  - Left: 12 tenant tiles (name, tier, p95, status colour).
  - Centre: 3×3 server grid with live peer ratio; selecting a tenant lights its barcode.
  - Right: incident timeline (Detected → Rescued → Named → Proved → Verdict → Resolved → Explained).
  - Bottom: the 3-mode toggle.
- **Case File:** the three witnesses (barcode match, Harm Ledger graph "who made whom wait / who evicted whom", relocation chart showing the harm following the suspect to the spare), verdict, action, TTL countdown, evidence hash, AI report.
- **Scorecard:** OFF vs Industry vs Padosi, per attack.
- **Billing:** per-tenant costs, credits, true-cost lines, "Why was I slow?".
- **/play (phone):** QR → pick tenant → pick attack → Start → Reveal. No sign-up.
- **Design rules:** colour means status only (green / amber / red) plus one accent; large type; every number has a unit; motion only when it means something.

---

## 9. Team roles

| Member | Owns |
|---|---|
| **M1: Mayur (lead)** | Brain: health, rescue, decoder, relocation prover, classifier, actions, evidence, billing engine, AI narrator; crash-test runner logic (from H14) |
| **M2 (Go)** | Gateway: identity, fingerprinting, admission, DRR scheduler, adaptive limiter, shard router, telemetry stream, control API, `/metrics` |
| **M3** | Workers (tenancy, cache, store, jobs, black-box indexer, meters), Docker Compose + cgroups, spare/quarantine pool, Azure VM + Caddy + relay, Traffic Lab + 8 attacks |
| **M4** | UI (Control Room, Case File, Scorecard, Billing, /play), pitch deck + storyline, demo rehearsal lead |

Pairing points: M1↔M2 at H1 (control API contract) and H8 (rescue loop); M2↔M3 at H2 (gateway→worker headers); M1↔M4 at H10 (incident WebSocket events).

---

## 10. 24-hour plan (Day 1 10:30 → Day 2 10:30)

| Time | Hour | Goal | Done when |
|---|---|---|---|
| 10:30–11:30 | H0–H1 | Repo skeleton, the 5 JSON contracts (§12), Compose with 9 workers, AG(2,3) layout file, wireframes | `docker compose up` works for everyone; contracts committed |
| 11:30–13:30 | H1–H3 | Walking skeleton: Go gateway routes by barcode; workers serve 4 endpoints; Lab sends normal traffic; tiles show live p95 | Tiles move with real data across both laptops |
| 13:30–14:30 | H3–H4 | **Calibration run:** 20 min of calm traffic → record each server's peer-ratio spread and canary noise → write thresholds into `padosi.yaml` | Thresholds from real measurements committed |
| 14:30–15:30 | H4–H5 | Admission (token + cost bucket, concurrency), DRR, tenant context in workers | Flood + whale handled in Industry mode |
| 15:30 | H5 | **Mentoring 1** | Feedback noted |
| 16:30 | H6 | **Convergence round** with Web PS 3 (§13): agree on the contract, send 5 sample cases by H7, joint test at H9 | Partner integration live by H9 |
| 16:30–20:30 | H6–H10 | **Primary flow:** health → rescue (route-around) → decoder → relocation prover → classifier → actions; black-box bomb, faulty server, honest spike; 3-mode toggle | Black-box bomb: rescued, named, proved, resolved, end to end |
| 20:30 | H10 | **Mentoring 2:** show the primary flow live | Primary flow demoed |
| 20:30–22:30 | H10–H12 | Harm Ledger + cache flood; retry storm; drill #1 of the blind test (10 runs) | Ledger evidence in Case File |
| 22:30–00:30 | H12–H14 | **Surprise challenge** (§13) | Change shipped |
| 00:30–04:30 | H14–H18 | Billing + credits + "Why was I slow?"; genuineness/importance scores + decision matrix + VIP lane + Event Calendar; AI narrator; /play via VM relay; personas + "day in the cloud"; live flow view + proof animation + ₹ impact panel + Scenario Director; crash-test runner + scorecard; evidence hash chain; drills #2–#3 | All showcase features working |
| 04:30 | H18 | **Feature freeze** | Only fixes after this |
| 04:30–06:30 | H18–H20 | Final threshold check; 40-run blind-test drill (results go on the slide); record backup video | 10 clean demo runs in a row |
| 06:30–08:30 | H20–H22 | Final deck; 3 timed rehearsals; Q&A drill | Pitch < 5 min, demo < 4 min |
| 08:30–09:30 | H22–H23 | Submission: repo, README, architecture diagram, setup guide, AI-tool disclosure | Submitted |
| 09:30–10:30 | H23–H24 | Buffer, rest, final run-through | Ready |

---

## 11. Decision-engine parameters (`padosi.yaml`, calibrated at H3–H4)

| Parameter | Starting value | Meaning |
|---|---|---|
| `tick_ms` | 250 | Brain loop period |
| `probe_window` | 8 ticks | Median window for server readings |
| `slow_peer_ratio` | 1.6 | Server p95 ÷ median of peers' p95 |
| `slow_abs_ms` | +8 ms | Minimum absolute increase |
| `open_after_ticks` | 4 | Consecutive slow ticks before an incident opens |
| `rescue_capacity_guard` | 70% | Healthy servers must stay below this after the shift |
| `confidence_to_prove` | 0.6 | Minimum suspect confidence before the relocation test |
| `spare_sizing` | 1.25× footprint | Spare CPU set by `docker update --cpus` |
| `prove_settle_s / prove_window_s` | 2 / 6 | Relocation test timing |
| `recovery_R` | ≥ 0.5 (+ harm follows to the spare) | Confirmation rule |
| `paid_share_costly` | < 0.35 | Costly-pattern threshold |
| `host_idle_ratio` | > 1.5× peers | Host-fault confirmation |
| `ttl_quarantine_s / probation_s` | 120 / 300 | Action lifetimes |
| `retry_budget` | 10% | Retries allowed per tenant |
| `max_queue_wait_ms` | interactive 1000 · batch 5000 | Bounded queue waits |

---

## 12. The five contracts (written at H0, JSON)

1. **Telemetry** (gateway → brain, 4 Hz, WebSocket): `{ts, cells:[{tenant, server, count, p50_ms, p95_ms, errors, shed}], queues:[{tenant, depth, wait_ms}], limiter:[{server, window}]}`
2. **Worker stats** (worker → brain, 1 Hz, `GET /stats`): `{server, cpu_total_ms, blackbox_cpu_ms, tenants:[{tenant, handler_cpu_ms, bg_cpu_ms, db_ms, cache_hits, cache_misses, evictions_caused, evictions_suffered, bytes}]}`
3. **Control** (brain → gateway, `POST /control/*`): `route_weights`, `relocate {tenant, class?, target}`, `limits {tenant, rate, cost, concurrency}`, `lane {tenant, class, lane}`, `drain {server}`, `mode {off|industry|padosi}`. Every command has `ttl_s` and `incident_id`.
4. **Incident events** (brain → UI/relay, WebSocket): `{incident_id, stage, ts, suspects:[{tenant, confidence}], verdict, action, evidence_hash, report?}`
5. **Play** (phone → VM → lab): `{session, tenant, attack, start|stop}` and `{session, reveal}`

---

## 13. Hour-6 convergence and hour-12 surprise playbooks

**Convergence partner: Web PS 3, "Digital Handoff & Continuity Management Platform".** Their PS lists "Web + Cybersecurity + Cloud", so we are their official Cloud partner. Full contract: `convergence-web.md`.

- **Idea:** every Padosi incident becomes a case in their app, with an owner (on-call), context, pending actions and deadlines. When the on-call shift changes, their app makes the handoff complete and safe.
- **Web team builds for us:** (1) cases created from Padosi incidents; (2) Hand off / Acknowledge / Approve buttons that call Padosi.
- **Padosi builds for them:** (1) `POST {WEB_URL}/cases` on every incident change (context, pending actions with deadlines, history); (2) `POST /control/case-update` that receives the new owner + decisions (approve / reject / extend / release / resolve) and acts on them.
- **Joint demo:** attack → case appears, owned by Priya → handoff blocked ("1 approval pending") → Rahul acknowledges → Padosi shows "Owner: Rahul" → Rahul approves → Padosi fixes it live.
- **Safety:** each side keeps working if the other is down (Padosi's timers + auto-undo; their app keeps the cases).

**Surprise challenge (cloud):**
- **Scaling:** switch to the STS(13) layout (26 tenants) or start a second cell; the brain is cell-scoped.
- **Deployment:** the same Compose file goes live on the Azure VM behind Caddy HTTPS (already prepared as standby).
- **Cost:** a 6-server pair layout, spare pool scaled to zero when calm, and a per-tenant cost dashboard.

---

## 14. Pitch (5 min) and demo (4 min)

**Pitch**
1. **Hook (30 s):** "Your app is slow. Your code is fine. Your servers are fine. The problem is your *padosi*." Water-tank analogy.
2. **Pain (60 s):** shared platforms everywhere; Shopify rebuilt into isolated pods to contain noisy shops; slow-hardware incidents took days to months to find (FAST 2018); rate limits stop only the obvious abuser.
3. **Solution (90 s):** prevent → rescue → name → prove → resolve → explain. Pooled-test analogy for the barcode. "Move, don't punish."
4. **Proof (60 s):** blind-test drill results from H18–H20, 3-mode scorecard numbers.
5. **Business (60 s):** open-core gateway + paid control plane, priced per protected tenant. Customers: SaaS platforms, API companies, LLM-API providers (fair GPU share). Roadmap: Envoy filter, Kubernetes operator, eBPF/PSI signals, GPU token fairness.

**Demo**
1. **Calm (15 s):** 12 green tiles, 3×3 grid.
2. **OFF → Industry (45 s):** black-box bomb in Industry mode: the rate limiter sees nothing and victims go red.
3. **Padosi, blind (75 s):** a judge picks a tenant on their phone. Victims recover in about 1 s, the barcode lights up, the suspect moves to the spare and the slowdown follows it, the verdict appears, the heavy lane isolates only the guilty request type, and the judge reveals the pick.
4. **Faulty server (25 s):** drained, "host fault, no tenant blamed".
5. **Honest spike (25 s):** burst lane + new worker, no penalty.
6. **Explain (40 s):** AI incident report, victim's "Why was I slow?", credits in billing.
7. **Close (15 s):** "Every tenant gets its fair share. Every slowdown gets a name."

**Backup:** recorded video of the full demo; the VM standby stack; own hotspot + wired LAN.

---

## 15. Q&A preparation

| Question | Answer |
|---|---|
| Isn't this just rate limiting? | The scorecard shows it: Industry mode includes rate limiting and still misses the black-box bomb and cache flood. Padosi catches both. |
| Why not instrument everything? | We do, for our own code: background jobs are tagged and billed. The barcode covers components nobody can instrument, such as vendor engines, database internals and kernels. |
| How fast are victims helped? | Route-around rescues them within about a second, before the investigation even starts. |
| Doesn't testing hurt the suspect? | The suspect moves to a dedicated spare sized for it, so it usually gets faster. |
| How do you separate abuse from a busy customer? | Two scores: genuineness (paid share, request types, growth shape, Retry-After behaviour, sources, Event Calendar) and importance (tier, revenue, SLA). The decision matrix picks the action. |
| Important customer is the noisy one: downscale it? | Never. We move it to dedicated capacity (VIP lane) and scale it up. Others stay protected, and the extra capacity is billed or offered as an upgrade. |
| How do you solve it, not just detect it? | Protect first, fix second, punish last: rescue → capacity → isolate the bad request type → shape → limit → quarantine → block (human-approved), all with timers and auto-undo. |
| Is the simulation real? | Real system, simulated customers: real containers, limits and servers, with 12 realistic business personas and a 40-run blind drill. |
| Faulty machine? | Drain it, re-measure it idle against its peers, label it host fault, blame nobody. |
| Multiple culprits? | The layout guarantees exact naming for two at once. The decoder explains away one culprit at a time, and the relocation test confirms each. |
| Scale? | Bigger layouts (13 / 25 / 49 servers) and independent cells; probes and decoding grow linearly. |
| Data isolation? | Namespaced cache, database per tenant, memory and payload budgets, hashed keys. |
| Is the AI making decisions? | No. Decisions come from deterministic, auditable rules. Claude only writes the explanation, and every fact in it is checked against the evidence. |
| Cloud? | Running live on Azure behind HTTPS; the design maps to Kubernetes (gateway as a sidecar, brain as an operator). |
| Privacy? | Victims see "another tenant", never who. Fingerprints store shapes and hashes, never values. |

---

## 16. Pre-event checklist (setup only)

- [ ] Laptop A: Docker Desktop (WSL2), `.wslconfig` giving the VM most cores and ≥ 12 GB RAM; Go 1.22+; Python 3.12; pull `python:3.12-slim`, `golang:1.22-alpine`, `node:20-alpine`.
- [ ] Laptop B: Python 3.12, Node 20, Chrome.
- [ ] Ethernet cable / small switch; own hotspot tested with 5 phones.
- [ ] Azure for Students: Ubuntu 22.04 VM created, Docker + Caddy installed, DNS name label set, ports 80/443 open.
- [ ] Anthropic API key stored as a secret for the brain (`ANTHROPIC_API_KEY`).
- [ ] Practise (as skills): Go `ReverseProxy` + goroutines, FastAPI + WebSocket, React + Recharts, Docker SDK for Python.
- [ ] Contract templates and wireframes on paper; deck template ready.
