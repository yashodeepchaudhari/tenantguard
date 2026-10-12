"""Measure the noisy-neighbor effect on real workers.

Tenant A probes /api/light on a victim worker and a control worker; tenant K attacks the
victim worker only during the attack phase:
  --attack search : hammer /api/search?n=  (CPU visible in K's handler meter)
  --attack ingest : POST /api/ingest {docs} (CPU burnt in the black-box child, invisible to K's meter)

    python tools/noisy_demo.py --victim http://127.0.0.1:8101 --control http://127.0.0.1:8100
    python tools/noisy_demo.py --attack ingest --docs 100 --attackers 1 --attack-gap-ms 100
"""
from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from typing import Dict, List

import httpx

from tenantguard_common.contracts import H_CPU_MS, H_TENANT


def pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--victim", default="http://127.0.0.1:8101")
    ap.add_argument("--control", default="http://127.0.0.1:8100")
    ap.add_argument("--attack", choices=["search", "ingest"], default="search")
    ap.add_argument("--n", type=int, default=500_000, help="search size for the attacker")
    ap.add_argument("--docs", type=int, default=100, help="docs per ingest request")
    ap.add_argument("--attack-gap-ms", type=float, default=0, help="pause between attacker requests")
    ap.add_argument("--attackers", type=int, default=4)
    ap.add_argument("--probe-gap-ms", type=float, default=20)
    ap.add_argument("--phases", default="baseline:8,attack:12,after:6")
    ap.add_argument("--out", help="write the result JSON here")
    args = ap.parse_args()

    phases = [(p.split(":")[0], float(p.split(":")[1])) for p in args.phases.split(",")]
    phase = {"name": phases[0][0]}
    stop = threading.Event()
    samples: Dict[str, Dict[str, List[float]]] = {s: {p: [] for p, _ in phases} for s in ("victim", "control")}
    attack_stats = {"requests": 0, "errors": 0, "cpu_ms": 0.0}

    def probe(which: str, url: str) -> None:
        with httpx.Client(base_url=url, headers={H_TENANT: "A"}, timeout=10) as c:
            while not stop.is_set():
                t0 = time.perf_counter()
                r = c.get("/api/light")
                ms = (time.perf_counter() - t0) * 1000
                if r.status_code == 200:
                    samples[which][phase["name"]].append(ms)
                time.sleep(args.probe_gap_ms / 1000)

    def attack() -> None:
        with httpx.Client(base_url=args.victim, headers={H_TENANT: "K"}, timeout=30) as c:
            while not stop.is_set():
                if phase["name"] != "attack":
                    time.sleep(0.05)
                    continue
                if args.attack == "search":
                    r = c.get("/api/search", params={"n": args.n})
                else:
                    r = c.post("/api/ingest", json={"docs": args.docs})
                attack_stats["requests"] += 1
                if r.status_code >= 300:
                    attack_stats["errors"] += 1
                else:
                    attack_stats["cpu_ms"] += float(r.headers[H_CPU_MS])
                if args.attack_gap_ms:
                    time.sleep(args.attack_gap_ms / 1000)

    def victim_stats() -> dict:
        s = httpx.get(args.victim + "/stats", timeout=10).json()
        k = next((t for t in s["tenants"] if t["tenant"] == "K"), {})
        return {"cpu_total_ms": s["cpu_total_ms"], "blackbox_cpu_ms": s["blackbox_cpu_ms"],
                "tenants_cpu_ms": sum(t["handler_cpu_ms"] + t["bg_cpu_ms"] for t in s["tenants"]),
                "K_cpu_ms": k.get("handler_cpu_ms", 0.0) + k.get("bg_cpu_ms", 0.0)}

    threads = [threading.Thread(target=probe, args=("victim", args.victim)),
               threading.Thread(target=probe, args=("control", args.control))]
    threads += [threading.Thread(target=attack) for _ in range(args.attackers)]
    s0 = victim_stats()
    for t in threads:
        t.start()
    for name, secs in phases:
        phase["name"] = name
        print(f"phase {name} ({secs:.0f}s)", flush=True)
        time.sleep(secs)
    stop.set()
    for t in threads:
        t.join()
    s1 = victim_stats()
    delta = {k: round(s1[k] - s0[k], 1) for k in s0}

    what = f"/api/search?n={args.n}" if args.attack == "search" else f"POST /api/ingest docs={args.docs}"
    result = {"victim": args.victim, "control": args.control,
              "attack": {"kind": args.attack, "n": args.n, "docs": args.docs, "concurrency": args.attackers,
                         "gap_ms": args.attack_gap_ms, **attack_stats, "cpu_ms": round(attack_stats["cpu_ms"], 1)},
              "victim_stats_delta": delta, "light_ms": {}}
    print(f"\nTenant A /api/light latency (ms); K sends {what} to {args.victim} x{args.attackers}")
    print(f"{'server':8} {'phase':9} {'count':>6} {'p50':>8} {'p95':>8} {'p99':>8}")
    for which in ("victim", "control"):
        result["light_ms"][which] = {}
        for name, _ in phases:
            xs = samples[which][name]
            row = {"count": len(xs), "p50": round(statistics.median(xs), 2) if xs else None,
                   "p95": round(pct(xs, 95), 2), "p99": round(pct(xs, 99), 2)}
            result["light_ms"][which][name] = row
            print(f"{which:8} {name:9} {row['count']:>6} {row['p50']:>8} {row['p95']:>8} {row['p99']:>8}")
    print(f"attacker K: {attack_stats['requests']} requests, {attack_stats['errors']} errors, "
          f"{attack_stats['cpu_ms']:.0f} ms handler CPU (sum of {H_CPU_MS})")
    print(f"victim /stats delta over the run: {delta}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
