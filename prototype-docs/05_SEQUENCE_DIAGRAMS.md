# 05 · Sequence diagrams

Nine flows, traced from the code on `master`. Every arrow names the real function or endpoint; config values come from `tenantguard.yaml` (or are marked *code default*). Units: `ts` / `*_at` are epoch seconds, `*_ms` are milliseconds, shares are 0..1.

| # | Flow | Main code |
|---|---|---|
| 1 | [Request path](#1-request-path) | `gateway/tenantguard_gateway/app.py`, `core.py`; `worker/app/main.py`, `meter.py` |
| 2 | [Admission refusal (429 / 503 / 401 / 413)](#2-admission-refusal) | `core.py`, `admission.py`, `buckets.py`, `retry.py`, `overrides.py` |
| 3 | [DRR dispatch](#3-drr-dispatch) | `core.py`, `scheduler.py`, `router.py`, `limiter.py` |
| 4 | [Detection → throttle ladder → recovery](#4-detection--throttle-ladder--recovery) | `brain/tenantguard_brain/runtime.py`, `core.py`, `score.py`, `ladder.py`, `incidents.py` |
| 5 | [Health → rescue → decoder naming](#5-health--rescue--decoder-naming) | `stage3.py`, `health.py`, `rescue.py`, `decoder.py`, `prover.py` |
| 6 | [Host fault](#6-host-fault) | `worker/app/fault.py`, Stage 3 path |
| 7 | [Operator / handoff decision](#7-operator--handoff-decision) | `integrations/handoff/tenantguard_handoff/*`, brain `/api/operator` |
| 8 | [Narrator report](#8-narrator-report) | `narrator/tenantguard_narrator/*` |
| 9 | [UI live stream](#9-ui-live-stream) | brain `main.py`, `runtime.py`, `core.py`; `ui/src/lib/*` |

---

## 1. Request path

A tenant request is identified by its API key, classified, admitted, queued per tenant, routed to one of the tenant's three barcode servers, metered by the worker and debited on the way back.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant P as Gateway app.py proxy
    participant G as Gateway core.Gateway
    participant R as tenants.Registry
    participant A as admission.admit
    participant S as Scheduler lanes
    participant W as Worker main.py
    participant M as worker meter.Meters
    participant T as telemetry.Telemetry

    C->>P: ANY /api/{path} + X-API-Key
    P->>G: handle(method, path, query, headers, read_body)
    G->>R: lookup(sha256(X-API-Key))
    alt unknown key
        G-->>C: 401 unauthorized
    end
    G->>G: safe_api_path, body limit 1 MiB (413), request_class = sha1(METHOD|path|keys|size)[:8]
    alt mode != off
        G->>G: RetryBudget.check(tenant, fingerprint)
        G->>A: admit(tokens, cost, pending, effective_quota, now, blocked)
        A-->>G: Decision(ok, reasons, retry_after_s)
        Note over G,A: refused -> 429, see flow 2
    end
    G->>T: admitted(tenant)
    G->>S: lanes[lane_for(path, class, ref_cost)].push(Item(tenant, cost, cls, Future))
    G->>G: _pump() picks a server (flow 3)
    G->>G: await Future up to max_queue_wait_ms (interactive 1000, batch 5000)
    G->>W: forward + X-TenantGuard-Tenant, X-TenantGuard-Class, X-Request-Id (client x-tenantguard-* and X-API-Key stripped)
    W->>W: TenantMiddleware: body limit, tenant header must be known, set tenant_var
    W->>W: handler wrapped by Worker.metered (fault latency first, then thread CPU)
    W->>M: record_request(tenant, cls, cpu_ms, bytes_in)
    W-->>G: response + X-TenantGuard-Cpu-Ms
    G->>G: cost.debit(cpu_ms) [mode != off], class_cost EWMA (alpha 0.2), limiter.observe
    G->>T: done(tenant, server, upstream_ms, e2e_ms, status, cpu)
    G-->>C: response + RateLimit, RateLimit-Policy, X-TenantGuard-Server, X-TenantGuard-Lane
    loop every gateway.tick_ms = 250 ms
        G->>G: tick(): Overrides.sweep, GradientLimiter.update, _pump
        G->>T: frame() -> TelemetryFrame to WS /internal/telemetry
    end
```

**Notes**
- The cost bucket is in CPU-ms: capacity `cost_ms_per_s × gateway.cost_bucket_s` (2 s), debited **after** the response with the worker-measured CPU, so it is a real resource quota rather than a request count.
- Workers trust only the gateway-set headers; the gateway strips any `x-tenantguard-*` header a client sends (after reading a client `X-TenantGuard-Class: batch` hint for lane choice).
- Background work (`POST /api/write` jobs) is charged to the tenant in `Meters.add_bg`, but it is not part of the `X-TenantGuard-Cpu-Ms` header, so the gateway does not debit it.
- Upstream errors return **502** `bad_gateway`. A missing CPU header debits 0 and is counted by `Telemetry.cpu_missing`.

---

## 2. Admission refusal

All dimensions are checked together; a token is consumed only if every dimension passes. `Retry-After` is the longest wait among the refusing dimensions.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Gateway core.Gateway
    participant RB as retry.RetryBudget
    participant O as overrides.Overrides
    participant A as admission.admit
    participant S as Scheduler

    C->>G: request (tenant identified)
    alt mode == off
        Note over G: no retry budget, no admission, no RateLimit headers
    else industry or tenantguard
        G->>RB: check(tenant, fingerprint, Idempotency-Key)
        alt retries > max(3, 0.1 x first attempts in 10 s)
            G-->>C: 429 reasons [retry_budget], Retry-After
        end
        G->>O: block_remaining(tenant) [tenantguard only]
        G->>A: admit(tokens, cost, pending, quota x LimitsCmd.factor)
        Note over A: blocked: wait = block remaining<br/>rate: TokenBucket level < 1, wait = (1 - level) / rate<br/>cost: CostBucket level < 0, wait = -level / rate<br/>concurrency: pending >= quota.concurrency, wait = 1 s
        alt any dimension refuses
            A-->>G: Decision(False, reasons, retry_after_s = max wait)
            G->>RB: failed(tenant, fingerprint, retry_after_s)
            G-->>C: 429 rate_limited, reasons, retry_after_ms<br/>Retry-After = max(1, ceil(retry_after_s)), RateLimit, RateLimit-Policy
        else admitted
            A-->>G: Decision(True), one token taken
        end
    end
    G->>S: push(item) and wait
    alt not dispatched within max_queue_wait_ms, or batch item shed
        G->>S: cancel(item)
        G-->>C: 503 queue_timeout or shed, Retry-After 1
    end
```

**Notes**
- `RateLimit-Policy` names one policy per dimension: `"rate";q=<burst>;w=…, "cost";q=<cost_ms_per_s×cost_bucket_s>;w=…;qu="cpu-ms", "concurrency";q=<concurrency>`; `RateLimit` gives the remaining amount and reset per dimension. Both are on admitted and refused responses in non-off modes.
- With an active `LimitsCmd` (tenantguard mode only) the effective quota is `max(1, round(base × factor))` for integer fields and `base × factor` for floats; bucket levels are kept and clamped to the new capacity.
- Other refusals: **401** for an unknown `X-API-Key`, and for `/admin/*` or `/control/*` without the right `X-TenantGuard-Control-Token` (`hmac.compare_digest`); **413** above `worker.body_limit_bytes` (checked by gateway and worker); **400** for a bad path or bad Content-Length.

---

## 3. DRR dispatch

Three lanes (express, interactive, batch), each a weighted deficit round robin over per-tenant queues; a per-worker adaptive window limits in-flight work.

```mermaid
sequenceDiagram
    autonumber
    participant G as core.Gateway
    participant L as lanes express / interactive / batch
    participant D as Scheduler._pop_drr
    participant R as router.choose
    participant GL as limiter.GradientLimiter (per worker)
    participant W as Worker

    G->>G: lane_for: off -> interactive, /api/export or class hint batch -> batch,<br/>ref_cost < express_cost_ms (5) -> express, else interactive
    G->>L: push(Item(tenant, cost = ref_cost))
    loop _pump(): lanes in order express, interactive, batch
        G->>D: pop(eligible = a server with slots < lane_cap)
        alt mode == off
            D-->>G: oldest eligible item (one global FIFO)
        else DRR
            D->>D: deficit[t] += quantum 5 ms x weight(t) once per turn
            D->>D: serve head if head.cost <= deficit, else advance cursor
            Note over D: weight = tier weight (4 / 2 / 1) x LimitsCmd.weight_factor (tenantguard)
            D-->>G: item
        end
        G->>R: choose(candidates(tenant), slot free)
        Note over R: tenantguard: relocation wins, else RouteWeightsCmd weights,<br/>drained servers skipped, never black-holes (falls back to the barcode)
        R-->>G: server
        G->>GL: note_inflight, slots[server] += 1
        G->>W: forward request
    end
    G->>G: _shed_batch(): cancel queued batch items when express/interactive wait and servers are at the interactive cap
    W-->>G: response
    G->>GL: observe(upstream_ms, cpu_ms)
    G->>G: _release: slots[server] -= 1, then _pump()
    loop every tick (250 ms), industry and tenantguard
        G->>GL: update(): sample = median(upstream - cpu), gradient = clamp(min_rtt / sample, 0.2, 1),<br/>window = 0.8 old + 0.2 (old x g + sqrt(old)), clamped [2, 64]
    end
```

**Notes**
- Item cost comes, in order, from brain-pushed `reference_costs[cls]` (tenantguard), the measured per-(tenant, class) EWMA, or `DEFAULT_CLASS_COST_MS` = 5 ms. Nothing on master sends `ReferenceCostsCmd`, so the EWMA or the default is used.
- Lane caps with window `w` (8 initially, `gateway.limiter`): express `w`, interactive `w − max(1, w//8)`, batch `max(1, min(interactive, w//2))`; batch is shed first.
- The limiter's RTT sample excludes the request's own CPU, so one heavy request does not shrink the window by itself.

---

## 4. Detection → throttle ladder → recovery

The brain ingests gateway telemetry (4 Hz) and worker stats (1 Hz), scores each tenant against its fair share on its own servers, walks a graduated ladder with hold times, and drives the gateway with TTL-bound `LimitsCmd`s.

```mermaid
sequenceDiagram
    autonumber
    participant GW as Gateway
    participant WK as Workers /stats
    participant RT as brain runtime.Runtime
    participant B as brain core.Brain
    participant SC as score.compute_scores
    participant LD as ladder.step
    participant IB as incidents.IncidentBook
    participant HUB as Hub (UI WS)

    RT->>GW: GET /admin/overrides (control token) -> Brain.adopt_overrides (retry every 2 s until ok)
    RT->>GW: WS /internal/telemetry
    loop every gateway frame (250 ms)
        GW-->>RT: TelemetryFrame
        RT->>B: ingest_frame(frame): baselines only while calm (half-life 60 s, skip completed == 0)
    end
    loop every brain.stats_poll_s = 1 s
        RT->>WK: GET /stats
        WK-->>RT: WorkerStats (cumulative)
        RT->>B: ingest_stats: diff counters (no diff after a restart)
    end
    loop every 0.5 s
        RT->>GW: POST /control/heartbeat Heartbeat(ts) + control token
    end
    loop every brain.tick_ms = 250 ms
        RT->>B: tick(now)
        B->>SC: cpu_share and queue_share on the tenant's servers vs fair share,<br/>victim_impact = neighbours' slowdown clipped [0, 3]
        SC-->>B: score = max(shares)/fair x (1 + impact), flagged = ratio > 1 and impact >= 0.2
        alt telemetry fresh and mode == tenantguard
            B->>LD: step(state, Signal(score, flagged, unhurt), now, LadderParams)
            Note over LD: normal -> warn: score >= 1.5 held 2 s<br/>warn -> throttle: >= 2.0 held 2 s, factor 0.5<br/>throttle step: factor x 0.5 down to 0.25<br/>-> deprioritize: held 4 s, weight_factor 0.25<br/>-> quarantine: >= 4.0 held 6 s, factor 0.1 / weight 0.1
            LD-->>B: Transition
            B->>IB: open_for(tenant) on the first escalation
            B->>HUB: IncidentEvent detected, then level_change (action + plain-English detail)
        else mode off or industry
            B->>B: ladder.reset to normal (observe only)
        else telemetry stale
            Note over B: ladders hold, commands keep being renewed
        end
        B->>B: _commands(now)
        alt acting (factor or weight_factor < 1)
            B->>RT: LimitsCmd(tenant, factor, weight_factor, ttl 60 s / 120 s quarantine) on change or renewal (ttl / 4)
        else back to 1.0 / 1.0
            B->>RT: explicit clear LimitsCmd(1.0, 1.0)
        end
        RT->>GW: POST /control/limits + control token (Sender: one in flight per tenant, newest wins)
        GW-->>RT: ControlAck (400 if factor outside (0, 1] or ttl outside (0, 3600])
        RT->>B: mark_sent(cmd, ok)
    end
    Note over B,LD: recovery: calm (score < 1.0 or unhurt) held 10 s,<br/>then +0.1 on both factors per second
    B->>HUB: level_change "Relaxed" ... resolved (back to full plan) ... closed (IncidentBook.close)
```

**Notes**
- Holds use `hold_ratio` 0.8: at least 80 % of the ticks in the hold window must qualify, including the current one. Every escalation restarts all hold clocks.
- Gateway-side safety: every override has a TTL that the gateway sweeps each tick and re-checks on read; `ttl_s` ≤ `gateway.ttl_max_s` (3600). If heartbeats stop for `fail_static_after_s` (2 s) the gateway keeps its last configuration and reports `fail_static: true` in `/admin/overrides`; TTLs still expire.
- Senders always send **both** factors; the latest `LimitsCmd` for a tenant replaces the previous one.
- **In progress:** quarantine is a 0.1 / 0.1 `LimitsCmd` on master. Relocating the tenant to the quarantine worker is a Wave 2 TODO in `ladder.py`.

---

## 5. Health → rescue → decoder naming

Stage 3 watches every server against its peers (traffic p95 and canary round trips), routes victims away from slow servers within a capacity guard, and names the cause by decoding which tenant's barcode explains the slow set.

```mermaid
sequenceDiagram
    autonumber
    participant GW as Gateway
    participant WK as Workers /canary
    participant RT as runtime.Runtime
    participant S3 as stage3.Stage3
    participant HT as health.HealthTracker
    participant RS as rescue.plan_rescue
    participant DC as decoder.decode
    participant IB as IncidentBook
    participant HUB as Hub (UI WS)

    loop every gateway frame (250 ms)
        RT->>S3: on_frame: per-server median of cell p95 + request count
        S3->>HT: observe({server: (p95, n)})
    end
    loop every health.canary_s = 1 s (code default)
        RT->>WK: GET /canary (round trip measured by the brain)
        RT->>S3: on_canary -> HT.observe_canary
    end
    Note over HT: level = median over 8 ticks (2 s), peer_ratio = level / median of other servers,<br/>slow = peer_ratio >= 1.6 and level - peers >= 8 ms, flips after 4 consecutive ticks
    loop Brain.tick every 250 ms
        RT->>S3: tick(now, acting)
        S3->>HT: slow_servers()
        S3->>S3: _update_ledger: bg CPU growth, eviction share, black-box CPU, over-share (last 3 s)
        S3->>DC: decode(barcodes, peer ratios, stuck, ledger)
        DC-->>S3: Decoding(p_slow, confidence incl. host_fault / none, culprits, host_suspects)
        alt not acting (mode != tenantguard or stale telemetry)
            S3->>S3: close any open server incident
        else nothing slow
            S3->>GW: RouteWeightsCmd(equal weights, ttl 5 s) if routes were sent
            S3->>HUB: resolved, closed
        else servers slow
            S3->>IB: open_server (id srv-epoch-seq) on the first slow server
            S3->>HUB: detected "Slow servers: wN x.x x its peers (by traffic or canary)"
            S3->>RS: plan_rescue(barcodes minus named culprits, slow, load, capacity, guard 0.7)
            RS-->>S3: weights per tenant (lightest first, healthy servers kept under 70 % CPU)
            S3->>GW: POST /control/route_weights RouteWeightsCmd(ttl 120 s) on change or renewal
            S3->>HUB: rescued, action "RouteWeightsCmd N tenants ttl=120s"
            opt same (culprits, host_suspects) for 4 brain ticks
                S3->>HUB: named: suspects, tenant = first culprit,<br/>report = {p_slow, confidence, host_suspects, culprits}
            end
        end
    end
```

**Notes**
- Decoder (code defaults): `p_slow(s) = logistic(8 × (peer_ratio − 1.6))`; each tenant is scored from the min and mean `p_slow` of its barcode, whether it is stuck (all its servers slow) and ledger hints; `host_fault` and `none` compete in the same softmax. A tenant above 0.5 is accepted and its servers explained away, up to two culprits; residual slow servers become `host_suspects`.
- Health, ledger and decoding always run (they feed `ServerView.peer_ratio` and `status`); route changes and server incidents happen only in tenantguard mode with fresh telemetry.
- A named culprit is excluded from rescue so that its load is not spread to healthy servers.
- **In progress:** the relocation **prover** is an interface only (`prover.Prover`, `NullProver` with `available = False`). Stage 3 never calls it, the `named` detail says the culprit is "not yet proven", and the brain emits no `proving`, `verdict` or `explained` stages on master. The gateway's `/control/relocate` endpoint exists but nothing in the brain sends `RelocateCmd`.

---

## 6. Host fault

A slow host with no tenant behind it: TenantGuard still rescues the victims, and the decoder names the host instead of blaming a tenant.

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator demo tool
    participant WK as Worker wN
    participant GW as Gateway
    participant S3 as brain Stage3
    participant DC as decoder.decode
    participant HUB as Hub (UI WS)

    OP->>WK: POST /admin/fault {latency_ms 150, cpu_burn_pct 30, duration_s} + X-Admin-Token
    WK->>WK: FaultInjector.set: fault-burn thread, latency added in Worker.metered
    Note over WK: every /api/* and /canary call on wN is 150 ms slower
    GW-->>S3: TelemetryFrames: wN cell p95 rises
    S3->>S3: HealthTracker: wN peer_ratio >= 1.6 for 4 ticks -> slow
    S3->>HUB: detected (server incident srv-...)
    S3->>GW: RouteWeightsCmd: every tenant on wN moved to its healthy servers (guard 0.7)
    S3->>HUB: rescued
    S3->>DC: decode: no barcode covers the slow set
    DC-->>S3: host_fault wins, host_suspects = [wN], culprits = []
    S3->>HUB: named, tenant None, "Host suspect wN ... no tenant's barcode explains it. No tenant blamed."
    WK->>WK: fault expires after duration_s (FaultInjector.current auto-clears)
    S3->>S3: wN back within peers for 4 ticks
    S3->>GW: RouteWeightsCmd equal weights (ttl 5 s)
    S3->>HUB: resolved, closed "Host suspects: wN."
```

**Notes**
- Peer comparison means a fleet-wide slowdown accuses no single server; the canary keeps a routed-away server measurable so it can be judged healed.
- **In progress:** the lab's `faulty_server` attack returns 501 on master (Wave 2), so the fault is injected directly on the worker (`tools/demo.py inject_fault`). The brain sets no `verdict` (such as `host_fault`) on its events, and sends no `DrainCmd`: the gateway's `/control/drain` endpoint and drain-aware routing exist but are unused, so `ServerView.status` is never `drained`.

---

## 7. Operator / handoff decision

The handoff bridge turns brain incidents into partner cases with an owner and pending actions; the partner's decision flows back as a TTL-bound command; a missed deadline applies the safe default (timers auto-undo).

```mermaid
sequenceDiagram
    autonumber
    participant B as Brain
    participant BR as handoff Bridge
    participant CB as cases.CaseBook
    participant OB as outbox.Outbox
    participant WEB as Partner Web app
    participant GW as Gateway

    BR->>B: WS /ws/events (backoff 0.5 to 10 s)
    B-->>BR: IncidentEvent
    BR->>CB: on_event: new incident -> case INC-0001, CaseContext(evidence_link = ui/case/incident_id)
    alt named with top confidence >= 0.6
        CB->>CB: adopt tenant, severity high, add block action (deadline now + 120 s)
    else level rises to a hold level (throttle .. blocked)
        CB->>CB: add hold action "auto-reverts at HH:MM:SS IST. Extend or release?"
    end
    BR->>OB: put(case_id, version, to_wire(case)) latest wins
    OB->>WEB: POST /cases (timeout 3 s, retry 0.5 to 8 s with jitter)
    WEB-->>OB: 2xx -> mark_delivered
    WEB->>BR: POST /control/case-update + X-Handoff-Token (503 if unset, 401 if wrong)
    alt type owner
        BR->>CB: on_owner -> owner, owner_since_at, history
    else type decision
        BR->>CB: on_decision -> Commands
        alt hold: approve or extend
            BR->>B: POST /api/operator OperatorCmd(extend, until_s 120) + control token
        else hold: release
            BR->>B: POST /api/operator OperatorCmd(release, until_s 120)
        else block: approve
            BR->>GW: POST /control/block BlockCmd(blocked, ttl 300 s)
        else block: release, or resolve
            BR->>GW: POST /control/block BlockCmd(blocked false)
        else reject
            Note over BR: no command, existing timers auto-undo
        end
        B->>B: Brain.operator: release -> ladder reset + clear LimitsCmd,<br/>extend -> hold level and keep renewing TTLs
        BR->>CB: note outcome in history, push case again
    end
    loop every 1 s
        BR->>CB: expire(now): pending past deadline -> expired, "safe default applied"
    end
```

**Notes**
- Every command the bridge sends is TTL-bound (block 300 s, extend 120 s, both capped by `gateway.ttl_max_s`), so "no decision" can never leave a tenant blocked.
- The brain's ladder obeys `OperatorCmd` (release suppresses re-escalation until the hold ends), so humans and the ladder do not fight.
- HH:MM:SS IST strings exist only on the partner wire; internal `CaseView` fields are epoch seconds plus `*_ist` display strings.
- **In progress:** the brain never sets `IncidentEvent.verdict`, so every case keeps the default `CaseContext.verdict = "over_plan"`, including host-fault cases. The bridge runs under the docker-compose profile `handoff`.

---

## 8. Narrator report

A template narrator follows the brain and writes a plain-English incident report and a tenant-neutral customer message whenever the evidence changes.

```mermaid
sequenceDiagram
    autonumber
    participant B as Brain
    participant N as narrator service.Narrator
    participant EB as evidence.EvidenceBook
    participant TP as template.build
    participant SUB as Readers (GET /reports, WS /ws/reports)

    N->>B: GET /api/incidents?limit=500 (backfill)
    N->>B: WS /ws/events (backoff 0.5 to 10 s)
    loop every message
        B-->>N: StateSnapshot or IncidentEvent
        alt state
            N->>EB: add_snapshot (peak peer_ratio of slow servers for open incidents)
        else incident
            N->>EB: add_event (de-duplicated)
            opt stage in (named, verdict, closed) or a report already exists
                N->>EB: pack(incident_id): stages, suspects, slow servers, actions + ttl, peak level, affected
                alt pack digest unchanged
                    Note over N: nothing to publish
                else changed
                    N->>TP: build(pack, anonymise(pack))
                    TP-->>N: IncidentReport (headline, timeline, cause, evidence, action, recommendation) + CustomerMessage
                    N->>SUB: ReportView(version + 1) to WS /ws/reports, kept for GET /reports (max 200)
                end
            end
        end
    end
```

**Notes**
- Reports are deterministic templates that use only fields from the evidence pack; the customer message names no tenant or server.
- `IncidentEvent.report` is a different thing: it carries the decoder dict from Stage 3 `named` events.
- **In progress:** the `verdict` trigger never fires on master (the brain sets no verdict), so reports appear on `named` and `closed`. The UI does not read the narrator's `/reports` yet; its Case File shows the decoder report or a template placeholder. The narrator runs on :8700 under the docker-compose profile `narrator`.

---

## 9. UI live stream

The brain pushes state at 2 Hz and every incident event as it happens; the UI polls the gateway admin API for quotas and overrides, the lab for attacks and results, and the bridge for cases.

```mermaid
sequenceDiagram
    autonumber
    participant UI as UI LiveProvider + LiveStore
    participant BR as Brain main.py / runtime.Hub
    participant BC as brain core.Brain
    participant GW as Gateway /admin
    participant LB as Lab /lab
    participant HO as Handoff bridge

    UI->>BR: GET /api/incidents (backfill)
    UI->>BR: WS /ws/events (reconnect 0.5 to 8 s)
    BR-->>UI: StateSnapshot now + last 100 IncidentEvents
    loop every 0.5 s while UI clients are connected
        BR->>BC: snapshot(now)
        Note over BC: TenantView: p95 (5 s), rps, rejected_rps, cpu_share, noisy_score/norm, level, factor<br/>ServerView: p95 (2 s), peer_ratio, cpu_pct, healthy or slow<br/>utilization, Jain fairness, status_sentence, brain_ok, noisy_thresholds
        BR-->>UI: StateSnapshot
    end
    BC-->>BR: IncidentEvent (tick loop, /api/operator, adoption)
    BR-->>UI: IncidentEvent immediately
    UI->>UI: LiveStore.apply: 60 s history, incident dedupe, impact meter
    loop every 2 s
        UI->>GW: GET /admin/overrides + X-TenantGuard-Control-Token
    end
    loop every 30 s
        UI->>GW: GET /admin/tenants (barcodes, revenue, effective quota)
    end
    UI->>GW: POST /admin/mode, PATCH /admin/tenants/{id}/quota (operator actions)
    UI->>LB: GET /lab/status (2 s), POST /lab/attack, DELETE /lab/attack/{id}, GET /lab/results (10 s)
    UI->>HO: GET /cases (5 s), shown only when reachable
    UI->>BR: GET /api/incidents?incident_id= (Case File, 3 s)
```

**Notes**
- The hub's per-client queue holds 256 messages; when it is full the oldest message is dropped, so a slow browser never stalls the brain.
- A wrong or missing control token gets 401 on every gateway `/admin/*` and `/control/*` route.
- **In progress:** `StateSnapshot.impact` (`ImpactView`) is in the contracts but the brain leaves it empty on master; the UI computes its ₹ impact meter client-side (`ui/src/lib/impact.ts`). `TenantView.placement` is always `normal` and `ServerView.status` is never `drained` on master.

---

## Implemented endpoints that nothing calls on master

| Endpoint | Status |
|---|---|
| gateway `POST /control/charge` (`ChargeCmd`), `POST /control/reference_costs` (`ReferenceCostsCmd`) | implemented, no sender (the billing service is in progress) |
| gateway `POST /control/relocate`, `POST /control/drain` | implemented, used only by `gateway/tools/prove.py` and `brain/tools/fake_gateway.py` |
| lab Wave 2 attacks (`background_bomb`, `blackbox_bomb`, `cache_flood`, `faulty_server`, `honest_spike`) | return 501 (in progress) |
