"""FastAPI app for the traffic lab (:8300)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from tenantguard_common.contracts import AttackName, AttackReq, AttackStatus, LabStatus, Mode, RunReq, RunResult

from .core import Lab, LabError
from .settings import LabConfig, estimated_fleet_cpu_ms_per_s, load_config

log = logging.getLogger("tenantguard_lab.app")


class CalmBody(BaseModel):
    rate_scale: Optional[float] = Field(None, gt=0, le=20)


def create_app(cfg: Optional[LabConfig] = None, autostart: Optional[bool] = None, transport=None) -> FastAPI:
    cfg = cfg or load_config()
    start_calm = cfg.autostart if autostart is None else autostart

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lab = Lab(cfg, transport=transport)
        app.state.lab = lab
        log.info("lab -> gateway %s, brain %s; CORS %s", cfg.gateway_url, cfg.brain_url, cfg.ui_origins)
        if start_calm:
            lab.start_calm()
        try:
            yield
        finally:
            await lab.aclose()

    app = FastAPI(title="TenantGuard traffic lab", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.ui_origins, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(LabError)
    async def _lab_error(_: Request, e: LabError) -> JSONResponse:
        return JSONResponse({"detail": e.detail}, status_code=e.status)

    def lab_of(request: Request) -> Lab:
        return request.app.state.lab

    @app.get("/healthz")
    async def healthz(request: Request) -> Dict[str, Any]:
        lab = lab_of(request)
        return {"ok": True, "gateway": cfg.gateway_url, "calm_running": lab.engine.calm_running}

    @app.get("/lab/status", response_model=LabStatus)
    async def status(request: Request) -> LabStatus:
        return lab_of(request).status()

    @app.get("/lab/personas")
    async def personas(request: Request) -> Dict[str, Any]:
        return {
            "rate_scale": cfg.rate_scale,
            "estimated_fleet_cpu_ms_per_s": round(estimated_fleet_cpu_ms_per_s(cfg), 1),
            "personas": [{"tenant": p.id, "name": p.tenant.name, "tier": p.tenant.tier, "rps": p.rps,
                          "effective_rps": round(p.rps * cfg.rate_scale, 3), "mix": p.mix,
                          "sla_p95_ms": p.sla_p95_ms, "end_users": p.end_users, "note": p.note,
                          "monthly_revenue_rs": p.tenant.monthly_revenue_rs, "protected": p.tenant.protected}
                         for p in cfg.personas.values()],
        }

    @app.post("/lab/calm/start", response_model=LabStatus)
    async def calm_start(request: Request, body: Optional[CalmBody] = None) -> LabStatus:
        if body and body.rate_scale is not None:
            cfg.rate_scale = body.rate_scale     # read live by every Poisson stream
        lab_of(request).start_calm()
        return lab_of(request).status()

    @app.post("/lab/calm/stop", response_model=LabStatus)
    async def calm_stop(request: Request) -> LabStatus:
        await lab_of(request).stop_calm()
        return lab_of(request).status()

    @app.post("/lab/attack", response_model=AttackStatus)
    async def attack(request: Request, req: AttackReq) -> AttackStatus:
        return await lab_of(request).launch(req)

    @app.delete("/lab/attack/{attack_id}", response_model=AttackStatus)
    async def stop_attack(request: Request, attack_id: str) -> AttackStatus:
        return await lab_of(request).stop_attack(attack_id)

    @app.delete("/lab/attacks", response_model=List[AttackStatus])
    async def stop_all(request: Request) -> List[AttackStatus]:
        return await lab_of(request).stop_all_attacks()

    @app.get("/lab/metrics")
    async def metrics(request: Request, window_s: float = Query(10.0, gt=0, le=900)) -> Dict[str, Any]:
        return lab_of(request).metrics(window_s)

    @app.post("/lab/run", status_code=202)
    async def run(request: Request, req: RunReq) -> Dict[str, Any]:
        return lab_of(request).start_run(req).view()

    @app.get("/lab/run/{run_id}")
    async def run_status(request: Request, run_id: str) -> Dict[str, Any]:
        info = lab_of(request).runs.get(run_id)
        if info is None:
            raise HTTPException(404, f"unknown run {run_id!r}")
        return info.view()

    @app.delete("/lab/run")
    async def cancel_run(request: Request) -> Dict[str, Any]:
        info = await lab_of(request).cancel_run()
        if info is None:
            raise HTTPException(404, "no run in progress")
        return info.view()

    @app.get("/lab/results", response_model=List[RunResult])
    async def results(request: Request, scenario: Optional[AttackName] = None,
                      mode: Optional[Mode] = None) -> List[RunResult]:
        """Measured runs (lab/results/*.json), newest first. The UI Scorecard reads this."""
        return lab_of(request).list_results(scenario, mode)

    return app


def app_factory() -> FastAPI:
    """uvicorn entry: `uvicorn tenantguard_lab.app:app_factory --factory --host 0.0.0.0 --port 8300`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
