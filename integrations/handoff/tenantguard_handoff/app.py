"""FastAPI wiring for the handoff bridge (:8400)."""
from __future__ import annotations

import contextlib
import hmac
import os
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from tenantguard_common.config import load_settings, load_tenants
from tenantguard_common.contracts import CaseView

from .bridge import Bridge
from .cases import DecisionError
from .settings import HandoffSettings
from .wire import PARTNER_TOKEN_HEADER



def create_app(bridge: Optional[Bridge] = None, start: bool = True) -> FastAPI:
    if bridge is None:
        raw = load_settings()
        bridge = Bridge(HandoffSettings.from_dict(raw), load_tenants(raw))
    partner_token = os.environ.get("HANDOFF_PARTNER_TOKEN", "")

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        if start:
            bridge.start()
        try:
            yield
        finally:
            await bridge.stop()

    app = FastAPI(title="TenantGuard handoff bridge", lifespan=lifespan)
    app.state.bridge = bridge
    app.add_middleware(CORSMiddleware, allow_origins=bridge.s.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @app.get("/healthz")
    async def healthz():
        return bridge.health()

    @app.get("/cases", response_model=List[CaseView])
    async def list_cases():
        return sorted((c.view for c in bridge.book.cases.values()), key=lambda v: v.created_at, reverse=True)

    @app.get("/cases/{case_id}", response_model=CaseView)
    async def get_case(case_id: str):
        v = bridge.book.get(case_id)
        if v is None:
            return JSONResponse({"detail": f"unknown case {case_id}"}, status_code=404)
        return v

    @app.get("/cases/{case_id}/wire")
    async def get_case_wire(case_id: str):
        if bridge.book.get(case_id) is None:
            return JSONResponse({"detail": f"unknown case {case_id}"}, status_code=404)
        return bridge.book.to_wire(case_id).model_dump()

    async def _update(request: Request):
        # Fail closed: an update can make the bridge send control commands with ITS control token.
        if not partner_token:
            return JSONResponse({"ok": False, "detail": "partner token not configured (HANDOFF_PARTNER_TOKEN)"},
                                status_code=503)
        got = request.headers.get(PARTNER_TOKEN_HEADER, "").encode("utf-8")
        if not hmac.compare_digest(got, partner_token.encode("utf-8")):
            return JSONResponse({"ok": False, "detail": f"missing or wrong {PARTNER_TOKEN_HEADER}"}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "detail": "body must be JSON"}, status_code=400)
        try:
            return await bridge.case_update(body)
        except ValidationError as e:
            return JSONResponse({"ok": False, "detail": e.errors(include_url=False, include_context=False)},
                                status_code=400)
        except ValueError as e:
            return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
        except DecisionError as e:
            return JSONResponse({"ok": False, "detail": e.detail}, status_code=e.status)

    # The partner contract names POST {TENANTGUARD_URL}/control/case-update; /cases/update is an alias.
    app.add_api_route("/control/case-update", _update, methods=["POST"])
    app.add_api_route("/cases/update", _update, methods=["POST"])
    return app


app = create_app()
