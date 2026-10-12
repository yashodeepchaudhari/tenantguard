"""Measure the cost of every worker endpoint at default params → worker/calibration.json.

Sequential, one request at a time, keep-alive, as tenant A. Per endpoint it records:
- handler_cpu_ms   : X-TenantGuard-Cpu-Ms (what the gateway's cost bucket is debited)
- process_cpu_ms   : /stats cpu_total_ms delta per request (handler + HTTP/framework overhead
                     + background + black box): use this to size traffic against the CPU limit
- bg_cpu_ms, blackbox_cpu_ms : deferred CPU per request (write / ingest)
- latency_ms       : client-side wall time

Inside a worker container (real 0.5-CPU cgroup; the image ships worker/tools), from the Docker host:
    docker compose exec -T w1 python tools/calibrate.py --where docker --out - > cal.tmp && mv cal.tmp worker/calibration.json
(The tool waits up to --wait-s for /healthz; on any failure it exits non-zero with nothing on stdout, and the
temp-file + mv keeps the committed calibration.json intact.)
From another machine against a published port:
    python tools/calibrate.py --url http://<host>:18101 --where docker-remote
Stdlib only, so it runs anywhere. Progress goes to stderr; with --out - the JSON goes to stdout.
"""
from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

TENANT_HEADER = "X-TenantGuard-Tenant"
CPU_HEADER = "X-TenantGuard-Cpu-Ms"
TENANT = "A"

# name, method, path, params/body, samples
PLAN = [
    ("light", "GET", "/api/light", None, 200),
    ("read_cold", "GET", "/api/read", "cold", 100),     # distinct keys: cache miss → SQLite
    ("read_warm", "GET", "/api/read", "warm", 200),     # same keys again: cache hit
    ("search", "GET", "/api/search", None, 60),         # default n=20000
    ("write", "POST", "/api/write", "write", 100),      # + background index job (default rounds)
    ("ingest", "POST", "/api/ingest", "ingest", 40),    # default docs=10 → black box
    ("memory", "GET", "/api/memory", {"mb": 1}, 10),    # default hold_ms=500
    ("export", "GET", "/api/export", None, 10),         # default rows=1000, chunk=100, pause 20 ms
    ("canary", "GET", "/canary", None, 100),
]


def pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


class Client:
    def __init__(self, url: str) -> None:
        u = urlsplit(url)
        self.conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=60)

    def call(self, method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None,
             tenant: Optional[str] = TENANT) -> Tuple[int, dict, float, float]:
        if query:
            path = f"{path}?{urlencode(query)}"
        headers = {TENANT_HEADER: tenant} if tenant else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        t0 = time.perf_counter()
        self.conn.request(method, path, body=data, headers=headers)
        resp = self.conn.getresponse()
        raw = resp.read()
        ms = (time.perf_counter() - t0) * 1000
        return resp.status, json.loads(raw or b"{}"), float(resp.getheader(CPU_HEADER) or 0), ms

    def stats(self) -> dict:
        return self.call("GET", "/stats", tenant=None)[1]

    def wait_idle(self, timeout: float = 120) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            h = self.call("GET", "/healthz", tenant=None)[1]
            if h.get("bg_pending", 0) == 0 and h.get("blackbox", {}).get("queued", 0) == 0:
                return
            time.sleep(0.1)


def tenant_row(stats: dict) -> dict:
    return next(t for t in stats["tenants"] if t["tenant"] == TENANT)


def wait_healthy(url: str, wait_s: float) -> None:
    end = time.monotonic() + wait_s
    last = ""
    while True:
        try:
            c = Client(url)
            status, body, _, _ = c.call("GET", "/healthz", tenant=None)
            if status == 200 and body.get("ok") and body.get("blackbox", {}).get("running", True):
                return
            last = f"HTTP {status} {body}"
        except (OSError, http.client.HTTPException, ValueError) as e:
            last = repr(e)
        if time.monotonic() >= end:
            raise SystemExit(f"worker at {url} not healthy after {wait_s:.0f} s: {last}")
        time.sleep(0.5)


