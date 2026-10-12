import os
from pathlib import Path

# test what this checkout contains (yaml + layout), not the main repo the shared package was installed from
os.environ["TENANTGUARD_ROOT"] = str(Path(__file__).resolve().parents[2])

import asyncio  # noqa: E402

import pytest  # noqa: E402

from tenantguard_brain import settings  # noqa: E402
from tenantguard_brain.core import Brain  # noqa: E402
from tenantguard_brain.prover import HostCheck, HostProver, ProofRequest  # noqa: E402
from tenantguard_brain.sim import Simulator  # noqa: E402

T0 = 1_000_000.0


@pytest.fixture
def cfg():
    return settings.load()


def drive(brain: Brain, sim: Simulator, until: float, *, apply=True, routes_apply=True, sent_ok=True, log=None):
    """Run sim + brain in lock-step on a virtual clock (brain tick = telemetry tick, stats + canary at 1 Hz).
    Control commands are applied to the sim like the gateway would; async jobs (proofs, drain tests) are
    queued on `sim.jobs` for drive_jobs()."""
    n = 0
    events, commands = [], []
    if not hasattr(sim, "jobs"):
        sim.jobs = []
    while sim.now < until - 1e-9:
        frame = sim.step()
        now = sim.now
        brain.ingest_frame(frame, now)
        n += 1
        if int(now + 1e-9) != int(now - sim.tick_ms / 1000.0 + 1e-9):     # every whole sim second
            for s in sim.workers:
                brain.ingest_stats(sim.worker_stats(s), now)
                brain.ingest_canary(s, sim.canary_ms(s), now)
        res = brain.tick(now)
        for c in res.commands:
            commands.append((now, c))
            if apply:
                sim.apply_limits(c.tenant, c.factor, c.weight_factor, c.ttl_s)
            brain.mark_sent(c, sent_ok, now)
        for rc in res.route_cmds:
            sim.route_log.append((now, rc))
            if apply and routes_apply:
                sim.apply_route_weights(rc.weights, rc.ttl_s)
            brain.s3.mark_routes(True, now)
        for _key, path, cmd in res.control:
            if apply and not path.endswith("/charge"):
                sim.apply(path, cmd)
        sim.jobs.extend(res.jobs)
        events.extend(res.events)
        if log is not None:
            log.append((now, {t: (s.level, s.factor) for t, s in brain.ladders.items()}))
    return events, commands


class SimEnv:
    """ProveEnv over the simulator: sleeping advances the simulation (brain included)."""

    def __init__(self, brain: Brain, sim: Simulator):
        self.brain, self.sim = brain, sim
        self.events, self.commands = [], []

    def readings(self):
        return self.brain.readings(self.sim.now)

    async def send(self, path, model) -> bool:
        self.sim.apply(path, model)
        return True

    async def sleep(self, seconds: float) -> None:
        ev, cmds = drive(self.brain, self.sim, self.sim.now + seconds)
        self.events += ev
        self.commands += cmds


def drive_jobs(brain: Brain, sim: Simulator, until: float, prover=None, host_prover=None, **kw):
    """drive(), running queued proofs / drain tests as they appear, until `until`."""
    env = SimEnv(brain, sim)
    host_prover = host_prover or HostProver(brain.cfg.prover, brain.cfg.verdicts)
    events, commands = [], []
    while sim.now < until - 1e-9:
        ev, cmds = drive(brain, sim, min(until, sim.now + 0.25), **kw)
        events += ev
        commands += cmds
        while getattr(sim, "jobs", None):
            job = sim.jobs.pop(0)
            env.events, env.commands = [], []
            if isinstance(job, ProofRequest):
                result = asyncio.run(prover.prove(job, env))
                res = brain.on_proof(result, sim.now)
            elif isinstance(job, HostCheck):
                result = asyncio.run(host_prover.check(job, env))
                res = brain.on_host(result, sim.now)
            else:                                      # ("restore_spare", cores)
                if prover is not None:
                    asyncio.run(prover.restore_spare(job[1]))
                continue
            events += env.events + res.events
            commands += env.commands
            for _key, path, cmd in res.control:
                if not path.endswith("/charge"):
                    sim.apply(path, cmd)
            sim.jobs.extend(res.jobs)
    return events, commands
