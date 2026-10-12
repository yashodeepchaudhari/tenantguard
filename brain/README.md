# TenantGuard brain

Control plane on **:8200**. It watches every tenant's usage and latency and every server's health.
- **Stage 2:** detects a noisy neighbour from usage share × victim impact and throttles it with a
  graduated, self-recovering ladder.
- **Stage 3:** tells *which server* is slow, rescues victims by routing around it within ~2 s, and names
  the culprit from the barcode pattern (or a host suspect, with no tenant blamed).

Every action is explained, and live state streams to the dashboard.

## Run

```
pip install -r brain/requirements.txt          # tenantguard_common is already installed
cd brain
python -m tenantguard_brain                     # binds 0.0.0.0:8200
```

| Env | Default | Meaning |
|---|---|---|
| `TENANTGUARD_GATEWAY_URL` | yaml `services.gateway` | telemetry WS, heartbeat and `/control/*` target |
| `TENANTGUARD_WORKER_URLS` | yaml `workers` | `w0=http://..,w1=..` polled at `brain.stats_poll_s` |
| `TENANTGUARD_CORS_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173` | UI origins |
| `TENANTGUARD_BRAIN_HOST` / `_PORT` | `0.0.0.0` / port of `services.brain` | bind |
| `TENANTGUARD_CONTROL_TOKEN` | `dev` | sent as `X-TenantGuard-Control-Token` on every gateway `/admin` + `/control` call; required on `POST /api/operator` |

Docker: `docker compose up brain` (image from `brain/Dockerfile`, build context = repo root).

## API

| | |
|---|---|
| `WS /ws/events` | on connect: one `StateSnapshot` + the last 100 `IncidentEvent`s; then a `StateSnapshot` every 500 ms and every `IncidentEvent` as it happens |
| `GET /api/state` | `StateSnapshot` |
| `GET /api/incidents?incident_id=&tenant=&limit=200` | `List[IncidentEvent]`, oldest first |
| `GET /healthz` | telemetry / gateway / worker reachability, counters |
| `POST /api/operator` | `OperatorCmd` (control token required, else 401). `release`: ladder to normal, clear 1.0/1.0, no automatic escalation until `until_s` (a suppressed escalation is noted once in the incident). `extend`: keep the current level/factors (TTLs renewed) until `until_s`. `resume`: cancel the hold. 400 unless `0 < until_s <= gateway.ttl_max_s` and finite; 404 for an unknown tenant. When a hold ends the ladder needs fresh, full holds again |
| `GET /api/debug` | scores, ladder state, baselines, last command per tenant, server health, decoding, rescue plan |

## Loops (`runtime.py`)

- on start: `GET /admin/overrides`. Limits left by a previous brain run are **adopted** (the ladder takes
  over at that level, so they keep being renewed and are relaxed when calm); limits for unknown
  tenants are cleared. Retries every 2 s until the gateway answers.
- telemetry WS `/internal/telemetry` → `Brain.ingest_frame`, reconnect with backoff 0.5 → 5 s
- worker `/stats` every `stats_poll_s`, cumulative counters diffed (a `started_at` change = restart, no diff)
- worker `/canary` every `health.canary_s` (default 1 s): round trip, the health fallback source
- `POST /control/heartbeat` every `gateway.fail_static_after_s / 4` (= 500 ms)
- decision tick every `brain.tick_ms`
- control sends are **serialised per key** (one per tenant's limits, one for route weights). At most one
  request is in flight per key and a newer command replaces an unsent older one (latest wins), so a
  stale command can never overtake a newer one. Task references are held until done.
- UI snapshot broadcast at 2 Hz. Before the first frame `mode` = `gateway.default_mode`;
  `noisy_thresholds` carries `brain.noisy` warn / throttle / quarantine / recover_below.

## Detection (`score.py`, pure)

```
score = max(cpu_share / fair_share, queue_share / fair_share) * (1 + victim_impact)
```

- **Shares are scoped to the tenant's own servers.** `cpu_share` = its CPU ms/s on its servers / their
  CPU capacity; `queue_share` = its (queue depth + inflight) / their dispatch slots; `fair_share` =
  per server `weight / sum of active weights on that server`, capacity-weighted. A plain
  "share of all tenant CPU vs weight / Σ weights" puts every calm free-tier tenant at ≈1.5× (demand is not
  proportional to weight), so the warn threshold of 1.5 would fire on innocents as soon as anyone is slow.
  Capacity-scoped shares keep calm tenants well below 1 and only exceed 1 when a tenant crowds its neighbours.
- **victim_impact** = mean over the tenant's *neighbours* (tenants sharing ≥ 1 server, from the barcode
  layout) of `max(0, p95 / baseline − 1)`, clipped to [0, 3]. p95 = `QueueStat.p95_ms` over the last
  `health.probe_window_ticks` ticks, weighted by completions; ticks with `completed == 0` are skipped.
  Neighbours instead of "all others" keeps a heavy tenant on disjoint servers from being blamed for
  someone else's victims.
