"""Prometheus text exposition (hand-written; no client library needed)."""
from __future__ import annotations

from typing import TYPE_CHECKING, List

from .telemetry import LAT_BUCKETS_MS

if TYPE_CHECKING:
    from .core import Gateway


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kv: str) -> str:
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in kv.items()) + "}"


def render_metrics(gw: "Gateway") -> str:
    t = gw.telemetry
    out: List[str] = []

    def head(name: str, kind: str, help_: str) -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")

    head("tenantguard_mode", "gauge", "Current gateway mode (1 for the active one).")
    for m in ("off", "industry", "tenantguard"):
        out.append(f"tenantguard_mode{_labels(mode=m)} {int(gw.mode == m)}")

    head("tenantguard_requests_total", "counter", "Requests forwarded and completed, by tenant, server and status class.")
    for (tenant, server, code), n in sorted(t.c_requests.items()):
        out.append(f"tenantguard_requests_total{_labels(tenant=tenant, server=server, code=code)} {n}")

    head("tenantguard_admitted_total", "counter", "Requests admitted into the queue.")
    for tenant, n in sorted(t.c_admitted.items()):
        out.append(f"tenantguard_admitted_total{_labels(tenant=tenant)} {n}")

    head("tenantguard_rejected_total", "counter", "Requests refused with 429, by refusing dimension.")
    for (tenant, reason), n in sorted(t.c_rejected.items()):
        out.append(f"tenantguard_rejected_total{_labels(tenant=tenant, reason=reason)} {n}")

    head("tenantguard_expired_total", "counter", "Requests dropped with 503 after the bounded queue wait.")
    for tenant, n in sorted(t.c_expired.items()):
        out.append(f"tenantguard_expired_total{_labels(tenant=tenant)} {n}")

    head("tenantguard_cpu_ms_total", "counter", "Worker-reported handler CPU ms, by tenant.")
    for tenant, v in sorted(t.c_cpu_ms.items()):
        out.append(f"tenantguard_cpu_ms_total{_labels(tenant=tenant)} {v:.3f}")

    head("tenantguard_cpu_header_missing_total", "counter",
         "Worker responses without a usable CPU header (cost debited 0), by tenant and server.")
    for (tenant, server), n in sorted(t.c_cpu_missing.items()):
        out.append(f"tenantguard_cpu_header_missing_total{_labels(tenant=tenant, server=server)} {n}")

    head("tenantguard_request_duration_ms", "histogram", "End-to-end latency (queue + upstream), ms.")
    for tenant, h in sorted(t.h_e2e.items()):
        cum = 0
        for b, n in zip(LAT_BUCKETS_MS, h):
            cum += n
            out.append(f"tenantguard_request_duration_ms_bucket{_labels(tenant=tenant, le=str(b))} {cum}")
        cum += h[-1]
        out.append(f"tenantguard_request_duration_ms_bucket{_labels(tenant=tenant, le='+Inf')} {cum}")
        out.append(f"tenantguard_request_duration_ms_sum{_labels(tenant=tenant)} {t.h_e2e_sum[tenant]:.3f}")
        out.append(f"tenantguard_request_duration_ms_count{_labels(tenant=tenant)} {cum}")

    head("tenantguard_queue_depth", "gauge", "Requests queued at the gateway.")
    head("tenantguard_tenant_inflight", "gauge", "Requests forwarded upstream and not yet answered.")
    head("tenantguard_cost_bucket_ms", "gauge", "Current cost-bucket level (CPU ms; negative = in debt).")
    head("tenantguard_throttle_factor", "gauge", "Active LimitsCmd factor (1 = none).")
    for tenant in gw.registry.ids():
        st = gw.registry.get(tenant)
        out.append(f"tenantguard_queue_depth{_labels(tenant=tenant)} {gw.sched.depth(tenant)}")
        out.append(f"tenantguard_tenant_inflight{_labels(tenant=tenant)} {st.inflight}")
        out.append(f"tenantguard_cost_bucket_ms{_labels(tenant=tenant)} {st.cost.level_ms:.3f}")
        out.append(f"tenantguard_throttle_factor{_labels(tenant=tenant)} {gw.factors(tenant)[0]}")

    head("tenantguard_worker_window", "gauge", "Dispatch concurrency window per worker.")
    head("tenantguard_worker_inflight", "gauge", "Requests in flight per worker.")
    for ls in gw.limiter_stats():
        out.append(f"tenantguard_worker_window{_labels(server=ls.server)} {ls.window}")
        out.append(f"tenantguard_worker_inflight{_labels(server=ls.server)} {ls.inflight}")

    head("tenantguard_loop_lag_ms", "gauge", "How late the last telemetry tick woke up (event-loop saturation).")
    out.append(f"tenantguard_loop_lag_ms {gw.loop_lag_ms:.3f}")

    age = gw.overrides.heartbeat_age()
    head("tenantguard_fail_static", "gauge", "1 when no control heartbeat within fail_static_after_s.")
    out.append(f"tenantguard_fail_static {int(gw.overrides.fail_static(gw.s.fail_static_after_s))}")
    if age is not None:
        head("tenantguard_heartbeat_age_seconds", "gauge", "Seconds since the last control heartbeat.")
        out.append(f"tenantguard_heartbeat_age_seconds {age:.3f}")
    return "\n".join(out) + "\n"
