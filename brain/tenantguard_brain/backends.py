"""Where replicas run: the only code that starts, stops or resizes them (I/O adapters, one interface).

- DockerBackend:  Docker Engine API over the unix socket (no SDK, no docker CLI). Every container it creates
                  carries the label tg.stack=<stack>, and it only ever lists or touches containers with that
                  label, so other stacks on the same host are never affected.
- ProcessBackend: local runs without Docker: each replica is a worker process on 127.0.0.1. CPU settings are
                  recorded, not enforced (no cgroups).

`apply` never raises: each action returns an ActionResult and failures are retried by the next control tick.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol

import httpx
import psutil

from tenantguard_common.contracts import ReplicaView

from .scaler import Action

log = logging.getLogger("tenantguard.brain")
WORKER_PORT = 8100
MiB = 1024 * 1024


@dataclass
class ActionResult:
    action: Action
    ok: bool
    replica: Optional[ReplicaView] = None     # the replica after the action (None after a stop)
    error: str = ""


class Backend(Protocol):
    async def list_replicas(self) -> List[ReplicaView]: ...
    async def apply(self, actions: List[Action]) -> List[ActionResult]: ...
    async def cpu_ms(self, ids: List[str]) -> Dict[str, float]: ...
    async def aclose(self) -> None: ...


def _view(a: Action, url: str, state: str = "starting") -> ReplicaView:
    return ReplicaView(id=a.replica_id, project=a.project, url=url, state=state, borrowed=a.borrowed,
                       nano_cpus=a.nano_cpus, cpu_shares=a.cpu_shares, mem_mb=a.mem_mb, started_at=time.time(),
                       tenants=list(a.tenants))


# ---------------------------------------------------------------------------------------------- Docker
@dataclass
class DockerCfg:
    image: str
    stack: str                     # compose project name; scopes every container we touch
    network: str = ""              # user-defined network the gateway is on ("" = default bridge, IP routing)
    cgroup_parent: str = ""
    socket_path: str = "/var/run/docker.sock"
    api: str = "v1.41"


class DockerBackend:
    def __init__(self, cfg: DockerCfg, client: Optional[httpx.AsyncClient] = None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=cfg.socket_path),
                                                  base_url=f"http://docker/{cfg.api}", timeout=10.0)

    def _name(self, replica_id: str) -> str:
        return f"{self.cfg.stack}-{replica_id}"

    def _create_body(self, a: Action) -> dict:
        labels = {"tg.stack": self.cfg.stack, "tg.project": a.project, "tg.replica": a.replica_id,
                  "tg.borrowed": "1" if a.borrowed else "0", "tg.tenants": ",".join(a.tenants),
                  "tg.mem_mb": str(a.mem_mb), "tg.started_at": f"{time.time():.3f}"}
        host = {"NanoCpus": a.nano_cpus, "CpuShares": a.cpu_shares, "Memory": a.mem_mb * MiB,
                "MemorySwap": a.mem_mb * MiB, "PidsLimit": 128, "RestartPolicy": {"Name": "no"}}
        if self.cfg.cgroup_parent:
            host["CgroupParent"] = self.cfg.cgroup_parent
        body = {"Image": self.cfg.image, "Labels": labels, "HostConfig": host,
                "Env": [f"SERVER_NAME={a.replica_id}", f"TENANTGUARD_TENANTS={','.join(a.tenants)}"]}
        if self.cfg.network:
            host["NetworkMode"] = self.cfg.network
            body["NetworkingConfig"] = {"EndpointsConfig": {self.cfg.network: {"Aliases": [a.replica_id]}}}
        return body

    async def _url(self, a: Action) -> str:
        if self.cfg.network:
            return f"http://{a.replica_id}:{WORKER_PORT}"
        r = await self.client.get(f"/containers/{self._name(a.replica_id)}/json")
        r.raise_for_status()
        return f"http://{r.json()['NetworkSettings']['IPAddress']}:{WORKER_PORT}"

    async def _one(self, a: Action) -> ActionResult:
        name = self._name(a.replica_id)
        if a.kind == "start":
            r = await self.client.post("/containers/create", params={"name": name}, json=self._create_body(a))
            r.raise_for_status()
            (await self.client.post(f"/containers/{name}/start")).raise_for_status()
            return ActionResult(a, True, _view(a, await self._url(a)))
        if a.kind == "update":
            body = {"NanoCpus": a.nano_cpus, "CpuShares": a.cpu_shares}
            (await self.client.post(f"/containers/{name}/update", json=body)).raise_for_status()
            return ActionResult(a, True)
        if a.kind == "stop":
            r = await self.client.delete(f"/containers/{name}", params={"force": "true"})
            if r.status_code not in (204, 404):
                r.raise_for_status()
            return ActionResult(a, True)
        return ActionResult(a, True)                      # drain: routing only, nothing to do in Docker

    async def apply(self, actions: List[Action]) -> List[ActionResult]:
        out = []
        for a in actions:
            try:
                out.append(await self._one(a))
            except Exception as e:              # never raise: the next tick retries
                log.warning("docker %s %s failed: %s", a.kind, a.replica_id, e)
                out.append(ActionResult(a, False, error=f"{type(e).__name__}: {e}"))
        return out

    async def list_replicas(self) -> List[ReplicaView]:
        """Our stack's replica containers (after a brain restart). State 'starting' until /healthz answers."""
        flt = json.dumps({"label": [f"tg.stack={self.cfg.stack}", "tg.replica"]})
        r = await self.client.get("/containers/json", params={"all": "true", "filters": flt})
        r.raise_for_status()
        out = []
        for c in r.json():
            lb = c.get("Labels") or {}
            if lb.get("tg.stack") != self.cfg.stack or c.get("State") != "running":
                continue
            hc = (await self.client.get(f"/containers/{c['Id']}/json")).json().get("HostConfig", {})
            a = Action("start", lb["tg.project"], lb["tg.replica"], int(hc.get("NanoCpus") or 0),
                       int(hc.get("CpuShares") or 0), int(lb.get("tg.mem_mb", "0")), lb.get("tg.borrowed") == "1",
                       [t for t in lb.get("tg.tenants", "").split(",") if t])
            v = _view(a, await self._url(a))
            out.append(v.model_copy(update={"started_at": float(lb.get("tg.started_at", "0"))}))
        return out

    async def cpu_ms(self, ids: List[str]) -> Dict[str, float]:
        """Cumulative cgroup CPU per replica (ms): exact per-project usage, including unmetered work."""
        async def one(rid: str) -> Optional[float]:
            try:
                r = await self.client.get(f"/containers/{self._name(rid)}/stats", params={"stream": "false"})
                r.raise_for_status()
                return r.json()["cpu_stats"]["cpu_usage"]["total_usage"] / 1e6
            except Exception:
                return None
        vals = await asyncio.gather(*(one(i) for i in ids))
        return {i: v for i, v in zip(ids, vals) if v is not None}

    async def aclose(self) -> None:
        await self.client.aclose()