- **baseline_p95** = per-tenant time-based EWMA with half-life `noisy.baseline_half_life_s` (60 s), updated
  only while the fleet is calm (no open incident, nobody flagged), ignoring > 2× outliers, and **frozen for a
  tenant while it is hurt (slowdown ≥ min_victim_impact) or while any neighbour uses more than its fair
  share**. Without the freeze a moderate attack that never crosses a threshold becomes the new "normal". The baseline is floored at
  `health.slow_abs_ms` so ~1 ms endpoint jitter never reads as a 3× slowdown.
- **flagged** = `ratio > 1` **and** `victim_impact ≥ noisy.min_victim_impact` (default 0.2). A legit burst
  with nobody hurt is never flagged, whatever its score.
- `noisy_norm = min(1, score / noisy.quarantine)`.

## Ladder (`ladder.py`, pure)

```
normal → warn → throttle (factor ×0.5 per step to 0.25) → deprioritize (weight_factor 0.25)
       → quarantine (factor 0.1, weight_factor 0.1; TODO Wave 2: relocate to the quarantine worker)
```

| Move | Needs (flagged and …) | held for |
|---|---|---|
| normal → warn | score ≥ `warn` | `hold_s.warn` |
| warn → throttle, each ×0.5 step | score ≥ `throttle` | `hold_s.throttle` |
| → deprioritize (factor at floor) | score ≥ `throttle` | `hold_s.deprioritize` |
| → quarantine (deprioritized) | score ≥ `quarantine` | `hold_s.quarantine` |
| recover +`recover_step` on both factors, once per `recover_tick_s` | calm: score < `recover_below` or impact < min_victim_impact / 2 | `hold_s.recover` first |
| warn → normal | calm | `hold_s.warn` after an action, `hold_s.recover` for a plain warning |

"Held for X s" means at least `noisy.hold_ratio` (0.8) of the ticks in the last X s qualified, and so does
the current tick: one noisy dip does not restart the clock, and the ladder never moves on a tick that
contradicts the move. Every change restarts the hold clocks. Between "flagged" and "calm" the ladder
holds still (hysteresis).
While the attack persists this is AIMD: probe up while neighbours are fine, halve when they hurt again.

**Actions** are `LimitsCmd` with **both** `factor` and `weight_factor` from the ladder state on every send,
renewal and recovery step; TTL = `brain.ttl_s.throttle` (quarantine: `ttl_s.quarantine`), renewed every
TTL/4 while needed, retried after 1 s if the gateway refused or was unreachable, and an explicit 1.0/1.0
clear at the end. Actions only happen in mode `tenantguard` (read from the telemetry frame); leaving that
mode resets every ladder, clears overrides and closes incidents. If telemetry goes stale the ladders hold.

## Stage 3: servers (`health.py`, `rescue.py`, `decoder.py`, `stage3.py`; pure)

**Health.** Each tick, each server's latency sample is the *median over its tenants* of their cell p95, so
one tenant's own heavy requests don't make a server look slow. Its level is the median of the last
`health.probe_window_ticks` samples, and `peer_ratio = level / median(level of the other servers)`. A server
is slow when `peer_ratio ≥ slow_peer_ratio` **and** level − peer median ≥ `slow_abs_ms`. The state opens
after `open_after_ticks` consecutive slow ticks and heals after as many healthy ones. When a server has
too few tenant requests (for example after a rescue moved everyone away), the canary round trip is used
instead, compared only with other servers' canaries. A fleet-wide slowdown accuses nobody.
`ServerView.peer_ratio` / `status` come from here.

