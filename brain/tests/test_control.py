"""The control loop on the simulator: lending, scaling, sleep and wake, hash split, no-blame contention."""
import asyncio

from tenantguard_brain import registry as R
from tenantguard_brain.sim import Phase
from tenantguard_common.contracts import ProjectPatch, TenantCreate

from .conftest import T0, Harness


def run(h: Harness, until: float, log=None) -> Harness:
    asyncio.run(h.run_until(until, log))
    return h


def project(h: Harness, pid: str):
    return next(p for p in h.rt.control.pool_view().projects if p.id == pid)


SPIKE = [Phase("calm", 20), Phase("spike", 40, burst={"E", "G", "K"}, burst_mult=2.0,
                                  idle={"H", "L", "C", "F", "I"}), Phase("after", 40)]


def test_honest_project_spike_borrows_idle_cpu_and_nobody_is_blamed(cfg):
    h = Harness(cfg, SPIKE)
    log = []
    run(h, T0 + 45, log)
    mob = project(h, "mobility")
    assert mob.alloc_cpu > mob.floor_cpu and mob.loan_cpu > 0           # lent: above its own floor
    assert len([r for r in mob.replicas if r.state == "ready"]) == 2
    assert h.events() == []                                             # an honest burst is not an incident
    assert max(v or 0 for t, v in h.rt.brain.slowdowns.items() if t in "EGK") < 1.0   # served after scaling


def test_scale_back_down_after_the_spike(cfg):
    h = run(Harness(cfg, SPIKE), T0 + 100)
    assert len([r for r in project(h, "mobility").replicas if r.state == "ready"]) == 1
    assert project(h, "mobility").loan_cpu == 0


def test_a_project_allowed_to_sleep_stops_and_wakes_on_demand(cfg):
    h = Harness(cfg, [Phase("calm", 5), Phase("idle", 400, idle={"C", "F", "I"})])
    reg = R.update_project(h.rt.control.registry, "free", ProjectPatch(warm_min=0), cfg.control.scale)
    h.rt.set_registry(reg)
    run(h, T0 + 320)
    assert project(h, "free").replicas == [] and project(h, "free").state == "asleep"
    assert "free" not in h.sim.ready or h.sim.ready["free"] == []
    assert h.rt.wake("free")
    asyncio.run(h.rt.control_once(h.sim.now))                           # the wake ticks the loop at once
    assert [r.state for r in project(h, "free").replicas] == ["starting"]
    run(h, h.sim.now + 1.5)
    assert [r.state for r in project(h, "free").replicas] == ["ready"]


def test_hidden_work_is_found_by_the_hash_split_and_charged(cfg):
    h = Harness(cfg, [Phase("calm", 20), Phase("hidden", 60, hidden="F", hidden_ms=600), Phase("after", 20)])
    run(h, T0 + 100)
    ev = h.events()
    rounds = [e for e in ev if e.stage == "proving"]
    verdict = [e for e in ev if e.stage == "verdict"]
    assert len(rounds) == 2                                             # ceil(log2 3) rounds for C, F, I
    assert len(verdict) == 1 and verdict[0].tenant == "F" and verdict[0].report["verdict"] == "costly_pattern"
    assert {e.tenant for e in ev if e.tenant} == {"F"}                  # nobody else blamed
    assert any(c.tenant == "F" for s in h.snapshots for c in s.limits)  # the ladder acted on the evidence
    assert any(s.pins for s in h.snapshots) and h.snapshots[-1].pins == {}   # pins only during the test


def test_a_slow_host_is_reported_without_blame(cfg):
    h = run(Harness(cfg, [Phase("calm", 30), Phase("fault", 30, fault_ms=30), Phase("after", 20)]), T0 + 80)
    ev = h.events()
    contention = [e for e in ev if e.report and e.report.get("verdict") == "host_fault"]
    assert len(contention) == 1 and contention[0].tenant is None and 33 <= contention[0].ts - T0 <= 37
    assert all(s.limits == [] for s in h.snapshots)                      # nobody throttled


def test_a_new_tenant_replaces_its_projects_replicas(cfg):
    h = run(Harness(cfg, [Phase("calm", 30)]), T0 + 10)
    before = [r.id for r in project(h, "media").replicas]
    reg, key = R.add_tenant(h.rt.control.registry, "media", TenantCreate(id="M2", name="New"), cfg.control.scale)
    h.rt.set_registry(reg)
    run(h, T0 + 20)
    after = project(h, "media").replicas
    assert [r.id for r in after] != before and all("M2" in r.tenants for r in after)
    assert any(t.id == "M2" for t in h.snapshots[-1].tenants)


def test_replica_that_never_becomes_healthy_is_stopped(cfg):
    h = Harness(cfg, [Phase("calm", 60)])
    h.rt.client._transport = type(h.rt.client._transport)(
        lambda req: __import__("httpx").Response(503) if req.url.path.endswith("/healthz") else h._gateway(req))
    run(h, T0 + 5)
    assert {r.state for r in h.rt.control.fleet.replicas.values()} == {"starting"}
    run(h, T0 + 5 + cfg.control.start_timeout_s + 2)
    assert all(h.rt.control.fleet.replicas[r].started_at > T0 + 5 for r in h.rt.control.fleet.replicas)
