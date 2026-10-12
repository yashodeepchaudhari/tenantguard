# TenantGuard brain (Wave 1 = Stage 2)

Control plane on **:8200**. It watches every tenant's usage and latency, detects a noisy neighbour,
throttles it with a graduated, self-recovering ladder, explains every action, and streams live state
to the dashboard.

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

Docker: `docker compose up brain` (image from `brain/Dockerfile`, build context = repo root).

## API

| | |
|---|---|
| `WS /ws/events` | on connect: one `StateSnapshot` + the last 100 `IncidentEvent`s; then a `StateSnapshot` every 500 ms and every `IncidentEvent` as it happens |
| `GET /api/state` | `StateSnapshot` |
| `GET /api/incidents?incident_id=&tenant=&limit=200` | `List[IncidentEvent]`, oldest first |
| `GET /healthz` | telemetry / gateway / worker reachability, counters |
| `GET /api/debug` | scores, ladder state, baselines, last command per tenant |

## Loops (`runtime.py`)

- telemetry WS `/internal/telemetry` → `Brain.ingest_frame`, reconnect with backoff 0.5 → 5 s
- worker `/stats` every `stats_poll_s`, cumulative counters diffed (a `started_at` change = restart, no diff)
- `POST /control/heartbeat` every `gateway.fail_static_after_s / 4` (= 500 ms)
- decision tick every `brain.tick_ms`; `LimitsCmd`s are sent without blocking the tick
- UI snapshot broadcast at 2 Hz

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
- **baseline_p95** = per-tenant EWMA (`noisy.baseline_alpha`, default 0.05), updated only while the fleet is
  calm (no open incident, nobody flagged) and ignoring > 2× outliers. The baseline is floored at
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

Every change restarts the hold clocks. Between "flagged" and "calm" the ladder holds still (hysteresis).
While the attack persists this is AIMD: probe up while neighbours are fine, halve when they hurt again.

**Actions** are `LimitsCmd` with **both** `factor` and `weight_factor` from the ladder state on every send,
renewal and recovery step; TTL = `brain.ttl_s.throttle` (quarantine: `ttl_s.quarantine`), renewed every
TTL/4 while needed, retried after 1 s if the gateway refused or was unreachable, and an explicit 1.0/1.0
clear at the end. Actions only happen in mode `tenantguard` (read from the telemetry frame); leaving that
mode resets every ladder, clears overrides and closes incidents. If telemetry goes stale the ladders hold.

## Incidents (`incidents.py`)

An incident opens when a tenant reaches warn (`detected` + `level_change`). Every level or factor change
emits `level_change` with `tenant`, `level`, `prev_level`, `action` and a plain-English `detail` (the rule
that fired + the evidence: shares, fair share, which neighbours are how much slower, score). Back to full
plan emits `resolved`; back to normal emits `closed`.

## Tunables not (yet) in tenantguard.yaml

Read with `.get()` and these defaults; the coordinator may add them under `brain.noisy`:
`min_victim_impact: 0.2`, `baseline_alpha: 0.05`, `recover_tick_s: 1.0`, `throttle_floor: 0.25`,
`deprioritize_weight: 0.25`, `quarantine_factor: 0.1`, `quarantine_weight: 0.1`.

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
`GET /fake/scenario` shows the phase, `POST /fake/scenario/restart` restarts it. `--quota-mult 10 --rps 25`
gives a whale strong enough to reach deprioritize.

## Tests

`cd brain && python -m pytest -q`: score, ladder and fairness math; the simulator scenarios (quiet fleet
has no incident, whale detected → throttled → recovered, only the culprit is touched, both factors always
sent, renewals, retries, mode gating, `completed == 0` ticks skipped, worker restarts); the HTTP/WS API.
