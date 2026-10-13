"""Run the whole system locally on offset ports, without Docker: brain, gateway, the traffic lab, the handoff
bridge (with the mock Web PS3 app), the narrator and the UI.
The brain starts and stops the replicas itself as local worker processes (control.backend=process).

    python brain/tools/local_stack.py                       # brain :8230, gateway :8030, lab :8330, handoff :8430,
                                                            # mock web :8530, narrator :8730, UI :5180
    python brain/tools/local_stack.py --lab-port 0 --ui-port 0 --rate-scale 0.25 --logs <dir>
    python brain/tools/local_stack.py --stop [--logs <dir>]   # stop a stack started in the background

The lab starts calm traffic at --rate-scale x the personas.yaml rates (the full rate is sized for the Docker
host). Ctrl+C (or killing this process) stops everything it started; the brain stops its replicas on exit.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

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


def kill_tree(pid: int) -> int:
    """Kill a process and everything under it; returns how many processes were stopped."""
    try:
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        return 0
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    return len(procs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-port", type=int, default=8030)
    ap.add_argument("--brain-port", type=int, default=8230)
    ap.add_argument("--replica-base", type=int, default=8130)
    ap.add_argument("--lab-port", type=int, default=8330, help="0 = no lab")
    ap.add_argument("--handoff-port", type=int, default=8430, help="0 = no handoff bridge (and no mock web)")
    ap.add_argument("--web-port", type=int, default=8530, help="mock Web PS3 app the bridge pushes cases to")
    ap.add_argument("--narrator-port", type=int, default=8730, help="0 = no narrator")
    ap.add_argument("--ui-port", type=int, default=5180, help="0 = no UI (Vite dev server)")
    ap.add_argument("--rate-scale", type=float, default=0.25, help="calm traffic = personas.yaml rate x this")
    ap.add_argument("--logs", default=str(ROOT / "brain" / ".stack-logs"))
    ap.add_argument("--stop", action="store_true", help="stop the stack whose pid file is in --logs")
    args = ap.parse_args()

    logs = Path(args.logs)
    pidfile = logs / "stack.pid"
    if args.stop:
        n = kill_tree(int(pidfile.read_text())) if pidfile.exists() else 0
        pidfile.unlink(missing_ok=True)
        print(f"stopped {n} processes" if n else "no stack running")
        return
    logs.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))
    gw = f"http://127.0.0.1:{args.gateway_port}"
    brain = f"http://127.0.0.1:{args.brain_port}"
    lab = f"http://127.0.0.1:{args.lab_port}"
    ui = f"http://127.0.0.1:{args.ui_port}"
    handoff = f"http://127.0.0.1:{args.handoff_port}"
    web = f"http://127.0.0.1:{args.web_port}"
    token = os.environ.get("TENANTGUARD_CONTROL_TOKEN", "dev")
    cors = f"http://127.0.0.1:{args.ui_port},http://localhost:{args.ui_port}"
    procs = []

    def spawn(name, cmd, cwd, env):
        f = open(logs / f"{name}.log", "w", encoding="utf-8")
        procs.append((name, subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **env}, stdout=f, stderr=subprocess.STDOUT)))

    # the brain first: it owns the registry and starts the replicas; the gateway pulls its snapshot
    spawn("brain", [sys.executable, "-m", "tenantguard_brain"], ROOT / "brain",
          {"TENANTGUARD_BACKEND": "process", "TENANTGUARD_GATEWAY_URL": gw, "TENANTGUARD_BRAIN_PORT": str(args.brain_port),
           "TENANTGUARD_PROCESS_BASE_PORT": str(args.replica_base), "TENANTGUARD_REGISTRY": str(logs / "registry.json"),
           "TENANTGUARD_CORS_ORIGINS": cors})
    spawn("gateway", [sys.executable, "-m", "tenantguard_gateway"], ROOT / "gateway",
          {"TENANTGUARD_BRAIN_URL": brain, "TENANTGUARD_GATEWAY_PORT": str(args.gateway_port), "TENANTGUARD_CORS_ORIGINS": cors})
    for name, url in (("brain", brain), ("gateway", gw)):
        if not wait_http(url + "/healthz", 60):
            print(f"{name} did not come up; see {logs / (name + '.log')}")
    if args.lab_port:
        spawn("lab", [sys.executable, "-m", "tenantguard_lab", "serve", "--port", str(args.lab_port)], ROOT / "lab",
              {"TENANTGUARD_GATEWAY_URL": gw, "TENANTGUARD_BRAIN_URL": brain, "TENANTGUARD_LAB_URL": lab,
               "TENANTGUARD_LAB_RATE_SCALE": str(args.rate_scale), "TENANTGUARD_UI_ORIGINS": cors})
        if not wait_http(lab + "/lab/status", 60):
            print(f"lab did not come up; see {logs / 'lab.log'}")
    if args.handoff_port:
        partner = {"HANDOFF_URL": handoff, "WEB_URL": web, "HANDOFF_PARTNER_TOKEN": os.environ.get("HANDOFF_PARTNER_TOKEN", "dev-partner")}
        spawn("web", [sys.executable, "tools/mock_web.py", "serve", "--host", "127.0.0.1", "--port", str(args.web_port)],
              ROOT / "integrations" / "handoff", partner)
        spawn("handoff", [sys.executable, "-m", "tenantguard_handoff", "--host", "127.0.0.1", "--port", str(args.handoff_port)],
              ROOT / "integrations" / "handoff",
              {**partner, "TENANTGUARD_BRAIN_URL": brain, "TENANTGUARD_GATEWAY_URL": gw, "TENANTGUARD_UI_URL": ui,
               "HANDOFF_STATE_PATH": str(logs / "handoff-state.json"), "TENANTGUARD_CORS_ORIGINS": cors})
        if not wait_http(handoff + "/healthz", 60):
            print(f"handoff did not come up; see {logs / 'handoff.log'}")
    if args.narrator_port:
        spawn("narrator", [sys.executable, "-m", "tenantguard_narrator", "--host", "127.0.0.1", "--port", str(args.narrator_port)],
              ROOT / "narrator", {"TENANTGUARD_BRAIN_URL": brain, "TENANTGUARD_CORS_ORIGINS": cors})
    if args.ui_port:
        spawn("ui", [shutil.which("npx") or "npx", "vite", "--port", str(args.ui_port), "--strictPort"], ROOT / "ui",
              {"VITE_MOCK": "0", "VITE_BRAIN_URL": brain, "VITE_GATEWAY_URL": gw, "VITE_LAB_URL": lab,
               "VITE_HANDOFF_URL": handoff, "VITE_CONTROL_TOKEN": token})
        if not wait_http(ui, 90):
            print(f"UI did not come up; see {logs / 'ui.log'}")
    print(f"stack up: brain {brain}, gateway {gw} (replicas from port {args.replica_base})"
          + (f", lab {lab}" if args.lab_port else "") + (f", handoff {handoff} (mock web {web})" if args.handoff_port else "")
          + (f", narrator :{args.narrator_port}" if args.narrator_port else "") + (f", UI {ui}" if args.ui_port else ""), flush=True)

    def stop(*_):
        # on Windows terminate() is a hard kill, so the brain cannot stop its own replicas: stop every tree
        for _, p in reversed(procs):
            kill_tree(p.pid)
        pidfile.unlink(missing_ok=True)
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
