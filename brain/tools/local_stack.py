"""Run the real gateway + brain locally on offset ports, without Docker. The brain starts and stops the replicas
itself as local worker processes (control.backend=process), from --replica-base upward.

    python brain/tools/local_stack.py --gateway-port 8030 --brain-port 8230 --replica-base 8130 --logs <dir>

Ctrl+C (or killing this process) stops everything it started; the brain stops its replicas on exit.
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
    ap.add_argument("--brain-port", type=int, default=8230)
    ap.add_argument("--replica-base", type=int, default=8130)
    ap.add_argument("--logs", default=str(ROOT / "brain" / ".stack-logs"))
    args = ap.parse_args()

    logs = Path(args.logs)
    logs.mkdir(parents=True, exist_ok=True)
    gw = f"http://127.0.0.1:{args.gateway_port}"
    brain = f"http://127.0.0.1:{args.brain_port}"
    procs = []

    def spawn(name, cmd, cwd, env):
        f = open(logs / f"{name}.log", "w", encoding="utf-8")
        procs.append((name, subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **env}, stdout=f, stderr=subprocess.STDOUT)))

    spawn("brain", [sys.executable, "-m", "tenantguard_brain"], ROOT / "brain",
          {"TENANTGUARD_BACKEND": "process", "TENANTGUARD_GATEWAY_URL": gw, "TENANTGUARD_BRAIN_PORT": str(args.brain_port),
           "TENANTGUARD_PROCESS_BASE_PORT": str(args.replica_base), "TENANTGUARD_REGISTRY": str(logs / "registry.json")})
    spawn("gateway", [sys.executable, "-m", "tenantguard_gateway"], ROOT / "gateway",
          {"TENANTGUARD_BRAIN_URL": brain, "TENANTGUARD_GATEWAY_PORT": str(args.gateway_port)})
    for name, url in (("brain", brain), ("gateway", gw)):
        if not wait_http(url + "/healthz", 60):
            print(f"{name} did not come up; see {logs / (name + '.log')}")
    print(f"stack up: gateway {gw}, brain {brain} (replicas from port {args.replica_base})", flush=True)
    print(f"TENANTGUARD_GATEWAY_URL={gw}\nTENANTGUARD_BRAIN_URL={brain}", flush=True)

    def stop(*_):
        for _, p in reversed(procs):
            p.terminate()
        for _, p in procs:
            try:
                p.wait(10)
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
