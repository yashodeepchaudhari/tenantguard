# tools/

## demo.py: the 4-minute demo, one command
```
python tools/demo.py --dry-run            # the plan + every HTTP call it would make; sends nothing
python tools/demo.py                      # timed run against the target
python tools/demo.py --step               # Enter before each step (presenter paces it)
python tools/demo.py --only tenantguard   # calm + one step;  --speed 0.5 halves every duration (rehearsal)
python -m pytest tools                    # step sequencing against mocked HTTP
```
Target: `TENANTGUARD_GATEWAY_URL`, `TENANTGUARD_BRAIN_URL`, `TENANTGUARD_LAB_URL` (default `http://100.112.232.8:{8000,8200,8300}`, the Docker host) and `TENANTGUARD_CONTROL_TOKEN` (default `dev`). The driver only uses the HTTP APIs: gateway `POST /admin/mode`; lab `/lab/calm/start`, `/lab/attack`, `DELETE /lab/attacks`, `/lab/metrics`, `/lab/results`; brain `/api/state`, `/api/incidents`.

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
