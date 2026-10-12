## Enhanced Implementation Prompt

### Objective
Build **Padosi**, a multi-tenant isolation and noisy-neighbour defense platform. A Go gateway enforces fair, cost-based per-tenant admission and scheduling. A Python brain detects interference, rescues victims within about a second, names the culprit with shuffle-sharded "barcodes", proves it with a relocation test, picks one of five verdicts, acts with auto-undo, and explains every incident (fair billing, "Why was I slow?", grounded Claude-written reports). A React dashboard shows it live, including a blind phone test and an OFF / Industry / Padosi scorecard.

### Context
- **Tech stack:**
  - Gateway: Go 1.22+ (`net/http`, `httputil.ReverseProxy`, WebSocket library).
  - Workers, brain, traffic lab, relay: Python 3.12 (FastAPI, uvicorn, asyncio, httpx, psutil, numpy, pydantic, PyYAML, `docker` SDK, `anthropic` SDK).
  - UI: React 18 + Vite + TypeScript + Tailwind + Recharts.
  - Infra: Docker Compose with cgroup v2 limits; Azure Ubuntu VM with Caddy.
- **Deployment topology:**
  - Laptop A runs gateway, workers, spare/quarantine pool and brain.
  - Laptop B runs the traffic lab and the dashboard.
  - The Azure VM runs Caddy, the relay (phones → Laptop A via an outbound WebSocket from Laptop A) and a standby copy of the full stack.
- **Greenfield repository.** Create this layout:
  ```
  padosi/
    docker-compose.yml            # platform (Laptop A / VM)
    docker-compose.lab.yml        # traffic lab + UI (Laptop B)
    padosi.yaml                   # thresholds, tiers, policy, action budget
    layouts/ag23.json             # 9 servers, 12 tenants (table below)
    layouts/sts13.json            # 13 servers, 26 tenants
    contracts/*.schema.json       # the 5 JSON contracts
    gateway/                      # Go
      cmd/gateway/main.go
      internal/identity/          # API key (sha256) → tenant, tier; optional JWT verify
      internal/fingerprint/       # method + route template + param-shape + size bucket → class hash
      internal/admission/         # tokenbucket.go, costbucket.go, concurrency.go, retrybudget.go
      internal/sched/             # per-tenant queues, weighted DRR, express lane, deadlines, bounded wait
      internal/limiter/           # adaptive per-worker concurrency window (gradient)
      internal/router/            # barcode routing, health weights, relocation/lane overrides, drain
      internal/telemetry/         # per (tenant,server) histograms, 250 ms WebSocket stream, /metrics
      internal/control/           # POST /control/* with TTL handling, mode switch, fail-static
    worker/app/                   # Python FastAPI
      main.py routes.py tenancy.py cache.py store.py jobs.py blackbox.py meter.py stats.py
    brain/padosi_brain/
      main.py ingest.py health.py rescue.py decoder.py prover.py classifier.py actions.py
      ledger.py evidence.py billing.py narrator.py policy.py events.py docker_ctl.py
    brain/tests/
    lab/                          # tenants.py, attacks/*.py, crashtest.py, scenarios.yaml, play_bridge.py
    relay/                        # FastAPI WebSocket hub + Caddyfile (VM)
    ui/src/pages/                 # ControlRoom.tsx CaseFile.tsx Scorecard.tsx Billing.tsx Play.tsx
  ```
- **Related patterns:**
  - Every cross-service message follows a JSON schema in `contracts/`.
  - Every tunable value lives in `padosi.yaml`, never hard-coded.
  - Every control action carries `incident_id` and `ttl_s`.
  - Python uses Pydantic models for all contract types. Go uses structs with JSON tags that mirror the same schemas.

### Requirements