def run(url: str, where: str, scale: float) -> dict:
    c = Client(url)
    s = c.stats()
    result: Dict[str, dict] = {}
    key_i = 0
    for name, method, path, arg, n in PLAN:
        n = max(3, int(n * scale))
        c.wait_idle()
        s0 = c.stats()
        cpu, lat = [], []
        warm_keys = [f"k{i:04d}" for i in range(min(n, 50))]
        for i in range(n):
            query, body = None, None
            if arg == "cold":
                query = {"key": f"k{(500 + key_i) % 1000:04d}"}
                key_i += 1
            elif arg == "warm":
                query = {"key": warm_keys[i % len(warm_keys)]}
            elif arg == "write":
                body = {"value": "calibration doc about tenant isolation and fair share " * 4}
            elif arg == "ingest":
                body = {}
            elif isinstance(arg, dict):
                query = arg
            status, _, cpu_ms, ms = c.call(method, path, query, body)
            if status >= 300:
                raise SystemExit(f"{name}: HTTP {status}")
            cpu.append(cpu_ms)
            lat.append(ms)
        c.wait_idle()
        s1 = c.stats()
        a0, a1 = tenant_row(s0), tenant_row(s1)
        result[name] = {
            "method": method, "path": path, "params": arg if isinstance(arg, dict) else None, "samples": n,
            "handler_cpu_ms": {"mean": round(statistics.mean(cpu), 3), "p95": round(pct(cpu, 95), 3)},
            "latency_ms": {"mean": round(statistics.mean(lat), 3), "p50": round(pct(lat, 50), 3),
                           "p95": round(pct(lat, 95), 3)},
            "bg_cpu_ms": round((a1["bg_cpu_ms"] - a0["bg_cpu_ms"]) / n, 3),
            "blackbox_cpu_ms": round((s1["blackbox_cpu_ms"] - s0["blackbox_cpu_ms"]) / n, 3),
            # includes the /stats + /healthz polls around the batch (a few ms per batch)
            "process_cpu_ms": round((s1["cpu_total_ms"] - s0["cpu_total_ms"]) / n, 3),
        }
        r = result[name]
        print(f"{name:10} n={n:4} handler {r['handler_cpu_ms']['mean']:8.3f} ms (p95 {r['handler_cpu_ms']['p95']:8.3f})"
              f"  process {r['process_cpu_ms']:8.3f}  bg {r['bg_cpu_ms']:6.3f}  bb {r['blackbox_cpu_ms']:7.3f}"
              f"  latency {r['latency_ms']['mean']:8.2f} ms (p95 {r['latency_ms']['p95']:8.2f})",
              file=sys.stderr, flush=True)
    return {
        "meta": {"where": where, "url": url, "server": s["server"], "cpu_limit_cores": s["cpu_limit_cores"],
                 "ts": time.time(), "tenant": TENANT, "client_platform": sys.platform,
                 "method": "sequential single client, keep-alive; latency measured client-side. In-container "
                           "runs share the cgroup with the client (adds client CPU to latency, not to "
                           "handler/process CPU); remote runs include network/port-forwarding time",
                 "notes": {"handler_cpu_ms": "X-TenantGuard-Cpu-Ms: what the gateway cost bucket is debited",
                           "process_cpu_ms": "worker process-tree CPU per request (incl. HTTP overhead, bg, "
                                             "black box): size fleet load with this against cpu_limit_cores",
                           "capacity_rps_at_limit": "cpu_limit_cores*1000/process_cpu_ms (one worker, 100% CPU)",
                           "windows_worker": "if the worker ran on Windows, process/bg/black-box CPU come from "
                                             "GetProcessTimes (15.6 ms ticks): batch averages, error +-15.6/n ms; "
                                             "handler_cpu_ms is exact (QueryThreadCycleTime)"}},
        "endpoints": {k: {**v, "capacity_rps_at_limit": round(s["cpu_limit_cores"] * 1000 / v["process_cpu_ms"], 1)
                          if v["process_cpu_ms"] > 0 else None} for k, v in result.items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8100", help="worker base URL (default: this container)")
    ap.add_argument("--where", default="docker", help="label stored in meta.where (docker | local)")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply sample counts")
    ap.add_argument("--wait-s", type=float, default=90, help="wait this long for /healthz before starting")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "calibration.json"),
                    help="output file, or '-' for stdout")
    args = ap.parse_args()
    wait_healthy(args.url, args.wait_s)
    data = run(args.url, args.where, args.scale)
    text = json.dumps(data, indent=2) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
