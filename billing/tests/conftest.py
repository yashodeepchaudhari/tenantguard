from __future__ import annotations

import os
from pathlib import Path

# tenantguard_common fixes ROOT at import: point it at this checkout (layout with sla_p95_ms) first.
os.environ.setdefault("TENANTGUARD_ROOT", str(Path(__file__).resolve().parents[2]))

from typing import Dict, List, Optional  # noqa: E402

import pytest  # noqa: E402

from tenantguard_billing.main import Billing  # noqa: E402
from tenantguard_billing.settings import load  # noqa: E402
from tenantguard_common.contracts import (ClassCost, IncidentEvent, ServerView, StateSnapshot, Suspect,  # noqa: E402
                                          TenantUsage, TenantView, WorkerStats)

T0 = 1_800_000_000.0  # a multiple of 60: bucket boundaries are T0 + k*60


def stats(server: str, ts: float, started_at: float, tenants: Dict[str, dict],
          classes: Optional[Dict[tuple, tuple]] = None, blackbox: float = 0.0) -> WorkerStats:
    return WorkerStats(
        server=server, ts=ts, started_at=started_at, cpu_total_ms=0.0, blackbox_cpu_ms=blackbox, mem_mb=50.0,
        tenants=[TenantUsage(tenant=t, requests=v.get("requests", 0), handler_cpu_ms=v.get("cpu", 0.0),
                             bg_cpu_ms=v.get("bg", 0.0), db_ms=v.get("db", 0.0)) for t, v in tenants.items()],
        classes=[ClassCost(tenant=t, cls=c, count=n, cpu_ms_total=ms) for (t, c), (n, ms) in (classes or {}).items()])


def ev(iid: str, stage: str, ts: float, tenant: Optional[str] = None, level: Optional[str] = None,
       report: Optional[dict] = None, suspects: Optional[List[Suspect]] = None, detail: str = "") -> IncidentEvent:
    return IncidentEvent(incident_id=iid, stage=stage, ts=ts, tenant=tenant, level=level, report=report,
                         suspects=suspects or [], detail=detail)


def snap(ts: float, p95: Dict[str, float], slow: List[str] = ()) -> StateSnapshot:
    return StateSnapshot(
        ts=ts, mode="tenantguard", utilization=0.3, fairness=0.9, status_sentence="",
        tenants=[TenantView(tenant=t, name=t, tier="pro", p95_ms=v, rps=1, rejected_rps=0, cpu_share=0.1,
                            noisy_score=0.5, noisy_norm=0.1, level="normal") for t, v in p95.items()],
        servers=[ServerView(server=s, p95_ms=5, peer_ratio=1.0, cpu_pct=30,
                            status="slow" if s in slow else "healthy") for s in [f"w{i}" for i in range(9)]])


@pytest.fixture
def cfg():
    return load()


@pytest.fixture
def billing(cfg):
    return Billing(cfg, started_at=T0 - 1)


SRV = "srv-1800000060-7"
NAMED_DETAIL = "Named KaamKaro (K) with 85% confidence: 2 of its 3 servers are slower than their peers (w1 3.1x)."


def scenario(b: Billing) -> None:
    """Calm minute [T0, T0+60), incident minute [T0+60, T0+120): K (w1,w3,w8) runs a black-box bomb, w1 is slow,
    A (w0,w1,w2) suffers on w1, the brain rescues A at +1.5 s and names K. Calm minute again after."""
    up = T0 - 10
    # worker w1 hosts A and K. Calm: A 100 light requests at 1 ms; K 10 searches at 20 ms; black box 1 ms/s.
    b.on_stats(stats("w1", T0 - 5, up, {"A": {}, "K": {}}, {}, blackbox=0))
    b.on_stats(stats("w1", T0 + 55, up, {"A": {"requests": 100, "cpu": 100.0, "db": 5},
                                         "K": {"requests": 10, "cpu": 200.0}},
                     {("A", "c_light"): (100, 100.0), ("K", "c_search"): (10, 200.0)}, blackbox=60.0))
    # incident minute: A's light requests cost 3x (contention); K ingests -> black box +20000 ms
    b.on_stats(stats("w1", T0 + 115, up, {"A": {"requests": 200, "cpu": 400.0, "db": 10},
                                          "K": {"requests": 20, "cpu": 400.0, "bg": 0}},
                     {("A", "c_light"): (200, 400.0), ("K", "c_search"): (20, 400.0)}, blackbox=20_120.0))
    # calm again
    b.on_stats(stats("w1", T0 + 175, up, {"A": {"requests": 300, "cpu": 500.0, "db": 15},
                                          "K": {"requests": 30, "cpu": 600.0}},
                     {("A", "c_light"): (300, 500.0), ("K", "c_search"): (30, 600.0)}, blackbox=20_180.0))
    b.on_events([
        ev(SRV, "detected", T0 + 60, detail="Slow servers: w1 3.1x its peers."),
        ev(SRV, "rescued", T0 + 61.5, detail="Moved 3 tenants off w1: A -> w0 50%, w2 50%; E -> ...; H -> ..."),
        ev(SRV, "named", T0 + 64, tenant="K", detail=NAMED_DETAIL, suspects=[Suspect(tenant="K", confidence=0.85)],
           report={"culprits": [{"tenant": "K", "confidence": 0.85, "covers": ["w1", "w3"]}], "host_suspects": []}),
        ev(SRV, "closed", T0 + 110, detail="Server incident closed after 50 s. Named: K."),
    ])
    # 2 Hz snapshots: A's p95 (SLA 50 ms) is 120 ms from +60 to +70 while w1 is slow, else 20 ms
    t = T0
    while t < T0 + 180:
        bad = T0 + 60 <= t < T0 + 70
        b.on_snapshot(snap(t, {"A": 120.0 if bad else 20.0, "K": 30.0, "E": 25.0}, slow=["w1"] if bad else []))
        t += 0.5
    b.tick(T0 + 1000)
