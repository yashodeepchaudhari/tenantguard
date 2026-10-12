"""Billing settings: tenantguard.yaml `billing:`, layout (tiers, barcodes, sla_p95_ms), service URLs."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tenantguard_common.config import load_settings, load_tenants, worker_urls
from tenantguard_common.contracts import TenantConfig

DEFAULTS: Dict[str, Any] = {
    "poll_s": 5,
    "bucket_s": 60,
    "retention_h": 24,
    "price_rs_per_cpu_s": {"enterprise": 0.40, "pro": 0.60, "free": 0.80},
    "db_price_rs_per_s": 0.0,
    "ref_alpha": 0.2,
    "sla_default_p95_ms": 100,
    "sla_credit_table": [{"min_violation_s": 2, "pct": 2}, {"min_violation_s": 10, "pct": 4},
                         {"min_violation_s": 60, "pct": 10}, {"min_violation_s": 300, "pct": 25}],
    "unmetered_min_confidence": 0.6,
}


@dataclass
class BillingSettings:
    poll_s: float
    bucket_s: float
    retention_s: float
    price_rs_per_cpu_s: Dict[str, float]
    db_price_rs_per_s: float
    ref_alpha: float
    sla_default_p95_ms: float
    sla_credit_table: List[Dict[str, float]]
    unmetered_min_confidence: float
    tenants: Dict[str, TenantConfig] = field(default_factory=dict)
    sla_p95_ms: Dict[str, float] = field(default_factory=dict)
    worker_urls: Dict[str, str] = field(default_factory=dict)
    brain_url: str = "http://127.0.0.1:8200"
    gateway_url: str = "http://127.0.0.1:8000"
    control_token: str = "dev"
    cors_origins: List[str] = field(default_factory=lambda: ["http://127.0.0.1:5173"])

    def sla(self, tenant: str) -> float:
        return self.sla_p95_ms.get(tenant, self.sla_default_p95_ms)

    def barcode(self, tenant: str) -> List[str]:
        t = self.tenants.get(tenant)
        return list(t.servers) if t else []

    def rate_rs_per_ms(self, tenant: str) -> float:
        t = self.tenants.get(tenant)
        tier = t.tier if t else "free"
        return self.price_rs_per_cpu_s.get(tier, max(self.price_rs_per_cpu_s.values())) / 1000.0


def load(settings: Optional[Dict[str, Any]] = None, **overrides: Any) -> BillingSettings:
    settings = settings or load_settings()
    b = {**DEFAULTS, **(settings.get("billing") or {})}
    tenants = {t.id: t for t in load_tenants(settings)}
    sla = {t.id: float(t.sla_p95_ms) for t in tenants.values()}  # layout; unknown ids use the default
    services = settings.get("services") or {}
    origins = (os.environ.get("TENANTGUARD_CORS_ORIGINS") or os.environ.get("TENANTGUARD_UI_ORIGINS")
               or "http://127.0.0.1:5173")
    cfg = BillingSettings(
        poll_s=float(b["poll_s"]), bucket_s=float(b["bucket_s"]), retention_s=float(b["retention_h"]) * 3600,
        price_rs_per_cpu_s={k: float(v) for k, v in b["price_rs_per_cpu_s"].items()},
        db_price_rs_per_s=float(b["db_price_rs_per_s"]), ref_alpha=float(b["ref_alpha"]),
        sla_default_p95_ms=float(b["sla_default_p95_ms"]),
        sla_credit_table=sorted(b["sla_credit_table"], key=lambda r: r["min_violation_s"]),
        unmetered_min_confidence=float(b["unmetered_min_confidence"]),
        tenants=tenants, sla_p95_ms=sla, worker_urls=worker_urls(settings),
        brain_url=os.environ.get("TENANTGUARD_BRAIN_URL", services.get("brain", "http://127.0.0.1:8200")),
        gateway_url=os.environ.get("TENANTGUARD_GATEWAY_URL", services.get("gateway", "http://127.0.0.1:8000")),
        control_token=os.environ.get("TENANTGUARD_CONTROL_TOKEN", "dev"),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg
