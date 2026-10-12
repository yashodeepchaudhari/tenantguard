"""Proof by relocation (DockerProver) and host-fault confirmation by drain (HostProver).

DockerProver, for a named suspect:
  1. size the spare to `spare_sizing` x the suspect's footprint (paid + unexplained CPU on its servers)
     through the Docker Engine API on the mounted socket;
  2. relocate the suspect to the spare (RelocateCmd target "spare");
  3. wait `prove_settle_s`, then sample for `prove_window_s`;
  4. R = 1 - post/pre slowdown on its original servers; confirmed when R >= recovery_R AND the harm
     followed it (the spare's CPU rose by a good share of the footprint, or its latency went slow).
It leaves the relocation in place: the verdict decides what happens next (stay on the spare as a
burst lane, class to the heavy lane, or back home). `restore_spare` puts the spare's CPU back.

Why the Engine API and not docker-py's `container.update(cpu_quota=...)`: compose's `cpus:` sets
NanoCpus, and the engine refuses a CpuQuota update on such a container; docker-py's update() cannot
set NanoCpus. `POST /containers/{id}/update {"NanoCpus": n}` (what `docker update --cpus` sends) works.

HostProver, for a host suspect: drain it (DrainCmd), wait for its traffic to leave, then compare its
idle canary latency with its peers'. Still slow while idle -> the host is at fault (it stays drained);
otherwise it was load, and it is undrained.

Both talk to the rest of the brain only through a ProveEnv (readings, send, sleep), so tests drive
them with scripted readings and a virtual clock.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from statistics import mean
from typing import Dict, List, Optional, Protocol

import httpx
from pydantic import BaseModel

from tenantguard_common.contracts import DrainCmd, RelocateCmd

from .verdicts import ProofMeasure, VerdictParams, evaluate_proof

SPARE = "spare"


@dataclass
class ProofRequest:
    incident_id: str
    tenant: str
    servers: List[str]                 # its barcode (where the slowdown is)
    confidence: float
    footprint_cpu_ms: float            # paid + unexplained CPU on its servers, ms/s
    paid_cpu_ms: float = 0.0
    cls: Optional[str] = None          # prove a single request class instead of the whole tenant


@dataclass
class ProofResult:
    tenant: str
    confirmed: bool
    recovery_R: float                  # 1 - post/pre slowdown on the original servers
    harm_followed: bool                # spare latency / CPU rose
    detail: str = ""
    readings: Dict[str, float] = field(default_factory=dict)
    incident_id: str = ""
    relocated: bool = False            # the suspect is on the spare now
    spare_cores_before: Optional[float] = None
    spare_cores_during: Optional[float] = None


@dataclass
class HostCheck:
    incident_id: str
    server: str


@dataclass
class HostResult:
    server: str
    drained: bool                      # the drain was accepted
    idle_ratio: float                  # canary level / peers' while drained
    detail: str = ""
    incident_id: str = ""


@dataclass
class Readings:
    slowdown: Dict[str, float]         # server -> max(0, peer_ratio - 1)
    peer_ratio: Dict[str, float]
    cpu_ms: Dict[str, float]           # server -> CPU ms/s
    source: Dict[str, str] = field(default_factory=dict)    # traffic | canary | none


class ProveEnv(Protocol):
    def readings(self) -> Readings: ...

    async def send(self, path: str, model: BaseModel) -> bool: ...

    async def sleep(self, seconds: float) -> None: ...


@dataclass
class ProverParams:
    spare_sizing: float = 1.25
    settle_s: float = 2.0
    window_s: float = 6.0
    sample_s: float = 0.5
    min_spare_cores: float = 0.25
    max_spare_cores: float = 4.0
    relocate_ttl_s: float = 120.0

    @classmethod
    def from_yaml(cls, brain: dict) -> "ProverParams":
        d = cls()
        v = brain.get("verdicts", {}) or {}
        return cls(spare_sizing=float(brain.get("spare_sizing", d.spare_sizing)),
                   settle_s=float(brain.get("prove_settle_s", d.settle_s)),
                   window_s=float(brain.get("prove_window_s", d.window_s)),
                   sample_s=float(v.get("prove_sample_s", d.sample_s)),
                   min_spare_cores=float(v.get("min_spare_cores", d.min_spare_cores)),
                   max_spare_cores=float(v.get("max_spare_cores", d.max_spare_cores)),
                   relocate_ttl_s=float((brain.get("ttl_s", {}) or {}).get("relocate", d.relocate_ttl_s)))


# ------------------------------------------------------------------ Docker
class DockerControl(Protocol):
    async def get_cpus(self, container: str) -> float: ...     # cores; 0 = unlimited

    async def set_cpus(self, container: str, cores: float) -> None: ...


class EngineDocker:
    """Minimal Docker Engine API client over the unix socket (no SDK needed in the image)."""

    def __init__(self, socket_path: str = "/var/run/docker.sock", api: str = "v1.41"):
        self.socket_path = socket_path
        self.client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path),
                                        base_url=f"http://docker/{api}", timeout=5.0)

    async def ping(self) -> bool:
        try:
            r = await self.client.get("/_ping")
            return r.status_code == 200
        except Exception:
            return False

    async def _host_config(self, container: str) -> dict:
        r = await self.client.get(f"/containers/{container}/json")
        r.raise_for_status()
        return r.json().get("HostConfig", {}) or {}

    async def get_cpus(self, container: str) -> float:
        hc = await self._host_config(container)
        if hc.get("NanoCpus"):
            return hc["NanoCpus"] / 1e9
        if hc.get("CpuQuota") and hc.get("CpuPeriod"):
            return hc["CpuQuota"] / hc["CpuPeriod"]
        return 0.0

    async def set_cpus(self, container: str, cores: float) -> None:
        hc = await self._host_config(container)
        if hc.get("CpuQuota") and not hc.get("NanoCpus"):
            body = {"CpuQuota": int(cores * 100_000), "CpuPeriod": 100_000}
        else:
            body = {"NanoCpus": int(cores * 1e9)}
        r = await self.client.post(f"/containers/{container}/update", json=body)
        r.raise_for_status()

    async def aclose(self) -> None:
        await self.client.aclose()


def spare_container_name() -> str:
    explicit = os.environ.get("TENANTGUARD_SPARE_CONTAINER")
    if explicit:
        return explicit
    return f"{os.environ.get('COMPOSE_PROJECT', 'codeastra')}-{SPARE}-1"


# ------------------------------------------------------------------ provers
class NullProver:
    """No Docker host: never proves, says so."""
    available = False

    async def prove(self, req: ProofRequest, env: ProveEnv) -> ProofResult:
        return ProofResult(tenant=req.tenant, confirmed=False, recovery_R=0.0, harm_followed=False,
                           detail="prover not available (needs the Docker host)", incident_id=req.incident_id)

    async def restore_spare(self, cores: Optional[float]) -> None:
        return None


class Prover(Protocol):
    available: bool

    async def prove(self, req: ProofRequest, env: ProveEnv) -> ProofResult: ...

    async def restore_spare(self, cores: Optional[float]) -> None: ...


def _slowdown(r: Readings, servers: List[str]) -> float:
    vals = [r.slowdown.get(s, 0.0) for s in servers]
    return mean(vals) if vals else 0.0


class DockerProver:
    available = True

    def __init__(self, docker: DockerControl, params: ProverParams, vparams: VerdictParams,
                 container: Optional[str] = None):
        self.docker = docker
        self.p = params
        self.v = vparams
        self.container = container or spare_container_name()
        self.lock = asyncio.Lock()          # one relocation test at a time: the spare is shared

    async def prove(self, req: ProofRequest, env: ProveEnv) -> ProofResult:
        async with self.lock:
            return await self._prove(req, env)

    async def _prove(self, req: ProofRequest, env: ProveEnv) -> ProofResult:
        res = ProofResult(tenant=req.tenant, confirmed=False, recovery_R=0.0, harm_followed=False,
                          incident_id=req.incident_id)
        pre = env.readings()
        pre_slow = _slowdown(pre, req.servers)
        spare_pre = pre.cpu_ms.get(SPARE, 0.0)
        if pre_slow <= 0:
            res.detail = "its servers are no longer slow: nothing to prove"
            return res
        want = max(self.p.min_spare_cores,
                   min(self.p.max_spare_cores, self.p.spare_sizing * req.footprint_cpu_ms / 1000.0))
        try:
            res.spare_cores_before = await self.docker.get_cpus(self.container)
            await self.docker.set_cpus(self.container, want)
            res.spare_cores_during = want
        except Exception as e:     # can't size the spare: still test, on the spare as it is
            res.detail = f"could not resize the spare ({type(e).__name__}); testing on it as is. "
        cmd = RelocateCmd(tenant=req.tenant, cls=req.cls, target=SPARE, ttl_s=self.p.relocate_ttl_s,
                          incident_id=req.incident_id)
        if not await env.send("/control/relocate", cmd):
            res.detail += "the gateway refused the relocation"
            await self.restore_spare(res.spare_cores_before)
            res.spare_cores_before = None
            return res
        res.relocated = True
        await env.sleep(self.p.settle_s)
        post, sp_cpu, sp_ratio = [], [], []
        n = max(1, int(round(self.p.window_s / self.p.sample_s)))
        for i in range(n):
            await env.sleep(self.p.sample_s)
            r = env.readings()
            post.append(_slowdown(r, req.servers))
            sp_cpu.append(r.cpu_ms.get(SPARE, 0.0))
            sp_ratio.append(r.peer_ratio.get(SPARE, 1.0))
        R, harm, confirmed, why = evaluate_proof(
            ProofMeasure(pre_slow, post, spare_pre, sp_cpu, sp_ratio, req.footprint_cpu_ms), self.v)
        res.recovery_R, res.harm_followed, res.confirmed = R, harm, confirmed
        res.detail += f"Spare sized to {want:.2f} CPU ({self.p.spare_sizing:g}x a {req.footprint_cpu_ms:.0f} ms/s " \
                      f"footprint); {why}."
        res.readings = {"pre_slowdown": round(pre_slow, 3), "post_slowdown": round(mean(post), 3),
                        "spare_cpu_pre": round(spare_pre, 1), "spare_cpu": round(mean(sp_cpu), 1),
                        "spare_ratio_max": round(max(sp_ratio), 2), "footprint_ms": round(req.footprint_cpu_ms, 1)}
        return res

    async def restore_spare(self, cores: Optional[float]) -> None:
        if cores is None:
            return
        try:
            await self.docker.set_cpus(self.container, cores if cores > 0 else self.p.max_spare_cores)
        except Exception:
            pass                            # the next proof re-sizes it anyway


class HostProver:
    """Confirm a host suspect by draining it and re-measuring it idle (no Docker needed)."""

    def __init__(self, params: ProverParams, vparams: VerdictParams):
        self.p = params
        self.v = vparams

    async def check(self, req: HostCheck, env: ProveEnv) -> HostResult:
        s = req.server
        ok = await env.send("/control/drain", DrainCmd(server=s, drained=True, ttl_s=self.p.relocate_ttl_s,
                                                       incident_id=req.incident_id))
        if not ok:
            return HostResult(server=s, drained=False, idle_ratio=0.0, incident_id=req.incident_id,
                              detail="the gateway refused the drain")
        await env.sleep(self.p.settle_s)
        ratios = []
        n = max(1, int(round(self.p.window_s / self.p.sample_s)))
        for _ in range(n):
            await env.sleep(self.p.sample_s)
            r = env.readings()
            ratios.append(r.peer_ratio.get(s, 1.0))
        idle = mean(ratios)
        return HostResult(server=s, drained=True, idle_ratio=round(idle, 3), incident_id=req.incident_id,
                          detail=f"drained {s}; idle it measured {idle:.1f}x its peers over {self.p.window_s:g} s")

    async def undrain(self, server: str, env: ProveEnv, incident_id: Optional[str] = None) -> bool:
        return await env.send("/control/drain", DrainCmd(server=server, drained=False, ttl_s=self.p.relocate_ttl_s,
                                                         incident_id=incident_id))


async def make_prover(mode: str, params: ProverParams, vparams: VerdictParams) -> Prover:
    """mode: 'docker' (must work), 'null', or 'auto' (docker when the socket answers, else null)."""
    if mode == "null":
        return NullProver()
    sock = os.environ.get("TENANTGUARD_DOCKER_SOCKET", "/var/run/docker.sock")
    if mode == "auto" and not os.path.exists(sock):
        return NullProver()
    eng = EngineDocker(sock)
    if await eng.ping():
        return DockerProver(eng, params, vparams)
    await eng.aclose()
    if mode == "docker":
        raise RuntimeError(f"TENANTGUARD_PROVER=docker but the Docker socket {sock} does not answer")
    return NullProver()
