# TenantGuard brain

Control plane on **:8200**. It watches every tenant's usage and latency and every server's health.
- **Stage 2:** detects a noisy neighbour from usage share × victim impact and throttles it with a
  graduated, self-recovering ladder.
- **Stage 3:** tells *which server* is slow, rescues victims by routing around it within ~2-3 s, names
  the culprit from the barcode pattern (or a host suspect, with no tenant blamed), **proves** it by moving it
  to the spare (or draining the host), and acts on one of five verdicts.

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
| `TENANTGUARD_PROVER` | yaml `brain.prover` (`auto`) | `docker`: relocation proofs through the Docker socket (fails loudly if it doesn't answer); `null`: no proofs; `auto`: docker when `/var/run/docker.sock` answers |
| `TENANTGUARD_DOCKER_SOCKET` | `/var/run/docker.sock` | |
| `TENANTGUARD_SPARE_CONTAINER` | `$COMPOSE_PROJECT-spare-1` | else found by the compose label `com.docker.compose.service=spare` |

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
- each second: `ChargeCmd` per tenant for its background-job CPU since the last charge (workers meter it,
  the request header doesn't carry it), `charge_id = <tenant>-<epoch s>`, tenantguard mode only. A failed
  delivery is folded into the next charge.
- async tests as tasks: relocation proofs (DockerProver) and drain tests (HostProver); their results go
  back into the brain (`on_proof` / `on_host`), which picks the verdict and the actions
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
  CPU capacity; `queue_share` = its (queue depth + inflight) / their dispatch slots, **scaled by the share of
  its requests that succeeded** (failed / 5xx requests, e.g. fast 502s while its servers are down, are not
  slot usage: T9 saw L warned on 16% of slots with 0% CPU); `fair_share` =
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

**Health (tenant-relative).** For every (tenant, server) cell, its level over the last `health.window_s`
(3 s) is the count-weighted median of its per-tick p95, used only with at least `health.min_cell_requests`
(5) requests (5xx don't count). Each tenant's reference is the median of its levels across its own servers;
a server's `peer_ratio` is the median over its tenants (at least `health.min_tenants`, 2) of
level / reference. Comparing a tenant with itself cancels workload mix: a search-heavy tenant is slow
everywhere, and that is not the server's fault. The first host calm run (raw cross-server p95) flagged
servers at calm with ratios of 0.1-6.6; this is why. A server is slow when `peer_ratio ≥ slow_peer_ratio`
**and** the median excess ≥ `slow_abs_ms`; it opens after `open_after_ticks` consecutive slow ticks and heals
after as many healthy ones. A fleet-wide slowdown keeps every ratio near 1, so nobody is accused.

**Canary** (fallback): the brain's own `GET /canary` round trip, median of the last `health.canary_window_s`,
compared with the fleet's canaries, floor `health.canary_abs_ms` (8 ms). It is used when too few tenants have
data on a server (e.g. a rescue moved them away). **The canary can keep a slow server open, never open one:**
an incident needs slow real traffic first, so an idle host (probe jitter of a few ms; T13 saw incidents
every 20-30 s at idle) opens nothing. Pool servers (spare, quarantine) are judged against the fleet but are
never anyone's peer and never a fleet incident or a host suspect: they hold whoever we moved there.

**Rescue.** For every tenant with slow and healthy servers, its traffic share moves off the slow servers
onto its healthy, *up* servers (`RouteWeightsCmd`, TTL `ttl_s.relocate`, renewed every TTL/4). The move is
capped so no healthy server's projected CPU exceeds `rescue_capacity_guard` (0.7); lightest tenants go
first. Tenants with every server slow are *stuck* (decoder evidence). Once named, a culprit is never moved
by a rescue (the prover relocates it). A rescue is **held while the named culprit still uses more than its
fair share** of those servers (with the victims gone, its servers can look fine; lifting the rescue then
would just bring them back into harm), and while a test is running. When every server is healthy again,
equal weights are sent for a few seconds (= default routing), our drains are undone, and the incident closes.

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
`report` holds p_s, confidences and host suspects) → `proving` → `verdict` → `resolved` → `closed`. Only in
mode `tenantguard`. Rescue comes first and the ladder throttles only if harm persists.

## Proof and verdicts (`prover.py`, `verdicts.py`)

**DockerProver** (named culprit, confidence ≥ `confidence_to_prove`, within the relocation budget):
1. Size the spare to `spare_sizing` × its footprint (paid + black-box CPU on its servers), clamped to
   `verdicts.min/max_spare_cores`. It uses the Docker Engine API on the mounted socket (`NanoCpus`, which is
   what compose's `cpus:` sets; docker-py's `update(cpu_quota=…)` is refused on such containers).
2. `RelocateCmd` it to `spare`, wait `prove_settle_s`, then sample every `verdicts.prove_sample_s` for
   `prove_window_s`.
3. Compute R = 1 − post/pre slowdown on its original servers.
4. Confirm when R ≥ `recovery_R` **and** the harm followed it: the spare's CPU rose by ≥ `harm_follow_share` of
   the footprint, or its latency reached `harm_follow_ratio` × peers.

A culprit that pauses mid-test makes its servers recover while the spare stays calm, so it is not
confirmed. The evidence for the verdict is captured before the move.

**HostProver** (host suspect, ≤ `max_drained` drained, peers below `drain_peer_busy`): `DrainCmd`, wait, measure
it idle by canary. Still ≥ `host_idle_ratio` × peers means the host is at fault.

| Verdict | When | Action (TTL-bound) |
|---|---|---|
| `mitigated_no_blame` | confidence below the bar, or not confirmed (and not a repeat offender) | back home; nobody penalised; the rescue protects the victims |
| `over_plan` | confirmed, and admission refuses ≥ `over_plan_rejected_share` of its requests | back home; limits tightened via the ladder (throttle_factor; AIMD relaxes them) |
| `costly_pattern` | confirmed, and paid share < `paid_share_costly`, or black-box / eviction evidence | the class that grew most vs its calm baseline → `quarantine` (heavy lane, `ttl_s.quarantine`, ≤ 1 per `quarantine_every_s`); the rest home |
| `honest_growth` | confirmed, and paid work explains the load | stays on the spare as a burst lane (`ttl_s.relocate`), no penalty |
| `host_fault` | drained and idle, still slow | stays drained (renewed) until healthy, then undrained |

The **action budget** limits relocations per minute, quarantines and drains; a deferred test is noted once.
**Repeat offender**: named `repeat_count` times in `repeat_window_s` with a known last verdict → that verdict
again at once, without re-proving. The spare goes back to its size unless the tenant stays there as a burst lane.

## Incidents (`incidents.py`)

An incident opens when a tenant reaches warn (`detected` + `level_change`). Every level or factor change
emits `level_change` with `tenant`, `level`, `prev_level`, `action` and a plain-English `detail` (the rule
that fired + the evidence: shares, fair share, which neighbours are how much slower, score). Back to full
plan emits `resolved`; back to normal emits `closed`.

## Tunables (tenantguard.yaml `brain.*`; code defaults until the coordinator adds them)

```yaml
brain:
  prover: auto                  # auto | docker | null  (env TENANTGUARD_PROVER wins)
  charge_s: 1.0
  health:                       # + probe_window_ticks, slow_peer_ratio, slow_abs_ms, open_after_ticks
    window_s: 3.0
    min_cell_requests: 5
    min_tenants: 2
    canary_s: 1.0
    min_canaries: 3
    canary_window_s: 6.0
    canary_abs_ms: 8.0
  decoder: {k: 8, w_min: 4, w_mean: 2, w_stuck: 1, w_ledger: 2, w_host: 4, w_none: 3,
            temperature: 1.0, min_confidence: 0.5, max_culprits: 2, host_p: 0.5}
  verdicts: {over_plan_rejected_share: 0.2, harm_follow_share: 0.3, harm_follow_ratio: 1.6,
             evidence_hint: 0.3, repeat_count: 3, repeat_window_s: 600,
             prove_sample_s: 0.5, min_spare_cores: 0.25, max_spare_cores: 4.0}
  action_budget: {drain_peer_busy: 0.75}     # + relocations_per_min, quarantine_every_s, max_drained
```

## Tools

- `tools/local_stack.py`: real gateway + workers (+ brain) on offset ports, no Docker
  (`--workers w3,w4,w5`). Kill it after use.
- `tools/traffic.py`: light calm traffic for every tenant (or `--servers w3,w4,w5`: only tenants with a live
  server) + an optional whale.
- `tools/calibrate_health.py --brain http://<host>:8200 --minutes 10`: per-server peer_ratio distribution
  by source, slow-now share, and every incident opened. Exit code 1 if any opened (quiet-run acceptance).

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

`cd brain && python -m pytest -q`. Tests use this checkout's `tenantguard.yaml` and layout (conftest sets
`TENANTGUARD_ROOT`), not the main repo's. They cover:
- the score, ladder, fairness, health, rescue, decoder and verdict math;
- the provers with a fake Docker and scripted readings;
- the simulator scenarios, with proofs and drain tests run inside the simulation (`drive_jobs`): host fault
  → host_fault; whale → honest_growth; black-box whale → costly_pattern with the heavy class quarantined;
  a paused culprit → mitigated_no_blame; over_plan; repeat offender; budget deferral;
- the regressions: L's fast 502s are not slot usage; idle canary jitter opens nothing; workload mix is not
  slowness;
- charges, operator holds, runtime send ordering and adoption, and the HTTP/WS API.