**Rescue.** For every tenant with slow and healthy servers, its traffic share moves off the slow servers
onto its healthy, *up* servers (`RouteWeightsCmd`, TTL `ttl_s.relocate`, renewed every TTL/4). The move is
capped so no healthy server's projected CPU exceeds `rescue_capacity_guard` (0.7); lightest tenants go
first. Tenants with every server slow are *stuck* (decoder evidence). Once named, a culprit is never moved
by a rescue (the prover relocates it). When every server is healthy again, equal weights are sent for a
few seconds (= default routing) and the incident closes.

**Decoder.** `p_s = logistic(k · (peer_ratio − slow_peer_ratio))`. Each tenant's evidence is:
- `4·min p_s` over its 3 servers;
- `2·mean` of the still-unexplained `p_s`;
- `1·stuck`;
- `2·ledger hint × mean p_s`. Hints: bg CPU growth, eviction share, black-box growth on all its servers, paid
  usage over fair share. They only count where its servers are slow.

`host_fault` and `none` compete with the tenants, and a softmax gives the confidence. Explain-away
names up to 2 culprits: accept the top tenant, zero the slowness it explains, decode again. Slow servers
still unexplained are **host suspects**, with no tenant blamed. With two culprits, each one's confidence is
reported with the other explained away. Property-tested: all 12 single culprits and **all 66 culprit
pairs** are named exactly with clean signals; every single slow server is a host suspect.

Server incidents (`srv-…`): `detected` (slow servers, growth only) → `rescued` → `named` (once the
decoding has been stable for `open_after_ticks` ticks; `suspects` holds culprits and their confidence;
`report` holds p_s, confidences and host suspects) → `resolved` → `closed`. Only in mode `tenantguard`.
Rescue comes first and the ladder throttles only if harm persists: in the whale scenario the victims are
routed away within ~2 s and K is never throttled.

**Prover** (`prover.py`): the interface only (`ProofRequest` / `ProofResult`, `NullProver`). The relocation test
needs the Docker host and comes next.

## Incidents (`incidents.py`)

An incident opens when a tenant reaches warn (`detected` + `level_change`). Every level or factor change
emits `level_change` with `tenant`, `level`, `prev_level`, `action` and a plain-English `detail` (the rule
that fired + the evidence: shares, fair share, which neighbours are how much slower, score). Back to full
plan emits `resolved`; back to normal emits `closed`.

## Tunables with code defaults (not in tenantguard.yaml)

`health.min_requests: 4`, `health.min_canaries: 2`, `health.canary_s: 1.0`, and `brain.decoder.*`
(`k 8`, `w_min 4`, `w_mean 2`, `w_stuck 1`, `w_ledger 2`, `w_host 4`, `w_none 3`, `temperature 1`,
`min_confidence 0.5`, `max_culprits 2`, `host_p 0.5`).

## Tools

- `tools/local_stack.py`: real gateway + workers (+ brain) on offset ports, no Docker
  (`--workers w3,w4,w5`). Kill it after use.
- `tools/traffic.py`: light calm traffic for every tenant (or `--servers w3,w4,w5`: only tenants with a live
  server) + an optional whale.

## Fake gateway (develop without T1/T2)

```
python tools/fake_gateway.py --port 8099 [--attacker K --calm 20 --attack 40 --after 60 --quota-mult 3 --loop]
export TENANTGUARD_GATEWAY_URL=http://127.0.0.1:8099
export TENANTGUARD_WORKER_URLS=$(python tools/fake_gateway.py --port 8099 --print-worker-urls)
python -m tenantguard_brain
```

It streams `TelemetryFrame`s from `tenantguard_brain/sim.py` (a toy fleet, not measurements), serves
`WorkerStats` per server under `/fake/workers/{server}/stats`, and accepts `/control/*` (limits are applied to
the simulation, so throttling really changes the next frames). `GET /fake/control_log` lists what it received,
`GET /fake/scenario` shows the phase, `POST /fake/scenario/restart` restarts it. Route weights are applied to
the simulation too. `POST/DELETE /fake/workers/{server}/admin/fault {latency_ms}` makes a faulty host, and
`/fake/workers/{server}/canary` answers with that server's latency. `/admin/*` and `/control/*` need the
control token, like the real gateway. `--quota-mult 10 --rps 25`
gives a whale strong enough to reach deprioritize.

## Tests

`cd brain && python -m pytest -q`: score, ladder and fairness math; the simulator scenarios (quiet fleet
has no incident, whale detected → throttled → recovered, only the culprit is touched, both factors always
sent, renewals, retries, mode gating, `completed == 0` ticks skipped, worker restarts); the HTTP/WS API.
