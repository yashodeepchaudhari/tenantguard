# 05 · Sequence diagrams

Thirteen flows, traced from the code on this branch. Every arrow names the real function or endpoint; config values come from `tenantguard.yaml` (or are marked *code default*). Units: `ts` / `*_at` are epoch seconds, `*_ms` are milliseconds, shares are 0..1.

| # | Flow | Main code |
|---|---|---|
| 1 | [Request path](#1-request-path) | `gateway/tenantguard_gateway/app.py`, `core.py`; `worker/app/main.py`, `meter.py` |
| 2 | [Admission refusal (429 / 503 / 401 / 413)](#2-admission-refusal) | `core.py`, `admission.py`, `buckets.py`, `retry.py`, `overrides.py` |
| 3 | [DRR dispatch](#3-drr-dispatch) | `core.py`, `scheduler.py`, `router.py`, `limiter.py` |
| 4 | [Detection → throttle ladder → recovery](#4-detection--throttle-ladder--recovery) | `brain/tenantguard_brain/runtime.py`, `core.py`, `score.py`, `ladder.py`, `incidents.py` |
| 5 | [Health → rescue → decoder naming](#5-health--rescue--decoder-naming) | `stage3.py`, `health.py`, `rescue.py`, `decoder.py` |
| 6 | [Held hosts (hot tenants judged first)](#6-held-hosts) | `stage3.py` (`hot_on`, `held_by_culprit`) |
| 7 | [Relocation proof](#7-relocation-proof) | `stage3.py` (`_judge`), `runtime.py` (`_run_job`), `prover.py` (`DockerProver`, `EngineDocker`), `verdicts.py` (`evaluate_proof`) |
| 8 | [Verdict application](#8-verdict-application) | `stage3.py` (`on_proof`, `_verdict`), `verdicts.py` (`classify`), `core.py` (`force_throttle`) |
| 9 | [Host fault and drain test](#9-host-fault-and-drain-test) | `worker/app/fault.py`, `stage3.py`, `prover.py` (`HostProver`) |
| 10 | [Background CPU charge](#10-background-cpu-charge) | brain `core.py` (`ingest_stats`, `_charges`), `runtime.py`; gateway `app.py`, `core.py` (`charge`) |
| 11 | [Operator / handoff decision](#11-operator--handoff-decision) | `integrations/handoff/tenantguard_handoff/*`, brain `/api/operator` |
| 12 | [Narrator report](#12-narrator-report) | `narrator/tenantguard_narrator/*` |
| 13 | [UI live stream](#13-ui-live-stream) | brain `main.py`, `runtime.py`, `core.py`; `ui/src/lib/*` |

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
- Background work (`POST /api/write` jobs) is charged to the tenant in `Meters.add_bg`, but it is not part of the `X-TenantGuard-Cpu-Ms` header. In `tenantguard` mode the brain debits it from the cost bucket once a second through `POST /control/charge` (flow 10).
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
- Item cost comes, in order, from brain-pushed `reference_costs[cls]` (tenantguard), the measured per-(tenant, class) EWMA, or `DEFAULT_CLASS_COST_MS` = 5 ms. Nothing on this branch sends `ReferenceCostsCmd`, so the EWMA or the default is used.
- Lane caps with window `w` (8 initially, `gateway.limiter`): express `w`, interactive `w − max(1, w//8)`, batch `max(1, min(interactive, w//2))`; batch is shed first.
- The limiter's RTT sample excludes the request's own CPU, so one heavy request does not shrink the window by itself.
- Relocations (`RelocateCmd`, flows 7 and 8) and drains (`DrainCmd`, flow 9) are applied here by `router.choose`.

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
        RT->>B: ingest_frame(frame): store, prune, Stage3.on_frame
    end
    loop every brain.stats_poll_s = 1 s
        RT->>WK: GET /stats
        WK-->>RT: WorkerStats (cumulative)
        RT->>B: ingest_stats: diff counters (no diff after a restart), bg CPU to bg_pending
    end
    loop every 0.5 s
        RT->>GW: POST /control/heartbeat Heartbeat(ts) + control token
    end
    loop every brain.tick_ms = 250 ms
        RT->>B: tick(now)
        B->>B: victim_latency = 0.3 quantile of per-tick p95 over 5 s (at least 4 ticks)
        B->>B: once a second while calm: _learn_baselines (half-life 60 s, frozen tenants and outliers skipped)
        B->>B: slowdown = 0 if less than 8 ms above baseline, else p95 / max(baseline, 8 ms) - 1
        B->>SC: cpu_share and median queue_share on the tenant's servers vs fair share,<br/>victim_impact = mean neighbour slowdown, each clipped [0, 3]
        SC-->>B: score = max(shares)/fair x (1 + impact)<br/>flagged = ratio >= 1.6 and impact >= 0.2 and 4+ neighbours hurt
        alt telemetry fresh and mode == tenantguard
            opt rescue grace (server incident on its servers, rescue younger than 7 s)
                B->>B: score capped just below throttle 2.0 (may warn, not throttle)
            end
            B->>LD: step(state, Signal(score, flagged, unhurt), now, LadderParams)
            Note over LD: normal -> warn: score >= 1.5 held 5 s<br/>warn -> throttle: >= 2.0 held 2 s, factor 0.5<br/>throttle step: factor x 0.5 down to 0.25<br/>-> deprioritize: held 4 s, weight_factor 0.25<br/>-> quarantine: >= 4.0 held 6 s, factor 0.1 / weight 0.1
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
    Note over B,LD: recovery: calm (score < 1.0, or impact < 0.1 and ratio < 1.6) held 10 s,<br/>then +0.1 on both factors per second
    B->>HUB: level_change "Relaxed" ... resolved (back to full plan) ... closed (IncidentBook.close)
```

**Notes**
- Holds use `hold_ratio` 0.8: at least 80 % of the ticks in the hold window must qualify, including the current one. Every escalation restarts all hold clocks.
- Gateway-side safety: every override has a TTL that the gateway sweeps each tick and re-checks on read; `ttl_s` ≤ `gateway.ttl_max_s` (3600). If heartbeats stop for `fail_static_after_s` (2 s) the gateway keeps its last configuration and reports `fail_static: true` in `/admin/overrides`; TTLs still expire.
- Senders always send **both** factors; the latest `LimitsCmd` for a tenant replaces the previous one.
- The ladder's quarantine rung is a 0.1 / 0.1 `LimitsCmd`. Moving a tenant's work to the quarantine worker is done by the `costly_pattern` verdict (flow 8).
- Every tightening is checked by the impact meter: a tenant tightened while its own score was below `warn` would be listed in `ImpactView.innocents_penalised`.

---

## 5. Health → rescue → decoder naming

Stage 3 compares each tenant with itself across its own servers (traffic) and uses canary round trips as a fallback, routes victims away from slow servers within a capacity guard, and names the cause by decoding which tenant's barcode explains the slow set.

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
        RT->>S3: on_frame: cells (tenant, server, p95, count minus errors)
        S3->>HT: observe(cells)
    end
    loop every health.canary_s = 1 s
        RT->>WK: GET /canary (round trip measured by the brain)
        RT->>S3: on_canary -> HT.observe_canary
    end
    Note over HT: cell level = 0.3 quantile of per-tick p95 over 5 s (5+ requests)<br/>peer_ratio(s) = median over its tenants of level / that tenant's own median level<br/>slow now = ratio >= 1.6 and excess >= 8 ms and level >= floor (25 ms or half the strictest SLA)<br/>opens after 4 s of 80 % slow ticks, heals after 2 s, canary can only keep it open
    loop Brain.tick every 250 ms
        RT->>S3: tick(now, acting)
        S3->>HT: slow_servers() plus held_by_culprit()
        S3->>S3: _update_ledger: bg CPU growth, eviction share, black-box CPU, over-share (last 3 s)
        S3->>DC: decode(barcodes, decoder_ratios, stuck, ledger)
        Note over S3,DC: decoder_ratios: a server below its SLA floor enters as ratio <= 1.0
        DC-->>S3: Decoding(p_slow, confidence incl. host_fault / none, culprits, host_suspects)
        S3->>S3: held hosts: host suspects with a hot tenant are removed (flow 6)
        alt not acting (mode != tenantguard or stale telemetry)
            S3->>S3: close any open server incident
        else nothing slow and no test running
            S3->>GW: RouteWeightsCmd(equal weights, ttl 5 s) if routes were sent, DrainCmd drained=false for our drains
            S3->>HUB: resolved, closed
        else servers slow (or a test is running)
            S3->>IB: open_server (id srv-epoch-seq) on the first slow server
            S3->>HUB: detected "Slow servers: wN x.x x its peers (by traffic or canary)"
            S3->>RS: plan_rescue(barcodes minus named culprits, slow, load, capacity, guard 0.7, targets = up and healthy)
            RS-->>S3: weights per tenant (lightest first, healthy servers kept under 70 % CPU)
            S3->>GW: POST /control/route_weights RouteWeightsCmd(ttl 120 s) on change, renewal (30 s) or retry (1 s)
            S3->>HUB: rescued, action "RouteWeightsCmd N tenants ttl=120s"
            opt same (culprits, host_suspects) for 4 brain ticks
                S3->>HUB: named: suspects, tenant = first culprit,<br/>report = {p_slow, confidence, host_suspects, culprits}
            end
            S3->>S3: _judge: proof or verdict for each culprit (flows 7 and 8), drain test for each host suspect (flow 9)
        end
    end
```

**Notes**
- Decoder (yaml `brain.decoder`): `p_slow(s) = logistic(8 × (peer_ratio − 1.6))`; each tenant is scored from the min and mean `p_slow` of its barcode, whether it is stuck (all its servers slow) and ledger hints; `host_fault` and `none` compete in the same softmax. A tenant is accepted when the tenants together hold ≥ 0.5 and it is the top option; its servers are explained away, up to two culprits; residual slow servers become `host_suspects`.
- Health, ledger and decoding always run (they feed `ServerView.peer_ratio` and `status`); route changes, tests and server incidents happen only in tenantguard mode with fresh telemetry.
- A named culprit is excluded from rescue so that its load is not spread to healthy servers. The prover moves it instead.
- With `NullProver` (no Docker socket, or no `COMPOSE_PROJECT`), a culprit at or above the confidence bar is named only, and the `named` detail says relocation needs the Docker host.

---

## 6. Held hosts

During a ramp the decoder can see one or two of a culprit's servers slow before all three, which looks like a faulty host. Before any host suspect is named or drained, Stage 3 asks whether a hot tenant runs there. If one does, the tenant is judged first and the server is a **held host**.

```mermaid
sequenceDiagram
    autonumber
    participant S3 as stage3.Stage3
    participant DC as decoder.decode
    participant B as core.Brain
    participant HUB as Hub (UI WS)

    S3->>DC: decode(barcodes, decoder_ratios, stuck, ledger)
    DC-->>S3: culprits, host_suspects (for example w1, w8 while K ramps)
    loop each host suspect srv
        S3->>S3: hot_on(srv)
        S3->>S3: named culprits on srv not yet judged, or being proved
        S3->>B: ladders: tenants on srv at warn or acting
        S3->>B: cpu_by_server, server_capacity: tenants using >= 1.6 x their weighted fair share of srv CPU
        alt hot tenants now
            S3->>S3: held_hosts[srv] = tenants, ever_held[srv] = tenants
        else none now, but ever_held[srv] in this incident
            S3->>S3: held_hosts[srv] = ever_held[srv]
        else no hot tenant at all
            S3->>S3: srv stays a host suspect (flow 9)
        end
    end
    S3->>S3: host_suspects = suspects not held (never named as a host, never drained)
    opt first time srv is held in this incident
        S3->>HUB: named "srv is slow while K runs hot there: it is judged first, srv is not treated as a faulty host"<br/>report = {host_suspects: [], held_hosts: {srv: [K]}}
    end
    S3->>S3: _judge culprits first (flow 7)
    Note over S3: held_by_culprit keeps the reported-slow servers a named culprit still over-uses in the slow set,<br/>so the rescue is not lifted while the culprit is hot
```

**Notes**
- The "hot on this server" test uses the entitlement over every tenant placed on the server, not only those still sending there, so a culprit left alone after a rescue is still hot.
- `ever_held` is kept for the rest of the incident: a server explained by a hot tenant earlier is not drained later in the same incident.
- In the host acceptance run, the K big-plan whale was named, proved (R = 0.88) and judged `honest_growth`, with held hosts w1 and w8 judged first.

---

## 7. Relocation proof

A culprit named at or above `confidence_to_prove` (0.8) is proved by moving it to the spare and watching whether its original servers recover and the harm follows it. The test runs as an async job so the tick loop never waits.

```mermaid
sequenceDiagram
    autonumber
    participant S3 as stage3.Stage3
    participant RT as runtime.Runtime
    participant DP as prover.DockerProver
    participant DE as prover.EngineDocker
    participant DK as Docker Engine (unix socket)
    participant GW as Gateway
    participant B as core.Brain
    participant HUB as Hub (UI WS)

    S3->>S3: _judge(K): not repeat, confidence >= 0.8, prover.available, budget can_relocate (6 per min)
    S3->>B: footprint(K) = paid CPU + black-box CPU on its servers, pre_evidence = verdict_inputs(K)
    S3->>HUB: proving, action "RelocateCmd target=spare"
    S3-->>RT: TickResult.jobs = [ProofRequest(K, servers, confidence, footprint)]
    RT->>DP: prove(req, env = Runtime) in a background task
    DP->>DP: take the lock (one test at a time, the spare is shared)
    DP->>RT: readings() -> Brain.readings (slowdown = max(0, peer_ratio - 1))
    alt mean slowdown of K's servers <= 0
        DP-->>RT: ProofResult(confirmed false, relocated false) "nothing to prove"
    else K's servers are slow
        DP->>DE: get_cpus(spare)
        DE->>DK: GET /containers/json filtered by labels project=COMPOSE_PROJECT and service=spare
        DE->>DK: GET /containers/{id}/json -> HostConfig.NanoCpus
        DP->>DE: set_cpus(spare, clamp(1.25 x footprint / 1000, 0.25, 4.0))
        DE->>DK: POST /containers/{id}/update {NanoCpus}
        opt resize fails
            DP->>DP: test on the spare as it is (noted in the detail)
        end
        DP->>RT: send /control/relocate RelocateCmd(K, target spare, ttl 120 s)
        RT->>GW: POST /control/relocate + control token
        alt gateway refuses
            DP->>DE: restore_spare(cores before)
            DP-->>RT: ProofResult(confirmed false) "the gateway refused the relocation"
        else relocated
            DP->>RT: sleep(prove_settle_s = 2 s)
            loop 12 samples, every 0.5 s for prove_window_s = 6 s
                DP->>RT: sleep(0.5), readings()
                DP->>DP: post slowdown on K's original servers, spare CPU, spare peer ratio
            end
            DP->>DP: evaluate_proof: R = 1 - mean(post) / pre
            DP->>DP: harm followed = spare CPU rise >= 0.3 x footprint or spare ratio >= 1.6
            alt R >= 0.5 and harm followed
                DP-->>RT: ProofResult(confirmed true, relocated true, R, readings)
            else
                DP-->>RT: ProofResult(confirmed false, relocated true, R, readings)
            end
        end
    end
    RT->>B: on_proof(result) -> Stage3.on_proof -> verdict (flow 8)
    RT->>RT: _apply(TickResult): events, control commands, jobs
```

**Notes**
- The relocation is left in place after the test; the verdict decides whether K stays on the spare, goes to the quarantine lane or goes home.
- The spare is resolved by **both** compose labels, or by `TENANTGUARD_SPARE_CONTAINER`. Without a project or explicit name, `make_prover` returns `NullProver` and no Docker call is made.
- While a test runs, `Stage3.tick` keeps the server incident and the rescue open even if K's servers look fine (they look fine *because* K was moved).
- If the incident has closed by the time the result arrives, `on_proof` sends `RelocateCmd(target=None)` and restores the spare.

---

## 8. Verdict application

`Stage3.on_proof` classifies on the evidence captured *before* the test (what K did on its own servers) plus the proof result, then `_verdict` queues the control commands for that outcome. A culprit below the confidence bar, or a repeat offender, reaches `_verdict` straight from `_judge` without a test.

```mermaid
sequenceDiagram
    autonumber
    participant S3 as stage3.Stage3
    participant VD as verdicts.classify
    participant B as core.Brain
    participant RT as runtime.Runtime (Sender)
    participant GW as Gateway
    participant DP as DockerProver
    participant HUB as Hub (UI WS)

    alt repeat offender (named 3+ times in 600 s, has a last verdict)
        S3->>S3: verdict = last verdict, no new proof
    else confidence < 0.8
        S3->>VD: classify(Evidence(confirmed false))
        VD-->>S3: mitigated_no_blame
    else proof result
        S3->>VD: classify(Evidence(confidence, confirmed, R, harm, paid_share, blackbox_hint, evict_hint, rejected_share))
        VD-->>S3: verdict + plain-English reasons
    end
    alt mitigated_no_blame (not confirmed, or below the bar)
        opt K was relocated
            S3->>RT: RelocateCmd(K, target None) key relocate:K:*
            RT->>GW: POST /control/relocate (K back home)
        end
        S3-->>RT: job restore_spare
        RT->>DP: restore_spare(cores before)
    else over_plan (confirmed, admission refuses >= 20 % of its requests)
        opt K was relocated
            S3->>RT: RelocateCmd(K, target None)
            RT->>GW: POST /control/relocate (home)
        end
        S3->>B: force_throttle(K): ladder at throttle, factor 0.5
        B->>RT: next tick: LimitsCmd(K, factor 0.5, weight_factor 1.0, ttl 60 s)
        RT->>GW: POST /control/limits (renewed, then relaxed by AIMD when calm)
        S3-->>RT: job restore_spare
    else costly_pattern (confirmed, paid share < 35 % or black-box / eviction hint >= 0.3)
        alt quarantine budget allows (one every 30 s)
            S3->>B: top_class(K) = the class that grew most vs its calm baseline
            S3->>RT: RelocateCmd(K, cls, target quarantine, ttl 120 s) key relocate:K:cls
            RT->>GW: POST /control/relocate (heavy lane)
            opt a class is known
                S3->>RT: RelocateCmd(K, target None) key relocate:K:*
                RT->>GW: POST /control/relocate (the rest of K goes home)
            end
            S3-->>RT: job restore_spare
        else budget used
            S3->>RT: RelocateCmd(K, target spare, ttl 120 s) only if K was not relocated
            Note over S3: kept apart on the spare, spare stays sized
        end
    else honest_growth (confirmed, paid work explains the load)
        alt K was relocated by the test
            Note over S3,GW: no new command: the proof's RelocateCmd target=spare (ttl 120 s) stands
        else not relocated (repeat offender path)
            S3->>RT: RelocateCmd(K, target spare, ttl 120 s)
            RT->>GW: POST /control/relocate (burst lane)
        end
        Note over S3: no penalty, spare stays sized until the relocation TTL ends
    end
    S3->>S3: judged[K] = verdict, memory.note_verdict
    S3->>HUB: verdict event: IncidentEvent.verdict, tenant K, suspects,<br/>detail "Verdict ... Action ...", report = {verdict, evidence, readings}
```

**Notes**
- The fifth outcome, `host_fault`, belongs to host suspects and is applied in flow 9.
- `over_plan` is checked before `costly_pattern`. A not-confirmed repeat offender is judged on its pre-test evidence.
- Relocation commands use their own sender keys (`relocate:<tenant>:<cls or *>`), so a class move and a whole-tenant move never replace each other.
- Every relocation has a TTL (120 s), so nothing stays moved without the brain renewing it.

---

## 9. Host fault and drain test

A slow host with no tenant behind it: TenantGuard rescues the victims, the decoder names the host instead of blaming a tenant, and a drain test confirms it.

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator (docker exec or tools/demo.py)
    participant WK as Worker wN
    participant GW as Gateway
    participant S3 as brain Stage3
    participant RT as runtime.Runtime
    participant HP as prover.HostProver
    participant HUB as Hub (UI WS)

    OP->>WK: POST /admin/fault {latency_ms 150, cpu_burn_pct 30, duration_s} + X-Admin-Token
    WK->>WK: FaultInjector.set: fault-burn thread, latency added in Worker.metered
    Note over WK: every /api/* and /canary call on wN is 150 ms slower
    GW-->>S3: TelemetryFrames: wN cell levels rise for every tenant on it
    S3->>S3: HealthTracker: ratio >= 1.6, above its SLA floor, 80 % of 4 s -> wN open
    S3->>HUB: detected (server incident srv-...)
    S3->>GW: RouteWeightsCmd: every tenant on wN moved to its healthy servers (guard 0.7)
    S3->>HUB: rescued
    S3->>S3: decode: no barcode covers the slow set -> host_suspects = [wN], hot_on(wN) is empty
    S3->>HUB: named, tenant None, "Host suspect wN ... no tenant's barcode explains it. No tenant blamed."
    S3->>S3: _judge: budget can_drain(wN) (none drained yet, peers below 75 % CPU)
    alt drain not allowed
        S3->>HUB: proving note "drain test for wN deferred: ..." (said once)
    else allowed
        S3->>HUB: proving, action "DrainCmd wN"
        S3-->>RT: job HostCheck(wN)
        RT->>HP: check(req, env = Runtime)
        HP->>GW: POST /control/drain DrainCmd(wN, drained true, ttl 120 s)
        HP->>HP: sleep 2 s, then 12 samples of wN peer_ratio every 0.5 s (canary source while drained)
        HP-->>RT: HostResult(drained, idle_ratio = mean)
        RT->>S3: Brain.on_host -> Stage3.on_host -> host_verdict
        alt idle_ratio >= 1.5
            S3->>HUB: verdict host_fault "wN stays drained ... No tenant blamed."
            loop every 30 s (ttl / 4) while the incident is open
                S3->>GW: DrainCmd(wN, drained true) renewal
            end
        else idle_ratio < 1.5
            S3->>GW: DrainCmd(wN, drained false)
            S3->>HUB: verdict mitigated_no_blame "it was load, not the host, undrained"
        end
    end
    WK->>WK: fault expires after duration_s (FaultInjector.current auto-clears)
    S3->>S3: wN canary back within peers, healthy after 2 s
    S3->>GW: RouteWeightsCmd equal weights (ttl 5 s), DrainCmd(wN, drained false)
    S3->>HUB: resolved, closed "Host suspects: wN."
```

**Notes**
- Peer comparison means a fleet-wide slowdown accuses no single server. The canary keeps a routed-away or drained server measurable, so it can be judged healed, but the canary alone never opens an incident.
- The lab's `faulty_server` attack is not included (the lab answers 501 for it), so the faulty-server scene is triggered with the worker's `/admin/fault` (a `docker exec` on the host, or `tools/demo.py inject_fault`).
- At most one server is drained at a time (`action_budget.max_drained` 1). In the host acceptance run, a w4 fault gave `host_fault` at 5.1x idle and no tenant was blamed.
- `ServerView.status` in the brain snapshot is `healthy` or `slow`; the drain shows in the gateway's `/admin/overrides`.

---

## 10. Background CPU charge

Background jobs burn CPU outside the request path, so the worker's `X-TenantGuard-Cpu-Ms` header does not include it. In `tenantguard` mode the brain pushes it to the gateway once a second and the gateway debits the tenant's cost bucket.

```mermaid
sequenceDiagram
    autonumber
    participant WK as Worker /stats
    participant RT as runtime.Runtime
    participant B as core.Brain
    participant SD as runtime.Sender
    participant GW as Gateway app.py control_charge
    participant GC as Gateway core.charge

    loop every brain.stats_poll_s = 1 s
        RT->>WK: GET /stats
        WK-->>RT: WorkerStats, TenantUsage.bg_cpu_ms (cumulative)
        RT->>B: ingest_stats: delta bg_cpu_ms per tenant
        B->>B: bg_pending[tenant] += delta
    end
    loop Brain.tick, at most once per brain.charge_s = 1 s
        B->>B: _charges(now, acting): take bg_pending, reset it
        alt mode == tenantguard and telemetry fresh
            B-->>RT: TickResult.control: (charge:K:second, /control/charge, ChargeCmd(K, cpu_ms, period_s, charge_id K-second))
            RT->>SD: submit(key, path, cmd, _control_done)
            SD->>GW: POST /control/charge + control token
            GW->>GW: 400 if unknown tenant, or cpu_ms negative or not finite
            GW->>GC: charge(cmd)
            alt gateway mode is not tenantguard
                GC-->>GW: "ignored: mode"
            else charge_id seen before (last 1024 kept)
                GC-->>GW: "duplicate charge_id"
            else
                GC->>GC: cost.debit(cpu_ms), telemetry.charged(tenant, cpu_ms)
                GC-->>GW: "charged K n ms"
            end
            GW-->>SD: ControlAck
            alt not delivered (error or 4xx)
                SD->>RT: _control_done(cmd, false)
                RT->>B: charge_failed(cmd): bg_pending[K] += cpu_ms
                Note over B: rides on the next charge with a new charge_id
            end
        else any other mode
            Note over B: pending CPU is dropped, not charged
        end
    end
```

**Notes**
- `ChargeCmd` is not an override: no TTL, applied once. The per-second sender key means two charges for the same tenant never replace each other in the `Sender`.
- The debited CPU makes the cost bucket refuse sooner (flow 2) for a tenant whose background jobs are heavy, in `tenantguard` mode only.

---

## 11. Operator / handoff decision

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
    alt named with top confidence >= confidence_to_prove (0.8)
        CB->>CB: adopt tenant, severity high, add block action (deadline now + 120 s)
    else level rises to a hold level (throttle .. blocked)
        CB->>CB: add hold action "auto-reverts at HH:MM:SS IST. Extend or release?"
    else verdict event
        CB->>CB: CaseContext.verdict = IncidentEvent.verdict, history "Verdict: ..."
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
- The bridge's naming bar (`confidence_to_name`) is read from `brain.confidence_to_prove` in `tenantguard.yaml` (code default 0.6). A case starts with `CaseContext.verdict = "over_plan"` and takes the brain's verdict when a `verdict` event arrives.
- HH:MM:SS IST strings exist only on the partner wire; internal `CaseView` fields are epoch seconds plus `*_ist` display strings. The bridge runs under the docker-compose profile `handoff`.

---

## 12. Narrator report

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
                N->>EB: pack(incident_id): stages, suspects, verdict, slow servers, actions + ttl, peak level, affected
                alt pack digest unchanged
                    Note over N: nothing to publish
                else changed
                    N->>TP: build(pack, anonymise(pack))
                    TP-->>N: IncidentReport (headline, timeline, cause "Verdict: ...", evidence, action, recommendation) + CustomerMessage
                    N->>SUB: ReportView(version + 1) to WS /ws/reports, kept for GET /reports (max 200)
                end
            end
        end
    end
```

**Notes**
- Reports are deterministic templates that use only fields from the evidence pack; the customer message names no tenant or server.
- `IncidentEvent.report` is a different thing: Stage 3 `named` events carry the decoder dict, and `verdict` events carry `{verdict, evidence, readings}`.
- The UI's Case File reads the brain's incident events, not the narrator's `/reports`; when the brain has not written a report it shows a template placeholder. The narrator runs on :8700 under the docker-compose profile `narrator`.

---

## 13. UI live stream

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
        Note over BC: TenantView: p95 (5 s), rps, rejected_rps, cpu_share, noisy_score/norm, level, factor<br/>ServerView: p95 (2 s), peer_ratio, cpu_pct, healthy or slow<br/>utilization, Jain fairness, status_sentence, brain_ok, impact, noisy_thresholds
        BR-->>UI: StateSnapshot
    end
    BC-->>BR: IncidentEvent (tick loop, proof and drain jobs, /api/operator, adoption)
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
- The brain fills `StateSnapshot.impact` (`ImpactView` from `impact.ImpactMeter`). The UI's ₹ impact meter (`ui/src/lib/impact.ts`) is computed client-side from the snapshot stream. `TenantView.placement` is always `normal` in the brain snapshot.

---

## Endpoints and their callers

| Endpoint | Caller |
|---|---|
| gateway `POST /control/charge` (`ChargeCmd`) | brain `Brain._charges`, once a second in tenantguard mode (flow 10) |
| gateway `POST /control/relocate` (`RelocateCmd`) | brain `DockerProver` and `Stage3._verdict` (flows 7 and 8); also `gateway/tools/prove.py` |
| gateway `POST /control/drain` (`DrainCmd`) | brain `HostProver`, `Stage3.on_host`, `_renew_drains`, `_close` (flow 9) |
| gateway `POST /control/reference_costs` (`ReferenceCostsCmd`) | implemented, no sender on this branch |
| lab wave-2 attacks (`background_bomb`, `blackbox_bomb`, `cache_flood`, `faulty_server`, `honest_spike`) | not included; the lab answers 501 for them |
