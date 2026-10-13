"""CLI.

  python -m tenantguard_lab serve [--port 8300] [--no-calm]
  python -m tenantguard_lab calm [--seconds N] [--scale X] [--local]
  python -m tenantguard_lab attack whale K [--intensity 1] [--duration 60] [--local]
  python -m tenantguard_lab stop <attack_id|all>
  python -m tenantguard_lab status | metrics
  python -m tenantguard_lab run whale tenantguard [--tenant K] [--calm-s 20] [--attack-s 40] [--local]
  python -m tenantguard_lab calibrate [--seconds 20]

Commands talk to a running lab server (TENANTGUARD_LAB_URL, default tenantguard.yaml services.lab) when one
answers, so the dashboard sees the same attacks; otherwise (or with --local) they run in-process."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any, Dict, Optional

import httpx

from tenantguard_common.contracts import AttackReq, RunReq

from .core import Lab, LabError
from .settings import LabConfig, estimated_fleet_cpu_ms_per_s, load_config


def _table(metrics: Dict[str, Any]) -> str:
    rows = [f"{'tenant':<22}{'rps':>7}{'ok/s':>7}{'429/s':>7}{'503/s':>7}{'err/s':>7}{'p50':>8}{'p95':>8}{'p99':>8}"]
    for t in metrics["tenants"].values():
        if not t["count"]:
            continue
        rows.append(f"{t['tenant'] + ' ' + t['name'][:18]:<22}{t['count_per_s']:>7.1f}{t['ok_per_s']:>7.1f}"
                    f"{t['r429_per_s']:>7.1f}{t['r503_per_s']:>7.1f}{t['other_err_per_s']:>7.1f}"
                    f"{t['p50_ms']:>8.1f}{t['p95_ms']:>8.1f}{t['p99_ms']:>8.1f}")
    lag = metrics.get("client_loop_lag_ms")
    if lag:
        rows.append(f"  lab event-loop lag ms: p50 {lag['p50']} p99 {lag['p99']} max {lag['max']}"
                    + ("  (lab overloaded: latencies inflated)" if lag["p99"] > 20 else ""))
    for a in metrics.get("attacks", {}).values():
        rows.append(f"  attack {a['attack_id']} {a['attack']} on {a['tenant']} [{a['state']}] {a['counts']}")
    return "\n".join(rows)


def _remote(cfg: LabConfig, local: bool) -> Optional[httpx.Client]:
    if local:
        return None
    c = httpx.Client(base_url=cfg.lab_url, timeout=10.0)
    try:
        c.get("/healthz", timeout=1.0).raise_for_status()
        return c
    except httpx.HTTPError:
        c.close()
        return None


def _print_json(x: Any) -> None:
    print(json.dumps(x, indent=2))


async def _watch(lab: Lab, seconds: Optional[float], every: float = 5.0) -> None:
    end = None if seconds is None else time.monotonic() + seconds
    while end is None or time.monotonic() < end:
        await asyncio.sleep(every if end is None else max(0.0, min(every, end - time.monotonic())))
        print(time.strftime("%H:%M:%S"), f"(last {every:.0f}s, client-side ms)")
        print(_table(lab.metrics(every)), flush=True)


async def _local_calm(cfg: LabConfig, seconds: Optional[float]) -> None:
    lab = Lab(cfg)
    lab.start_calm()
    try:
        await _watch(lab, seconds)
    finally:
        await lab.aclose()


async def _local_attack(cfg: LabConfig, req: AttackReq) -> None:
    lab = Lab(cfg)
    lab.start_calm()
    try:
        st = await lab.launch(req)
        print(f"attack {st.attack_id} running locally with calm traffic for {req.duration_s:.0f}s")
        await _watch(lab, req.duration_s)
        print("attacker counts:", lab.attack_counts(st.attack_id))
    finally:
        await lab.aclose()


async def _local_run(cfg: LabConfig, req: RunReq) -> int:
    lab = Lab(cfg)
    try:
        info = lab.start_run(req)
        print(f"{info.run_id}: {req.scenario} in {req.mode} mode, ~{info.planned_end - info.started_at:.0f}s")
        await lab.wait_run()
        if info.result is None:
            print("run failed:", info.error)
            return 1
        _print_json(info.result.model_dump())
        return 0
    finally:
        await lab.aclose()


async def _calibrate(cfg: LabConfig, seconds: float) -> int:
    """Run calm traffic and read the pool's measured CPU from the brain (GET /api/pool) once a second."""
    async with httpx.AsyncClient(timeout=3.0) as http:
        async def pool() -> Optional[dict]:
            try:
                return (await http.get(f"{cfg.brain_url}/api/pool")).json()
            except (httpx.HTTPError, ValueError):
                return None

        lab = Lab(cfg)
        lab.start_calm()
        samples = []
        try:
            await asyncio.sleep(3)          # warm up pools and caches
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                p = await pool()
                if p:
                    samples.append((sum(x["used_cpu"] for x in p["projects"]) * 1000.0, p["usable_cpu"] * 1000.0))
                await asyncio.sleep(1.0)
            print(_table(lab.metrics(seconds)))
        finally:
            await lab.aclose()
    est = estimated_fleet_cpu_ms_per_s(cfg)
    if not samples:
        print(f"estimate from est_cpu_ms: {est:.0f} CPU-ms/s; brain {cfg.brain_url}/api/pool not reachable")
        return 1
    used = sum(u for u, _ in samples) / len(samples)
    cap = samples[-1][1]
    util = used / cap if cap > 0 else 0.0
    print(f"estimate from est_cpu_ms: {est:.0f} CPU-ms/s = {est / cap:.1%} of the usable pool {cap:.0f}")
    print(f"measured (pool, {len(samples)} samples): {used:.0f} CPU-ms/s = {util:.1%} of {cap:.0f}")
    if util > 0:
        print(f"suggested rate_scale for {cfg.target_fleet_util:.0%}: "
              f"{cfg.rate_scale * cfg.target_fleet_util / util:.2f} (current {cfg.rate_scale})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tenantguard_lab", description="TenantGuard traffic lab")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the lab API on :8300")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8300)
    s.add_argument("--no-calm", action="store_true", help="don't start calm traffic on boot")

    s = sub.add_parser("calm", help="start calm traffic for all 12 tenants")
    s.add_argument("--seconds", type=float, default=None, help="local mode: stop after N seconds")
    s.add_argument("--scale", type=float, default=None, help="rate_scale override")
    s.add_argument("--local", action="store_true")

    s = sub.add_parser("attack", help="launch an attack")
    s.add_argument("attack")
    s.add_argument("tenant")
    s.add_argument("--intensity", type=float, default=1.0)
    s.add_argument("--duration", type=float, default=60.0)
    s.add_argument("--local", action="store_true")

    s = sub.add_parser("stop", help="stop an attack (id) or all attacks")
    s.add_argument("attack_id")

    sub.add_parser("status")
    s = sub.add_parser("metrics")
    s.add_argument("--window", type=float, default=10.0)

    s = sub.add_parser("run", help="scripted scenario: calm -> attack -> calm, writes lab/results/*.json")
    s.add_argument("scenario")
    s.add_argument("mode")
    s.add_argument("--tenant", default=None)
    s.add_argument("--intensity", type=float, default=1.0)
    s.add_argument("--calm-s", type=float, default=20.0)
    s.add_argument("--attack-s", type=float, default=40.0)
    s.add_argument("--local", action="store_true")

    s = sub.add_parser("calibrate", help="measure the calm pool CPU via the brain (GET /api/pool)")
    s.add_argument("--seconds", type=float, default=20.0)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.cmd == "serve" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    if a.cmd == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(cfg, autostart=False if a.no_calm else None), host=a.host, port=a.port, access_log=False)
        return 0

    if a.cmd == "calibrate":
        return asyncio.run(_calibrate(cfg, a.seconds))

    try:
        if a.cmd == "calm":
            if a.scale is not None:
                cfg.rate_scale = a.scale
            remote = _remote(cfg, a.local)
            if remote:
                body = {"rate_scale": a.scale} if a.scale is not None else None
                _print_json(remote.post("/lab/calm/start", json=body).json())
                return 0
            print(f"no lab server at {cfg.lab_url}; running calm traffic locally against {cfg.gateway_url} (Ctrl-C stops)")
            asyncio.run(_local_calm(cfg, a.seconds))
            return 0

        if a.cmd == "attack":
            req = AttackReq(attack=a.attack, tenant=a.tenant, intensity=a.intensity, duration_s=a.duration)
            remote = _remote(cfg, a.local)
            if remote:
                r = remote.post("/lab/attack", json=req.model_dump())
                _print_json(r.json())
                return 0 if r.status_code < 300 else 1
            asyncio.run(_local_attack(cfg, req))
            return 0

        if a.cmd == "run":
            req = RunReq(scenario=a.scenario, mode=a.mode, tenant=a.tenant, intensity=a.intensity,
                         calm_s=a.calm_s, attack_s=a.attack_s)
            remote = _remote(cfg, a.local)
            if remote is None:
                return asyncio.run(_local_run(cfg, req))
            r = remote.post("/lab/run", json=req.model_dump())
            if r.status_code >= 300:
                _print_json(r.json())
                return 1
            info = r.json()
            print(f"{info['run_id']}: running on the lab server, ~{info['planned_end'] - info['started_at']:.0f}s")
            while info["state"] == "running":
                time.sleep(2)
                info = remote.get(f"/lab/run/{info['run_id']}").json()
                print(f"  phase {info['phase']}", flush=True)
            _print_json(info["result"] or info)
            return 0 if info["state"] == "done" else 1

        remote = _remote(cfg, False)
        if remote is None:
            print(f"no lab server at {cfg.lab_url}")
            return 1
        if a.cmd == "stop":
            r = remote.delete("/lab/attacks" if a.attack_id == "all" else f"/lab/attack/{a.attack_id}")
            _print_json(r.json())
        elif a.cmd == "status":
            _print_json(remote.get("/lab/status").json())
        elif a.cmd == "metrics":
            print(_table(remote.get("/lab/metrics", params={"window_s": a.window}).json()))
        return 0
    except LabError as e:
        print(f"error {e.status}: {e.detail}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
