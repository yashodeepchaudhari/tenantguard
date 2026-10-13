# tools/

## demo.py: the 4-minute demo, one command
```
python tools/demo.py --dry-run            # the plan + every HTTP call it would make; sends nothing
python tools/demo.py                      # timed run against the target
python tools/demo.py --step               # Enter before each step (presenter paces it)
python tools/demo.py --only tenantguard   # calm + one step;  --speed 0.5 halves every duration (rehearsal)
python -m pytest tools                    # step sequencing against mocked HTTP
```
Target: `TENANTGUARD_GATEWAY_URL`, `TENANTGUARD_BRAIN_URL`, `TENANTGUARD_LAB_URL` (default: `tenantguard.yaml` `services`; set them to the Docker host's address for the real show), `TENANTGUARD_CONTROL_TOKEN` (default `dev`), and for the faulty-server fallback `TENANTGUARD_WORKER_URLS` (default: `workers`) and `TENANTGUARD_ADMIN_TOKEN` (default `dev`). The driver only uses the HTTP APIs: gateway `POST /admin/mode`; lab `/lab/calm/start`, `/lab/attack`, `DELETE /lab/attacks`, `DELETE /lab/attack/{id}`, `/lab/metrics`, `/lab/results`; brain `/api/state`, `/api/incidents`; worker `/healthz`, `/admin/fault` (fallback only).

**Preflight:** before the show the driver asks the lab which attacks it can run, without starting any (a probe with an unknown tenant: a shipped attack answers 4xx, an unshipped one 501). The bomb falls back to `whale`. `faulty_server` falls back to a direct worker `POST /admin/fault` (TTL-bound by `duration_s`) only if that worker answers `/healthz` from here; on the Docker host workers are not published, so the step is skipped. Every step that cannot run is printed as `[NOT AVAILABLE]` up front and skipped in the show.

**Cleanup** (normal exit, error, Ctrl-C): stop all attacks (retry, then one by one; anything left is printed), clear injected faults, mode back to `tenantguard`. A second Ctrl-C during cleanup is ignored.

| # | Step | s | Mode | Attack |
|---|---|---|---|---|
| 1 | Calm | 15 | tenantguard | none (calm traffic) |
| 2 | OFF + flood | 30 | off | flood K |
| 3 | Industry + same flood | 15 | industry | flood K (refused with 429s) |
| 4 | Industry + black-box bomb | 30 | industry | blackbox_bomb K (falls back to whale if the lab answers 501) |
| 5 | TenantGuard + same attack | 75 | tenantguard | blackbox_bomb K; incident stages printed as they happen |
| 6 | Faulty server | 20 | tenantguard | faulty_server w4 |
| 7 | Honest spike | 20 | tenantguard | honest_spike J |
| 8 | Scorecard | 25 | | table from `/lab/results` (latest run per scenario × mode) |
| 9 | Close | 10 | | |

- Every `--tick` seconds (default 5), each step prints one line of live numbers:
  - from the brain: mode, fairness, utilization, worst victim p95, attacker level and factor, plus the status sentence;
  - from the lab: client-side victim p95 (calm traffic only), attacker ok/s and 429/s.
- Each step stops the previous attacks before starting its own.
- A failed attack is reported, and the demo goes on.
- On exit (normal, error or Ctrl-C), all attacks are stopped and the mode returns to `tenantguard`.
- Options: `--attacker`, `--bomb {blackbox_bomb,whale}`, `--faulty-server`, `--spiker`.
