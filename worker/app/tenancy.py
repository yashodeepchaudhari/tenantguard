"""Tenant context. The tenant comes only from X-TenantGuard-Tenant (set by the gateway).

`tenant_var` is the single source of the tenant for the repository, cache, memory budget
and background jobs. `request_var` holds the per-request meter whose cpu_ms ends up in
the X-TenantGuard-Cpu-Ms response header.
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass
from typing import Optional

TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
NO_CLASS = "none"

tenant_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tenantguard_tenant", default=None)


@dataclass
class RequestMeter:
    tenant: Optional[str]
    cls: str = NO_CLASS
    cpu_ms: float = 0.0
    bytes_in: int = 0


request_var: contextvars.ContextVar[Optional[RequestMeter]] = contextvars.ContextVar("tenantguard_request", default=None)


class NoTenantError(RuntimeError):
    """Tenant-scoped code ran outside a tenant context."""


def current_tenant() -> str:
    t = tenant_var.get()
    if t is None:
        raise NoTenantError("no tenant in context")
    return t


def valid_tenant(t: Optional[str]) -> bool:
    return bool(t) and TENANT_RE.match(t) is not None