**A. Tenancy and layout**
1. Twelve tenants A–L with tiers and weights (Enterprise 4, Pro 2, Free 1) and the AG(2,3) barcodes below, loaded from `layouts/ag23.json`:
   A{0,1,2} Ent · B{3,4,5} Pro · C{6,7,8} Free · D{0,3,6} Ent · E{1,4,7} Pro · F{2,5,8} Free · G{0,4,8} Ent · H{1,5,6} Pro · I{2,3,7} Free · J{0,5,7} Ent · K{1,3,8} Pro · L{2,4,6} Free.
2. Workers W0–W8 run with `cpus` and `mem_limit` from config (default 0.5 CPU / 256 MB). The gateway and brain are pinned with `cpuset` to dedicated cores.
3. A spare pool (default 1 container whose CPU the brain resizes with `docker update --cpus`) and a quarantine/heavy-lane worker, both from the same worker image.

**B. Gateway (Go)**
4. Identity: the `X-API-Key` header is SHA-256 hashed and mapped to tenant + tier. 401 if unknown. Optional `Authorization: Bearer <JWT>` verification behind a config flag (convergence).
5. Fingerprint: class hash = method + route template + sorted parameter keys + body-size bucket. Track the top-N classes per tenant (heavy-hitter counting).
6. Admission, in this order:
   - Token bucket (rate + burst per tier).
   - Cost bucket debited with the class's **reference cost**. The reference is an EWMA of measured cost, updated only when no incident is open; it is reported back by workers and pushed by the brain.
   - Per-tenant concurrency cap.
   - Retry budget (≤ 10% of a tenant's requests). A request counts as a retry if it repeats an `Idempotency-Key` or matches (tenant, class, body-hash) within 10 s.
7. Rejections return 429 with an accurate `Retry-After` plus the `RateLimit-Policy` / `RateLimit` headers.
8. Request class: `X-Padosi-Class: batch` or a route listed in config marks a request batch; everything else is interactive.
9. Scheduler:
   - Per-tenant queues served by deficit round robin, weighted by tier, with quantum in cost units.
   - Express lane for classes whose reference cost is below the configured threshold.
   - Bounded wait: interactive 1000 ms, batch 5000 ms. Expired requests are dropped with 503 and never forwarded.
   - Honours an `X-Request-Deadline` header.
10. Dispatch through a per-worker adaptive concurrency window (gradient limiter: `new = old × (minRTT / sampleRTT) + sqrt(old)`, clamped to [2, 64]), so queueing happens at the gateway.
11. Router:
    - Sends each request to one of the tenant's 3 servers using weights (default equal).
    - Applies brain overrides: per-tenant weights, tenant or class relocation to a target, class → lane, server drain.
    - Overrides expire at their TTL.
12. Telemetry: per (tenant, server) count, p50, p95, errors and shed; queue depth and wait per tenant; limiter window per server. Streamed every 250 ms on `GET /internal/telemetry` (WebSocket), plus Prometheus `/metrics`.
13. Control API: `POST /control/{route_weights|relocate|limits|lane|drain|mode|reference_costs}`.
    - `mode` switches between `off` (FIFO, no limits, no routing intelligence), `industry` (rules 6–10 only, static equal weights) and `padosi` (everything).
    - If no control heartbeat arrives for 2 s, the gateway keeps its last known-good config (fail-static).
14. Proxy overhead target: p50 < 2 ms at the demo load.

**C. Workers (Python)**
15. `tenancy.py`: a middleware reads `X-Padosi-Tenant` (set by the gateway, rejected if it comes from outside the gateway network) into a `contextvars.ContextVar`. `jobs.py` copies the context into every background job and measures job CPU with `time.thread_time()`, charged to that tenant.
16. Endpoints (synchronous handlers, so `time.thread_time()` measures per-request CPU):
    - `GET /api/read` (cache → DB)
    - `GET /api/search` (CPU-heavy)
    - `POST /api/write` (DB write + tagged background indexing)
    - `POST /api/ingest` (feeds the **black-box indexer**)
    - `GET /api/export` (batch)
    - `GET /canary/{cpu|cache|db}` (representative probes)
17. `blackbox.py`: a separate `multiprocessing` process (the "vendor indexer") whose CPU is **not** attributed to tenants. Its CPU is reported only as `blackbox_cpu_ms`, via psutil on the child process.
18. `cache.py`:
    - LRU with keys enforced as `tenant:{id}:{key}`.
    - Per-tenant guaranteed share (config, default 1/8 of capacity) plus a shared pool.
    - Each eviction is recorded as (evictor_tenant → victim_tenant, bytes).
19. `store.py`: one SQLite file per tenant per worker (`/data/{tenant}.db`), seeded at start. The repository API takes the tenant from the context only.
20. Limits: 1 MB body limit and a per-tenant in-worker memory budget (cache bytes + in-flight bodies). Exceeding it returns 413/507 for that tenant only.
21. `GET /stats` (1 Hz) returns contract 2. The worker also reports per-class measured cost for reference-cost learning.
22. Fault injection: `POST /admin/fault {latency_ms, cpu_burn}` (lab use only, admin token) simulates a faulty host.

**D. Brain (Python)**
23. `ingest.py`: consumes gateway telemetry (WebSocket), polls worker `/stats` at 1 Hz, sends canaries at 4 Hz, and keeps a rolling 60 s state.
24. `health.py`: per server, `peer_ratio = p95(server) / median(p95 of other servers)` over an 8-tick median, plus a calm-only EWMA baseline. Slow if `peer_ratio ≥ slow_peer_ratio` and the increase is `≥ slow_abs_ms`. An incident opens after `open_after_ticks` consecutive slow ticks.
25. `rescue.py`: when an incident opens, compute new route weights that move every tenant's share away from slow servers onto its healthy servers. Respect `rescue_capacity_guard` by limiting the shift so projected healthy-server utilisation stays under the guard. Prioritise interactive traffic and shed batch on slow servers. Push the result via `/control/route_weights`.
26. `decoder.py`:
    - Map each server's peer ratio to p_s with a logistic function.
    - Score each tenant with:
      - barcode coverage (min and mean of p_s over its 3 servers)
      - a "stuck" signal (it cannot route away because all its servers are slow)
      - ledger evidence: evictions caused share, bg_cpu growth versus its own baseline, and black-box CPU growth on all 3 of its servers
    - Combine into a softmax confidence across candidates plus "host fault" and "none".
    - Explain-away loop: accept the top candidate, remove the slowness it explains, recompute. Supports 2 culprits.
    - Servers left unexplained become host suspects.
27. `prover.py`, relocation test:
    - For a suspect at confidence ≥ `confidence_to_prove`, size the spare to `spare_sizing × footprint` with `docker_ctl.py`, where footprint = paid CPU + unexplained CPU on its servers.
    - Relocate the suspect, wait `prove_settle_s`, then measure for `prove_window_s`.
    - Compute R = 1 − (post slowdown / pre slowdown) on its original servers.
    - Confirm when R ≥ `recovery_R` **and** the spare shows the harm followed (spare unexplained/black-box CPU or latency rose).
    - Host suspects run in parallel: drain (within the action budget) and confirm host fault if, while idle, the server is > `host_idle_ratio` × the peer median.
28. `classifier.py`: the five verdicts.
    - **over_plan**: metered usage above plan.
    - **costly_pattern**: paid share < `paid_share_costly`, or black-box/eviction evidence dominant. Then rank the tenant's classes (cost growth, ledger, newly seen) and run a class-level relocation to find the guilty class.
    - **honest_growth**: paid work explains the load.
    - **host_fault**.
    - **mitigated_no_blame**: confidence below the bar.
28a. `profile.py`, genuineness and importance (mentor feedback):
    - **Genuineness score 0–100** = weighted sum (weights in `padosi.yaml`) of: paid share; request-class similarity to the tenant's own 7-day/calm history (new class → lower); growth shape (ramp vs. step jump); `Retry-After` obedience (retries after 429 within the advised time → lower); source novelty (share of requests from sources not seen before → lower); active Event Calendar window (→ high).
    - **Importance level** Low / Medium / High / Critical, from tenant profile fields in `layouts/ag23.json`: `tier`, `monthly_revenue_rs`, `sla` (99.9 / 99.95 / 99.99), `protected` (bool), `end_users`.
    - **Decision matrix** (`policy.matrix` in `padosi.yaml`), genuine = score ≥ 60, important = High or Critical:
      - genuine + important → `vip_lane` (relocate to dedicated spare capacity + scale out one worker + notify account manager + upgrade offer; never throttled)
      - genuine + not important → `fair_share` (plan limits; spare capacity if idle; upgrade suggestion)
      - not genuine + important → `surgical` (class-level heavy lane; rest of the traffic untouched; full block only with human approval)
      - not genuine + not important → `contain` (limit → quarantine → block, each with TTL)
    - Both scores and the chosen matrix cell are added to the evidence pack and shown in the Case File.
28b. **Event Calendar:** `POST /events/calendar {tenant, start, end, expected_multiplier}` and `GET /events/calendar`. Padosi pre-sizes the spare/burst lane 2 minutes before the window, and traffic inside the window counts as genuine.
29. `actions.py`: maps verdicts and matrix cells to actions, escalating in this order: rescue → capacity → class isolation → batch shaping → limit → quarantine → block (block needs human approval).
    - costly_pattern → class → heavy lane
    - honest_growth → keep on spare as burst lane + scale out one worker
    - over_plan → tighten limits / batch shaping
    - host_fault → keep drained
    - Every action has a TTL and auto-undo followed by probation.
    - Enforce the action budget (≤ 6 relocations/min, ≤ 1 quarantine per 30 s, ≤ 1 drained server and only when peers are < 75% busy).
    - Shadow mode (recommend only, with an Approve endpoint), a kill switch, a policy YAML per tier, and repeat-offender memory (same barcode 3× in 10 min → immediate relocation).
30. `ledger.py`: a queue-wait harm graph (wait charged to the tenants ahead in the same DRR round) plus cache evictions. Drives live refunds: victims' cost-bucket usage from measured incident wait is refunded through `/control/limits`.
31. `evidence.py`: an evidence pack per incident (readings, suspects + confidence, test results, verdict, action), appended to a SHA-256 hash chain, with an export endpoint.
32. `billing.py`:
    - Per-tenant cost (handler, background, DB) at reference prices.
    - SLA credits for victims, proportional to SLO-violation-seconds.
    - True-cost lines for costly patterns.
    - "Why was I slow?" records per victim, with the offender anonymised.
33. `events.py`: a WebSocket broadcast of contract 4 for every stage (detected, rescued, named, proving, verdict, resolved, explained), plus a full state snapshot at 2 Hz for the UI.
34. `narrator.py` (Claude):
    - Use the official `anthropic` SDK with model `claude-opus-5-5`, `output_config={"effort": "low"}`, and structured output via `client.messages.parse(..., output_format=IncidentReport)`.
    - `IncidentReport` fields (Pydantic): `headline`, `timeline: list[str]`, `cause`, `evidence_points: list[str]`, `action_taken`, `operator_recommendation`.
    - A second call on the **anonymised** evidence returns `CustomerMessage {message}`.
    - Put the stable system prompt first, with `cache_control={"type": "ephemeral"}`. The system prompt instructs: use only facts in the evidence JSON; write plain English for a non-expert.
    - Run asynchronously off the decision path.
    - Grounding check: every tenant ID, server ID and number in the output must appear in the evidence. If the check fails, or `stop_reason == "refusal"`, or the API errors, show the deterministic template report.
    - Show the template instantly and replace it with the AI report when it arrives.
    - Decisions are never made by the narrator.

**E. Traffic lab (Python)**
35. Twelve tenant clients as **personas**: A Aarogya Health, B BazaarKart, C Chitra Studio, D DhanSetu Payments, E Ekalavya Learn, F FitPadosi, G GaadiGo, H HisaabBooks, I IdliExpress, J JhatpatMart, K KaamKaro, L LokVani News. Each has:
    - Poisson arrivals
    - its own request mix and daily-peak curve
    - profile fields (revenue ₹, SLA, protected, end users)

    Rates are calibrated so the calm fleet sits at about 30% utilisation.
35a. **"Day in the cloud" mode:** a 4-minute compressed 24-hour script in `scenarios.yaml`: morning rush → lunch peak → JhatpatMart sale (with an Event Calendar entry) → a bug at 3 PM (costly pattern) → a faulty server at night.
35b. **Scenario Director API:** `POST /lab/scenario {events:[{at_s, type, tenant?, server?, intensity}]}` lets judges combine events (for example, 2 attackers + a faulty server).
36. Attacks, each started and stopped by API: `flood`, `whale`, `hog`, `retry_storm`, `background_bomb` (tagged path), `blackbox_bomb`, `cache_flood`, `faulty_server` (via `/admin/fault`), `honest_spike` (8× within plan). Each takes `tenant`, `intensity` and `duration` parameters.
37. `crashtest.py`: runs `scenarios.yaml` × N runs × modes (off / industry / padosi) with random culprits. Records victim SLO-violation-seconds, time to rescue, time to verdict, culprit correct, innocents penalised, victim p99 and gateway overhead. Writes JSON results that the Scorecard reads.
38. `play_bridge.py`: an outbound WebSocket client to the VM relay. It receives play commands (contract 5), starts the chosen attack, and reports results back for the reveal.

**F. Relay (VM)**
39. A FastAPI WebSocket hub.
    - Phones connect to `/play` (served static from the UI build).
    - Laptop A's lab bridge and brain event stream connect outbound with a shared token.
    - The hub forwards play commands and broadcasts a read-only dashboard stream.
    - Caddy terminates HTTPS using the VM's Azure DNS name.

**G. UI (React)**
40. **Control Room**:
    - Status sentence.
    - 12 tenant tiles (name, tier, p95 ms, status colour).
    - 3×3 server grid showing peer ratio; selecting a tenant highlights its barcode.
    - Incident timeline with stage timestamps.
    - Confidence bar per suspect.
    - Mode toggle (OFF / Industry / Padosi).
    - Shadow-mode Approve button and kill switch.
41. **Case File**: barcode match, harm graph (who made whom wait / who evicted whom), relocation chart (original servers recovering vs. spare absorbing the harm), verdict, action, TTL countdown, evidence hash, AI report (template → AI swap).
42. **Scorecard**: OFF / Industry / Padosi per attack (table + bar charts) from crash-test JSON and live runs.
43. **Billing**: per-tenant cost breakdown, credits, true-cost lines, "Why was I slow?" per victim.
44. **Play** (mobile-first): pick tenant → pick attack → Start → Reveal, showing the judge's pick next to Padosi's verdict. No sign-up.
45. Design rules: colour only for status (green / amber / red) plus one accent; large type; units on every number.
46. **Live flow view** (canvas): request dots travel tenant → server, sampled from telemetry. Slow servers turn red, and dots visibly shift away during the rescue.
47. **Proof animation:** during the relocation test, red "smoke" moves from the suspect's original servers to the spare.
48. **₹ impact panel:** revenue protected (₹, from victims' `monthly_revenue_rs` × SLO-violation time avoided), SLA credits avoided, customers saved from slowdown, innocents punished.
49. **Case File** shows the genuineness score breakdown, the importance level and the decision-matrix cell.
50. **Scenario Director page** (judge-facing): build and launch combined scenarios through `/lab/scenario`.

**H. Convergence with Web PS 3 (Digital Handoff), see `convergence-web.md`**
51. `brain/padosi_brain/handoff.py`: on every incident change, `POST {WEB_URL}/cases` with `{case_id, title, severity, status, context:{verdict, affected, evidence_link, summary}, pending_actions:[{action_id, text, deadline}], history:[{time, event}]}`. Retry with backoff. Never blocks the decision loop.
52. `POST /control/case-update` on the brain accepts `{case_id, type:"owner", owner, time}` (shown as "Owner: <name>" on the incident timeline) and `{case_id, type:"decision", action_id, decision: approve|reject|extend|release|resolve, by}` (applied through `actions.py`, which honours the action budget).
53. Pending actions with no decision before their deadline follow the safe default: the TTL auto-undo, or the configured default action.

### Implementation Details
- **Build order** (each step demoable):
  1. Contracts + layout + Compose skeleton.
  2. Gateway pass-through routing by barcode + worker endpoints + lab calm traffic + tiles.
  3. **Calibration run** (20 min calm) → thresholds into `padosi.yaml`.
  4. Admission + DRR + limiter (Industry mode complete).
  5. Brain health → rescue → decoder → prover → classifier → actions (Padosi mode, primary flow: black-box bomb).
  6. Faulty server + honest spike.
  7. Ledger + cache flood + retry storm.
  8. Billing, evidence, narrator.
  9. Relay + /play.
  10. Crash-test runner + Scorecard.
- **Gateway concurrency model:** one goroutine per worker dispatcher pulling from the DRR scheduler under that worker's limiter window. The scheduler is guarded by a single mutex plus a condition variable. Histograms use fixed log buckets (1 ms to 10 s) and are swapped every tick.
- **Brain loop:** one asyncio task per input (telemetry WS, stats poller, canary prober) feeds a shared state object. A 250 ms tick task runs health → incident state machine → stage handlers. The relocation test runs as its own task per suspect, and drains run in parallel tasks.
- **Incident state machine:** `OPEN → RESCUED → NAMED → PROVING → VERDICT → RESOLVED → EXPLAINED → CLOSED`, with `MITIGATED_NO_BLAME` reachable from `NAMED` or `PROVING`.
- **Docker control:** the brain container mounts `/var/run/docker.sock`. `docker_ctl.py` wraps `container.update(cpu_quota=..., cpu_period=100000)` and scale-out (start a pre-created stopped worker).
- **Reference costs:** workers report per-class measured cost each second. The brain keeps the EWMA (calm periods only) and pushes the table to `/control/reference_costs` every 5 s.
- **Narrator call shape** (Python):
  ```python
  client = anthropic.Anthropic()
  resp = client.messages.parse(
      model="claude-opus-5-5",
      max_tokens=4000,
      output_config={"effort": "low"},
      system=[{"type": "text", "text": NARRATOR_SYSTEM, "cache_control": {"type": "ephemeral"}}],
      messages=[{"role": "user", "content": evidence_json}],
      output_format=IncidentReport,
  )
  report = resp.parsed_output
  ```
  Check `resp.stop_reason` before use. Catch `anthropic.RateLimitError`, `anthropic.APIStatusError` and `anthropic.APIConnectionError` (most specific first) and fall back to the template.
- **Errors:**
  - The gateway never crashes on a bad control command (it validates and returns 400).
  - The brain logs and skips a tick on missing data.
  - Workers return structured JSON errors `{error, tenant, detail}`.
- **Config reload:** `padosi.yaml` is hot-reloaded by the brain on change. The gateway receives the relevant parts through the control API.

### Edge Cases & Constraints
- **Two simultaneous culprits:** the explain-away decoder plus sequential relocation tests name both.
- **Three or more culprits:** the decoder iterates, the relocation test confirms each, and the action budget paces the actions.
- **Culprit pauses during the relocation test:** its original servers recover and the spare stays calm. The verdict uses the pre-test ledger plus repeat-offender memory, and the next onset triggers immediate relocation.
- **Pulsed attacks** (1.5 s on / 2.5 s off): the 8-tick median and onset-window measurement handle them.
- **Fleet-wide slowdown** (all servers slow together): peer ratios stay near 1, so no tenant is accused. Rescue sheds batch fleet-wide and raises an overload alert.
- **Rescue would overload healthy servers:** the shift is capped by `rescue_capacity_guard`, and the remaining load is handled by batch shedding.
- **Spare can't be sized to 1.25× footprint:** start a second spare container; relocation proceeds once capacity is ready.
- **Honest tenant on the spare:** it stays there as a burst lane. Scale-out adds a worker and the tenant returns after the TTL.
- **Changing fingerprints** (randomised payloads): fall back to tenant-level relocation to the heavy lane.
- **Stolen API key:** actions are reversible, the key owner is notified, and the key can be rotated through the admin API.
- **Brain down:** the gateway is fail-static with the last-known-good config. UI shows "control plane offline".
- **Narrator unavailable or ungrounded:** the template report is shown.
- **Relay/VM unreachable:** /play is also served locally from Laptop B on the LAN.
- **Requests past their deadline** are dropped, never processed.
- **Tenant data isolation:** cross-tenant cache or DB access is impossible by construction (namespaced keys, per-tenant files, tenant taken from context only).

### Testing
- **Go unit tests** (`go test ./...`):
  - Token and cost bucket refill/debit math.
  - DRR fairness: 3 tenants with weights 4/2/1 receive service within ±5% of weight share over 10k requests, including a whale.
  - Express lane never waits behind a whale.
  - Bounded-wait expiry.
  - Limiter window convergence on a synthetic RTT step.
  - Router weight normalisation and TTL expiry.
  - Retry-budget detection by Idempotency-Key and by body hash.
  - Fail-static after 2 s without heartbeat.
- **Python unit tests** (`pytest brain/tests`):
  - Layout property: every pair of AG(2,3) tenants shares ≤ 1 server, and for all 66 culprit pairs the decoder with clean signals returns exactly that pair.
  - Health peer-ratio math.
  - Rescue weights respect the capacity guard.
  - Recovery R and harm-follows check.
  - Classifier table tests for all five verdicts.
  - Action budget and TTL undo.
  - Ledger refund amounts.
  - Hash-chain verification.
  - Decision matrix: 4 table tests (one per cell), including "important + genuine is never throttled".
  - Genuineness score: an Event Calendar window raises the score; a new request class and source novelty lower it.
  - Handoff: `case-update` with an owner updates the incident; `approve` applies the pending action; an unknown `case_id` returns 404; the Web app being unreachable never blocks the brain loop.
  - Narrator grounding check (rejects an output that mentions an unknown tenant or number; accepts a valid one, using a stubbed client).
- **Worker tests:**
  - Cache key namespacing (tenant B can never read tenant A's key).
  - Per-tenant DB file isolation.
  - Background job charged to the correct tenant via contextvar copy.
  - Black-box CPU excluded from tenant attribution.
- **Scenario tests** (`lab/crashtest.py`): every attack × 20 runs in Padosi mode with random culprits.
  - Acceptance: culprit named correctly ≥ 95%, innocents penalised = 0, time to rescue ≤ 2 s, time to verdict ≤ 15 s, host fault drained ≤ 3 s, honest spike never penalised, quiet 5-minute run produces no incident.
  - The same scenarios in OFF and Industry modes produce the comparison numbers.
- **Chaos checks:** kill the brain mid-incident (traffic continues, actions expire by TTL); restart a worker (gateway routes around it); drop the relay (local /play works).

### Out of Scope
- Hypervisor/VM-level isolation, hardware cache partitioning, kernel modules or eBPF programs.
- Network-layer DDoS protection.
- Real payments (billing is computed and displayed only).
- OAuth / user sign-up flows (API keys + optional partner JWT only).
- Multi-region deployment and a production Redis-backed gateway cluster (roadmap slide only).
- Envoy filter and Kubernetes operator (roadmap slide only).
- Letting the AI narrator make or change any decision.