# --------------------------------------------------------------------------------------------- process
class ProcessBackend:
    """Local runs: one worker process per replica on 127.0.0.1:<base_port + n>."""

    def __init__(self, base_port: int, worker_dir: Path, data_dir: Path, python: str = sys.executable):
        self.base_port = base_port
        self.worker_dir = worker_dir
        self.data_dir = data_dir
        self.python = python
        self.procs: Dict[str, subprocess.Popen] = {}
        self.views: Dict[str, ReplicaView] = {}

    def _port(self) -> int:
        used = {int(v.url.rsplit(":", 1)[1]) for v in self.views.values()}
        port = self.base_port
        while port in used:
            port += 1
        return port

    def _start(self, a: Action) -> ActionResult:
        port = self._port()
        env = {**os.environ, "SERVER_NAME": a.replica_id, "TENANTGUARD_TENANTS": ",".join(a.tenants),
               "CPU_LIMIT_CORES": str(a.nano_cpus / 1e9 or 1.0)}
        cmd = [self.python, "-m", "app", "--host", "127.0.0.1", "--port", str(port), "--server", a.replica_id,
               "--data-dir", str(self.data_dir / a.replica_id)]
        self.procs[a.replica_id] = subprocess.Popen(cmd, cwd=self.worker_dir, env=env)
        self.views[a.replica_id] = _view(a, f"http://127.0.0.1:{port}")
        return ActionResult(a, True, self.views[a.replica_id])

    def _stop(self, a: Action) -> ActionResult:
        p = self.procs.pop(a.replica_id, None)
        self.views.pop(a.replica_id, None)
        if p is not None and p.poll() is None:
            for child in psutil.Process(p.pid).children(recursive=True):
                child.kill()
            p.kill()
        return ActionResult(a, True)

    async def apply(self, actions: List[Action]) -> List[ActionResult]:
        out = []
        for a in actions:
            try:
                if a.kind == "start":
                    out.append(self._start(a))
                elif a.kind == "stop":
                    out.append(self._stop(a))
                else:                            # update: recorded (no cgroups locally); drain: routing only
                    out.append(ActionResult(a, True))
            except Exception as e:
                out.append(ActionResult(a, False, error=f"{type(e).__name__}: {e}"))
        return out

    async def list_replicas(self) -> List[ReplicaView]:
        return list(self.views.values())         # processes do not survive the brain

    async def cpu_ms(self, ids: List[str]) -> Dict[str, float]:
        out = {}
        for rid in ids:
            p = self.procs.get(rid)
            try:
                tree = [psutil.Process(p.pid)] + psutil.Process(p.pid).children(recursive=True)
                out[rid] = sum(sum(x.cpu_times()[:2]) for x in tree) * 1000.0
            except (psutil.Error, AttributeError):
                continue
        return out

    async def aclose(self) -> None:
        for rid in list(self.procs):
            self._stop(Action("stop", "", rid))
