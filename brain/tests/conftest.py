import pytest

from tenantguard_brain import settings
from tenantguard_brain.core import Brain
from tenantguard_brain.sim import Simulator

T0 = 1_000_000.0


@pytest.fixture
def cfg():
    return settings.load()


def drive(brain: Brain, sim: Simulator, until: float, *, apply=True, routes_apply=True, sent_ok=True, log=None):
    """Run sim + brain in lock-step on a virtual clock (brain tick = telemetry tick, stats at 1 Hz)."""
    n = 0
    events, commands = [], []
    routes = getattr(sim, "route_log", None)
    while sim.now < until - 1e-9:
        frame = sim.step()
        now = sim.now
        brain.ingest_frame(frame, now)
        n += 1
        if n % 4 == 0:
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
            if routes is not None:
                routes.append((now, rc))
            if apply and routes_apply:
                sim.apply_route_weights(rc.weights, rc.ttl_s)
            brain.s3.mark_routes(True, now)
        events.extend(res.events)
        if log is not None:
            log.append((now, {t: (s.level, s.factor) for t, s in brain.ladders.items()}))
    return events, commands
