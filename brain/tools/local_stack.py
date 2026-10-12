"""Run the real gateway + workers (+ optionally the brain) locally on offset ports, without Docker.

    python brain/tools/local_stack.py --gateway-port 8030 --worker-base 8130 --brain-port 8230 --logs <dir>

Workers w0..w8 listen on worker-base + i (no spare / quarantine: this laptop is CPU-starved).
Ctrl+C (or killing this process) stops everything it started.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def wait_http(url: str, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-port", type=int, default=8030)
    ap.add_argument("--worker-base", type=int, default=8130)
    ap.add_argument("--brain-port", type=int, default=0, help="0 = don't start the brain")
    ap.add_argument("--workers", default="w0,w1,w2,w3,w4,w5,w6,w7,w8")
    ap.add_argument("--logs", default=str(ROOT / "brain" / ".stack-logs"))
    ap.add_argument("--data", default="", help="worker data dir (default <logs>/data)")
    args = ap.parse_args()

    logs = Path(args.logs)
    logs.mkdir(parents=True, exist_ok=True)
    data = Path(args.data or logs / "data")
    names = args.workers.split(",")
    urls = {n: f"http://127.0.0.1:{args.worker_base + i}" for i, n in enumerate(names)}
    worker_env = ",".join(f"{n}={u}" for n, u in urls.items())
    gw = f"http://127.0.0.1:{args.gateway_port}"
    procs = []

    def spawn(name, cmd, cwd, env):
        f = open(logs / f"{name}.log", "w", encoding="utf-8")
        e = {**os.environ, **env}
        procs.append((name, subprocess.Popen(cmd, cwd=cwd, env=e, stdout=f, stderr=subprocess.STDOUT)))

    for i, n in enumerate(names):
        spawn(n, [sys.executable, "-m", "app", "--port", str(args.worker_base + i)], ROOT / "worker",
              {"SERVER_NAME": n, "WORKER_DATA_DIR": str(data / n)})
    for n, u in urls.items():
        if not wait_http(u + "/healthz", 60):
            print(f"worker {n} did not come up; see {logs / (n + '.log')}")
    spawn("gateway", [sys.executable, "-m", "tenantguard_gateway"], ROOT / "gateway",
          {"TENANTGUARD_WORKER_URLS": worker_env, "TENANTGUARD_GATEWAY_PORT": str(args.gateway_port)})
    if not wait_http(gw + "/healthz", 60):
        print(f"gateway did not come up; see {logs / 'gateway.log'}")
    if args.brain_port:
        spawn("brain", [sys.executable, "-m", "tenantguard_brain"], ROOT / "brain",
              {"TENANTGUARD_WORKER_URLS": worker_env, "TENANTGUARD_GATEWAY_URL": gw,
               "TENANTGUARD_BRAIN_PORT": str(args.brain_port)})
        wait_http(f"http://127.0.0.1:{args.brain_port}/healthz", 30)
    print(f"stack up: gateway {gw}, workers {worker_env}" +
          (f", brain http://127.0.0.1:{args.brain_port}" if args.brain_port else ""), flush=True)
    print(f"TENANTGUARD_GATEWAY_URL={gw}\nTENANTGUARD_WORKER_URLS={worker_env}", flush=True)

    def stop(*_):
        for _, p in reversed(procs):
            p.terminate()
        for _, p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            for name, p in procs:
                if p.poll() is not None:
                    print(f"{name} exited with {p.returncode}; see {logs / (name + '.log')}", flush=True)
                    stop()
            time.sleep(1)
    finally:
        stop()


if __name__ == "__main__":
    main()
