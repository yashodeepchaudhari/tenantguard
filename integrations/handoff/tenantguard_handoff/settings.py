"""Bridge settings: tenantguard.yaml (handoff, brain.ttl_s, gateway.ttl_max_s, services) + env for deployment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict
from urllib.parse import urlparse


@dataclass
class HandoffSettings:
    web_url: str = "http://127.0.0.1:8500"
    brain_url: str = "http://127.0.0.1:8200"
    gateway_url: str = "http://127.0.0.1:8000"
    ui_url: str = "http://127.0.0.1:5173"
    control_token: str = "dev"
    state_path: str = "data/handoff-state.json"
    port: int = 8400
    decision_window_s: float = 120.0
    block_s: float = 300.0
    extend_s: float = 120.0
    push_timeout_s: float = 3.0
    ttl_max_s: float = 3600.0
    ttl_throttle_s: float = 60.0
    ttl_quarantine_s: float = 120.0
    confidence_to_name: float = 0.6
    retention_s: float = 3600.0          # closed/resolved cases are evicted this long after they close
    history_wire_max: int = 50           # history items sent to the partner per case (+ "N earlier events")
    history_keep: int = 500              # history items kept per case in memory
    id_map_max: int = 2000               # incident_id -> case_id entries kept (and persisted)
    cors_origins: list = field(default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"])

    @classmethod
    def from_dict(cls, s: Dict[str, Any]) -> "HandoffSettings":
        h = s.get("handoff", {}) or {}
        svc = s.get("services", {}) or {}
        ttl = ((s.get("brain") or {}).get("ttl_s") or {})
        env = os.environ.get
        cors = env("TENANTGUARD_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
        return cls(
            web_url=env("WEB_URL", "http://127.0.0.1:8500"),
            brain_url=env("TENANTGUARD_BRAIN_URL", svc.get("brain", "http://127.0.0.1:8200")),
            gateway_url=env("TENANTGUARD_GATEWAY_URL", svc.get("gateway", "http://127.0.0.1:8000")),
            ui_url=env("TENANTGUARD_UI_URL", "http://127.0.0.1:5173"),
            control_token=env("TENANTGUARD_CONTROL_TOKEN", "dev"),
            state_path=env("HANDOFF_STATE_PATH", "data/handoff-state.json"),
            port=urlparse(svc.get("handoff", "http://127.0.0.1:8400")).port or 8400,
            decision_window_s=float(h.get("decision_window_s", 120)),
            block_s=float(h.get("block_s", 300)),
            extend_s=float(h.get("extend_s", 120)),
            push_timeout_s=float(h.get("push_timeout_s", 3)),
            ttl_max_s=float((s.get("gateway") or {}).get("ttl_max_s", 3600)),
            ttl_throttle_s=float(ttl.get("throttle", 60)),
            ttl_quarantine_s=float(ttl.get("quarantine", 120)),
            confidence_to_name=float((s.get("brain") or {}).get("confidence_to_prove", 0.6)),
            retention_s=float(h.get("retention_s", 3600)),
            history_wire_max=int(h.get("history_wire_max", 50)),
            history_keep=int(h.get("history_keep", 500)),
            id_map_max=int(h.get("id_map_max", 2000)),
            cors_origins=[o.strip() for o in cors.split(",") if o.strip()],
        )
