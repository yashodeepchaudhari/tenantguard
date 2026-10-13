# TenantGuard traffic lab (:8300)

Fills the platform with 12 tenant personas (Poisson arrivals, per-tenant request mix), launches and stops
attacks, and runs scripted scenarios. Each scenario writes a measured `RunResult` to `lab/results/`, and the
slide numbers come only from these files.

## Run
```
cd lab
pip install -r requirements.txt                 # tenantguard_common must already be installed
python -m tenantguard_lab serve                 # API on 0.0.0.0:8300; starts calm traffic (autostart)
python -m tenantguard_lab calm                  # start calm traffic (on the server, or in-process if none)
python -m tenantguard_lab attack whale K [--intensity 1] [--duration 60]
python -m tenantguard_lab stop <attack_id|all>
python -m tenantguard_lab status | metrics [--window 10]
python -m tenantguard_lab run whale tenantguard [--tenant K] [--calm-s 20] [--attack-s 40] [--local]
python -m tenantguard_lab calibrate [--seconds 20]   # measures fleet CPU via worker /stats, suggests rate_scale
python -m tenantguard_lab scorecard [--scenarios whale,flood] [--modes off,industry,tenantguard] [--runs 3] [--aggregate-only] [--out PATH]
python -m pytest -q
docker compose --profile lab up lab             # from the repo root
```
CLI commands talk to a running lab server (`TENANTGUARD_LAB_URL`) when one answers, so the dashboard sees
the same attacks. Otherwise, or with `--local`, they run in-process.

Env:
- `TENANTGUARD_GATEWAY_URL`: target. Default: `tenantguard.yaml services.gateway`.
- `TENANTGUARD_BRAIN_URL`: used for time-to-detect.
- `TENANTGUARD_WORKER_URLS`: used by `calibrate`.
- `TENANTGUARD_UI_ORIGINS`: CORS. Comma list, default `http://127.0.0.1:5173`.
- `TENANTGUARD_CONTROL_TOKEN`: sent on `/admin/mode`. Default `dev`.
- `TENANTGUARD_LAB_RATE_SCALE`, `TENANTGUARD_LAB_AUTOSTART`, `TENANTGUARD_LAB_PERSONAS`, `TENANTGUARD_LAB_RESULTS`.

## API
| Endpoint | |
|---|---|
| `GET /lab/status` | `LabStatus` (attacks, calm_running, run in progress) |
| `POST /lab/attack` | `AttackReq` → `AttackStatus`. Attacks not included in this build (blackbox_bomb, background_bomb, cache_flood, faulty_server, honest_spike) → 501, unknown tenant → 404, intensity outside 0.1..5 → 422 |
| `DELETE /lab/attack/{id}` / `DELETE /lab/attacks` | stop one / all → `AttackStatus` (`state: stopped`) |
| `POST /lab/calm/start` `{rate_scale?}` / `POST /lab/calm/stop` | calm traffic on/off → `LabStatus` |
| `GET /lab/metrics?window_s=10` | per tenant, client-side: p50/p95/p99, count/ok/429/503/err per second, split calm vs attack; attack counters; lab event-loop lag |
| `GET /lab/personas` | personas with effective rates and the CPU estimate |
| `POST /lab/run` (`RunReq`) | 202 + run view. Sets the gateway mode, then runs calm → attack → calm. 409 if a run is in progress |
| `GET /lab/run/{id}`, `DELETE /lab/run` | run progress (phase, result) / cancel (attack stopped, calm restored) |
| `GET /lab/results?scenario=&mode=` | every `RunResult` in `lab/results/`, newest first (the Scorecard reads this) |

## Traffic
- **Personas** (`personas.yaml`): rps, mix over `light/read/search/write`, SLA and profile fields. The effective
  rate is `rps × rate_scale`. Arrivals are Poisson on an absolute schedule, so loop jitter doesn't lower the rate.
