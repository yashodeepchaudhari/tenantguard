"""Relocation prover interface (implementation comes with the Docker host, next task).

The prover takes a named suspect, sizes the spare (`spare_sizing` x its footprint, via docker update),
relocates it there (RelocateCmd), waits `prove_settle_s`, measures for `prove_window_s`, and computes
R = 1 - post_slowdown / pre_slowdown on its original servers. It confirms when R >= `recovery_R` AND
the harm followed it to the spare.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol


@dataclass
class ProofRequest:
    incident_id: str
    tenant: str
    servers: List[str]                 # its barcode (where the slowdown is)
    confidence: float
    footprint_cpu_ms: float            # paid + unexplained CPU on its servers, ms/s
    cls: Optional[str] = None          # prove a single request class instead of the whole tenant


@dataclass
class ProofResult:
    tenant: str
    confirmed: bool
    recovery_R: float                  # 1 - post/pre slowdown on the original servers
    harm_followed: bool                # spare latency / unexplained CPU rose
    detail: str = ""
    readings: Dict[str, float] = field(default_factory=dict)


class Prover(Protocol):
    available: bool

    async def prove(self, req: ProofRequest) -> ProofResult: ...


class NullProver:
    """No Docker host: never proves, says so."""
    available = False

    async def prove(self, req: ProofRequest) -> ProofResult:
        return ProofResult(tenant=req.tenant, confirmed=False, recovery_R=0.0, harm_followed=False,
                           detail="prover not available (needs the Docker host)")
