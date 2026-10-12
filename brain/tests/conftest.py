"""Shared fixtures: settings with a temp state dir, and a Harness that drives the real Runtime against the
simulator on a virtual clock (ladder every 250 ms, stats + control every 1 s; no network, no Docker)."""
from __future__ import annotations

import json
from typing import List

import httpx
import pytest

from tenantguard_brain import registry as R
from tenantguard_brain import settings
from tenantguard_brain.backends import ActionResult
from tenantguard_brain.runtime import Runtime
from tenantguard_brain.sim import Phase, Simulator
from tenantguard_common.contracts import IncidentEvent, ReplicaView, Snapshot

T0 = 1_000_000.0


@pytest.fixture
def cfg(tmp_path):
    c = settings.load()
    c.control.registry_path = tmp_path / "registry.json"
    c.control.events_path = tmp_path / "events.jsonl"
    return c


class SimBackend:
    """Backend whose replicas live in the simulator."""

    def __init__(self, sim: Simulator):
        self.sim = sim

    async def list_replicas(self) -> List[ReplicaView]:
        return []

    async def apply(self, actions):
        out = []
        for a in actions:
            if a.kind == "start":
                self.sim.start_replica(a.replica_id, a.project, a.nano_cpus, a.cpu_shares, a.tenants)
                out.append(ActionResult(a, True, ReplicaView(
                    id=a.replica_id, project=a.project, url=f"http://{a.replica_id}", state="starting",
                    borrowed=a.borrowed, nano_cpus=a.nano_cpus, cpu_shares=a.cpu_shares, mem_mb=a.mem_mb,
                    started_at=self.sim.now, tenants=list(a.tenants))))
                continue
            if a.kind == "update":
                self.sim.update_replica(a.replica_id, a.nano_cpus, a.cpu_shares)
            elif a.kind == "stop":
                self.sim.stop_replica(a.replica_id)
            out.append(ActionResult(a, True))
        return out

    async def cpu_ms(self, ids):
        return {i: self.sim.container_ms(i) for i in ids if i in self.sim.replicas}

    async def aclose(self):
        pass


class Harness:
    def __init__(self, cfg, phases: List[Phase], mode: str = "tenantguard", **simkw):
        reg = R.seed(cfg.seed_pool, cfg.seed_projects, cfg.seed_tenants, cfg.control.scale)
        self.sim = Simulator(R.snap_tenants(reg, cfg.tier_quotas), phases, pool_cpu=reg.pool.cpu_total, start=T0,
                             mode=mode, **simkw)
        self.rt = Runtime(cfg, backend=SimBackend(self.sim), registry=reg)
        self.snapshots: List[Snapshot] = []
        self.rt.client = httpx.AsyncClient(transport=httpx.MockTransport(self._gateway))
        self.n = 0

    def _gateway(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/control/snapshot":
            snap = Snapshot.model_validate(json.loads(req.content))
            self.snapshots.append(snap)
            self.sim.apply_snapshot(snap)
            return httpx.Response(200, json={"ok": True})
        if req.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    async def run_until(self, until: float, log=None) -> None:
        brain = self.rt.brain
        while self.sim.now < until - 1e-9:
            frame = self.sim.step()
            now = self.sim.now
            brain.ingest_frame(frame, now)
            self.n += 1
            if self.n % 4 == 0:
                routed = {r for rs in self.sim.ready.values() for r in rs}
                for rid in [r for r in self.sim.replicas if r in routed]:
                    brain.ingest_stats(self.sim.worker_stats(rid), now)
            res = brain.tick(now)
            self.rt.publish(res.events)
            if res.changed:
                await self.rt.push_snapshot()
            if self.n % 4 == 0:
                await self.rt.control_once(now)
            if log is not None:
                log.append((now, {t: (s.level, s.factor) for t, s in brain.ladders.items()},
                            {p: len(rs) for p, rs in self.sim.ready.items()}))

    def events(self) -> List[IncidentEvent]:
        return list(self.rt.brain.book.log)