- **Pools:** each tenant (and each attack) has its own keep-alive httpx pool. An attacker's connections never
  queue a victim's request on the client side. Open-loop streams cap in-flight requests (`client.max_inflight_*`);
  arrivals over the cap are counted as `client_dropped`.
- **Attacks** run in their own OS process (`client.attack_isolation: process`). httpx costs ~3 ms CPU per
  request on the dev laptop, so attack traffic sharing the victims' event loop would inflate victim latency
  and the results would measure the lab, not the platform. Samples stream back to the parent every 250 ms.
  `GET /lab/metrics` reports the lab's own event-loop lag. If its p99 is above ~20 ms, treat client
  latencies with care.
- **Attack profiles** at intensity 1.0, scaled by `intensity`:
  - `flood`: 10× persona rps of `/api/light`.
  - `whale`: 2 rps of `/api/search?n=1.5M` (~0.5 s CPU each).
  - `hog`: 24 closed-loop `/api/export` (~1 s each).
  - `memory`: 4 rps × 24 MB held 1 s (exceeds the 32 MB tenant budget).
  - `retry_storm`: 3× persona rps of `/api/read`; each 429/503 is retried immediately, ignoring Retry-After, up to 50 times.
  - `background_bomb`, `blackbox_bomb`, `cache_flood`, `faulty_server`, `honest_spike`: not included in this build (501). The faulty-server scene is produced with the worker's `/admin/fault` instead.

## Measurement (`RunResult`)
- Timeline: warmup, then 10 s more if calm traffic was cold, then `calm_s` baseline, attack for `attack_s`,
  then `calm_s` after.
- `attack_start` is marked once the attack process is actually sending; spawning it takes ~4 s on Windows.
- Victims are every tenant except the attacker. Only their calm requests of `run.victim_kinds`
  (default `light`, `read`: the interactive SLO probes) count, so a victim's own heavy searches don't set its p99.
- p99 uses linear interpolation (numpy's default).
- `time_to_recover_s`: seconds from attack start to the start of the first 5 consecutive 1 s buckets whose
  pooled victim p99 is ≤ 1.5 × baseline, counted after victims first degraded. It is 0 if they never degraded,
  and null if they never recovered.
- `time_to_detect_s`: the brain's first incident event (`/api/incidents?tenant=<attacker>`) after attack start.
  Null if the brain is unreachable.
- `attacker_429/503`: all of the attacker's responses from attack start to run end.

## Scorecard (`scorecard.py`)
`python -m tenantguard_lab scorecard` runs every scenario in every mode `--runs` times through the lab server
(`POST /lab/run`, one at a time, `--cooldown-s` apart; scenarios the lab answers 501 for are skipped and listed),
then writes a summary to `results/summary-<ts>.json` (or `--out`, `-` for stdout). Per scenario x mode cell: median
and mean victim p99 (baseline / attack / after), time to detect and recover, attacker 429/503 medians, and, from the
brain's incidents inside each run's real attack window (run marks), whether the culprit was named and which
innocent tenants were penalised. `--aggregate-only` summarises the existing result files and runs nothing.
The committed Docker-host summary is `results/summary-scorecard-host.json` (whale and flood, 3 runs per cell).

## Calibration
`personas.yaml` targets ~30% of the Docker fleet (9 × 0.5 cores = 4500 CPU-ms/s). Most of that CPU comes from
request weight (search n), not request count, because the gateway and the lab both pay per-request Python
overhead. Measured on 9 local workers: 1000 CPU-ms/s against an estimate of 1295 (`calibrate` suggested
`rate_scale` 1.35). That was a CPU-starved laptop, so recalibrate on the Docker host. On a single laptop,
use `TENANTGUARD_LAB_RATE_SCALE=0.25`.

## Tools
`tools/stub_gateway.py`: a stand-in gateway. It sleeps 2 ms, or with `STUB_FORWARD=1` passes through to real
workers by barcode, and has a naive per-tenant 429 limiter outside `off` mode.
