"""Calibrate server health on a running stack: watch the brain for N minutes and report.

    python brain/tools/calibrate_health.py --brain http://<host>:8200 --minutes 10 [--json out.json]

Polls GET /api/debug once a second. For every server it prints the peer_ratio distribution
(p50 / p95 / p99 / max) per health source (traffic / canary / none), the share of samples that were
"slow now", and the excess latency. It also lists every incident opened during the run (GET /api/incidents).
Exit code 1 if any incident opened: on a quiet (calm or idle) run that is the acceptance test.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", default="http://127.0.0.1:8200")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    t0 = time.time()
    end = t0 + args.minutes * 60
    ratio = defaultdict(lambda: defaultdict(list))       # server -> source -> [peer_ratio]
    excess = defaultdict(list)
    slow_now = defaultdict(int)
    samples = defaultdict(int)
    tenants_used = defaultdict(list)
    while time.time() < end:
        try:
            d = get(args.brain + "/api/debug")
        except Exception as e:
            print(f"poll failed: {e}", file=sys.stderr)
            time.sleep(1)
            continue
        for s, h in (d.get("servers") or {}).items():
            samples[s] += 1
            ratio[s][h["source"]].append(h["peer_ratio"])
            excess[s].append(h.get("excess_ms", 0.0))
            tenants_used[s].append(h.get("tenants_used", 0))
            slow_now[s] += int(h["slow_now"])
        el = time.time() - t0
        if int(el) % 60 == 0:
            print(f"... {el / 60:.0f} min", flush=True)
        time.sleep(1.0)
    evs = [e for e in get(args.brain + "/api/incidents?limit=1000") if e["ts"] >= t0]
    opened = [e for e in evs if e["stage"] == "detected"]
    print(f"\n{'server':10} {'samples':>7} {'source':>8} {'p50':>6} {'p95':>6} {'p99':>6} {'max':>6} "
          f"{'slow_now%':>9} {'excess p95':>10} {'tenants':>7}")
    report = {}
    for s in sorted(samples):
        for src, xs in sorted(ratio[s].items()):
            print(f"{s:10} {len(xs):7d} {src:>8} {pct(xs, .5):6.2f} {pct(xs, .95):6.2f} {pct(xs, .99):6.2f} "
                  f"{max(xs):6.2f} {100.0 * slow_now[s] / samples[s]:8.1f}% {pct(excess[s], .95):10.1f} "
                  f"{pct(tenants_used[s], .5):7.0f}")
        report[s] = {src: {"n": len(xs), "p50": pct(xs, .5), "p95": pct(xs, .95), "p99": pct(xs, .99),
                           "max": max(xs)} for src, xs in ratio[s].items()}
        report[s]["slow_now_share"] = slow_now[s] / samples[s]
    print(f"\nincidents opened during the run: {len(opened)}")
    for e in opened:
        print(f"  {e['incident_id']} {e.get('tenant') or '-'}: {e['detail'][:200]}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"minutes": args.minutes, "servers": report, "incidents": evs}, f, indent=1)
    return 1 if opened else 0


if __name__ == "__main__":
    sys.exit(main())
