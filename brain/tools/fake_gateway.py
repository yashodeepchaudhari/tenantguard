"""Fake gateway + fake workers for developing the brain without T1/T2.

Streams TelemetryFrames on WS /internal/telemetry from a scripted scenario
(calm -> K whale -> calm), serves WorkerStats for every server under /fake/workers/{server}/stats,
and accepts /control/* exactly like the real gateway (LimitsCmd is applied to the simulation,
so throttling really changes the next frames).

Run (from brain/):
    python tools/fake_gateway.py --port 8000
    TENANTGUARD_WORKER_URLS="w0=http://127.0.0.1:8000/fake/workers/w0,..." python -m tenantguard_brain
`--print-worker-urls` prints that env value.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from tenantguard_common.config import load_settings, load_tenants  # noqa: E402
from tenantguard_common.contracts import (BlockCmd, ControlAck, DrainCmd, Heartbeat, LimitsCmd,  # noqa: E402
                                          ModeBody, OverridesView, RelocateCmd, RouteWeightsCmd,
                                          TelemetryFrame)

from tenantguard_brain.sim import Simulator, whale_scenario  # noqa: E402

log = logging.getLogger("fake_gateway")


def build_app(args) -> FastAPI:
    raw = load_settings()
    tenants = load_tenants(raw)
    tick_ms = int(raw["gateway"]["tick_ms"])
    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(ticker())
        yield
        task.cancel()

    app = FastAPI(title="fake gateway", lifespan=lifespan)
    st = {"sim": None, "started": 0.0, "latest": None, "hb": None, "run": 0}
    clients: Set[asyncio.Queue] = set()
    limits: Dict[str, LimitsCmd] = {}
    blocks: Dict[str, BlockCmd] = {}
    relocations: List[RelocateCmd] = []
    drains: Dict[str, DrainCmd] = {}
    route_weights: Dict[str, Dict[str, float]] = {}
    expires: Dict[str, float] = {}
    control_log: List[dict] = []

    def new_sim(mode: str) -> Simulator:
        st["started"] = time.time()
        st["run"] += 1
        phases = whale_scenario(args.attacker, args.calm, args.attack, args.after, args.rps, args.cost)
        log.info("scenario #%d: calm %ss -> %s whale %ss (%.0f rps x %.0f ms) -> calm %ss, mode=%s",
                 st["run"], args.calm, args.attacker, args.attack, args.rps, args.cost, args.after, mode)
        return Simulator(tenants, phases, tick_ms=tick_ms, server_cores=float(raw["worker"]["cpus"]),
                         quota_mult={args.attacker: args.quota_mult}, start=time.time(), mode=mode,
                         seed=st["run"])

    st["sim"] = new_sim(args.mode)

    async def ticker():
        while True:
            sim: Simulator = st["sim"]
            if sim.phase_at(sim.now + tick_ms / 1000.0) is None:
                if args.loop:
                    st["sim"] = sim = new_sim(sim.mode)
            t0 = time.time()
            sim.now = t0 - tick_ms / 1000.0          # keep the sim clock on wall time
            frame = sim.step()
            st["latest"] = frame
            text = frame.model_dump_json()
            for q in list(clients):
                if q.full():
                    q.get_nowait()
                q.put_nowait(text)
            await asyncio.sleep(max(0.01, tick_ms / 1000.0 - (time.time() - t0)))

    @app.websocket("/internal/telemetry")
    async def telemetry(ws: WebSocket):
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        clients.add(q)
        log.info("telemetry client connected (%d)", len(clients))
        try:
            while True:
                await ws.send_text(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.discard(q)

    @app.get("/internal/telemetry/latest", response_model=Optional[TelemetryFrame])
    def latest():
        return st["latest"]

    @app.get("/fake/workers/{server}/stats")
    def stats(server: str):
        sim: Simulator = st["sim"]
        if server not in sim.workers:
            raise HTTPException(404, "unknown server")
        return sim.worker_stats(server)

    @app.get("/fake/scenario")
    def scenario():
        sim: Simulator = st["sim"]
        p = sim.phase_at(sim.now)
        return {"run": st["run"], "elapsed_s": round(sim.now - sim.t0, 2), "phase": p.name if p else "done",
                "total_s": sim.total_s, "mode": sim.mode}

    @app.post("/fake/scenario/restart")
    def restart():
        st["sim"] = new_sim(st["sim"].mode)
        return scenario()

    @app.get("/fake/control_log")
    def get_control_log(limit: int = 200):
        return control_log[-limit:]

    @app.get("/admin/mode")
    def get_mode():
        return {"mode": st["sim"].mode}

    @app.post("/admin/mode")
    def set_mode(body: ModeBody):
        st["sim"].mode = body.mode
        log.info("mode -> %s", body.mode)
        return {"mode": body.mode}

    @app.get("/admin/overrides", response_model=OverridesView)
    def overrides():
        now = time.time()
        for k in [k for k, v in expires.items() if v <= now]:
            kind, key = k.split(":", 1)
            expires.pop(k)
            {"limits": limits, "blocks": blocks, "drains": drains}.get(kind, {}).pop(key, None)
        hb = st["hb"]
        age = now - hb if hb else None
        return OverridesView(limits=limits, blocks=blocks, relocations=relocations, route_weights=route_weights,
                             drains=drains, expires_at=expires, last_heartbeat_age_s=age,
                             fail_static=age is not None and age > float(raw["gateway"]["fail_static_after_s"]))

    async def parse(request: Request, model):
        try:
            return model.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            raise HTTPException(400, f"invalid {model.__name__}: {e}")

    @app.post("/control/heartbeat", response_model=ControlAck)
    async def heartbeat(request: Request):
        await parse(request, Heartbeat)
        st["hb"] = time.time()
        return ControlAck(ok=True)

    @app.post("/control/limits", response_model=ControlAck)
    async def control_limits(request: Request):
        cmd: LimitsCmd = await parse(request, LimitsCmd)
        if not (0 < cmd.factor <= 1) or not (0 < cmd.weight_factor <= 1) or cmd.ttl_s <= 0:
            raise HTTPException(400, "factor and weight_factor must be in (0, 1], ttl_s > 0")
        if cmd.tenant not in {t.id for t in tenants}:
            raise HTTPException(400, f"unknown tenant {cmd.tenant}")
        sim: Simulator = st["sim"]
        sim.apply_limits(cmd.tenant, cmd.factor, cmd.weight_factor, cmd.ttl_s)
        if cmd.factor >= 1 and cmd.weight_factor >= 1:
            limits.pop(cmd.tenant, None)
            expires.pop(f"limits:{cmd.tenant}", None)
        else:
            limits[cmd.tenant] = cmd
            expires[f"limits:{cmd.tenant}"] = time.time() + cmd.ttl_s
        control_log.append({"ts": time.time(), "kind": "limits", **cmd.model_dump()})
        log.info("CONTROL limits %s factor=%.2f weight_factor=%.2f ttl=%gs incident=%s",
                 cmd.tenant, cmd.factor, cmd.weight_factor, cmd.ttl_s, cmd.incident_id)
        return ControlAck(ok=True, detail="applied")

    @app.post("/control/block", response_model=ControlAck)
    async def control_block(request: Request):
        cmd: BlockCmd = await parse(request, BlockCmd)
        if cmd.blocked:
            blocks[cmd.tenant] = cmd
            expires[f"blocks:{cmd.tenant}"] = time.time() + cmd.ttl_s
        else:
            blocks.pop(cmd.tenant, None)
        control_log.append({"ts": time.time(), "kind": "block", **cmd.model_dump()})
        return ControlAck(ok=True, detail="stored (not simulated)")

    @app.post("/control/route_weights", response_model=ControlAck)
    async def control_rw(request: Request):
        cmd: RouteWeightsCmd = await parse(request, RouteWeightsCmd)
        route_weights.update(cmd.weights)
        control_log.append({"ts": time.time(), "kind": "route_weights", **cmd.model_dump()})
        return ControlAck(ok=True, detail="stored (not simulated)")

    @app.post("/control/relocate", response_model=ControlAck)
    async def control_relocate(request: Request):
        cmd: RelocateCmd = await parse(request, RelocateCmd)
        relocations.append(cmd)
        control_log.append({"ts": time.time(), "kind": "relocate", **cmd.model_dump()})
        return ControlAck(ok=True, detail="stored (not simulated)")

    @app.post("/control/drain", response_model=ControlAck)
    async def control_drain(request: Request):
        cmd: DrainCmd = await parse(request, DrainCmd)
        drains[cmd.server] = cmd
        control_log.append({"ts": time.time(), "kind": "drain", **cmd.model_dump()})
        return ControlAck(ok=True, detail="stored (not simulated)")

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--attacker", default="K")
    ap.add_argument("--calm", type=float, default=20)
    ap.add_argument("--attack", type=float, default=40)
    ap.add_argument("--after", type=float, default=60)
    ap.add_argument("--rps", type=float, default=10.0, help="whale requests/s")
    ap.add_argument("--cost", type=float, default=90.0, help="CPU ms per whale request")
    ap.add_argument("--quota-mult", type=float, default=3.0,
                    help="attacker's quota multiplier (an over-provisioned plan the gateway alone won't stop)")
    ap.add_argument("--mode", default="tenantguard", choices=["off", "industry", "tenantguard"])
    ap.add_argument("--loop", action="store_true", help="restart the scenario when it ends")
    ap.add_argument("--print-worker-urls", action="store_true")
    args = ap.parse_args()
    if args.print_worker_urls:
        raw = load_settings()
        base = f"http://127.0.0.1:{args.port}/fake/workers"
        print(",".join(f"{s}={base}/{s}" for s in sorted({s for t in load_tenants(raw) for s in t.servers})))
        return
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    uvicorn.run(build_app(args), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
