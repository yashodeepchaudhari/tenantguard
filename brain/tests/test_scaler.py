"""Scaler: replica memory, counts with hysteresis, CPU settings and the ordered action plan."""
import random

from tenantguard_brain.scaler import (Fleet, Hyst, ScaleCfg, Target, cpu_settings, own_replicas, plan,
                                      replica_memory_mb, replicas_needed)
from tenantguard_common.contracts import ProjectSpec, ReplicaView

CFG = ScaleCfg()
P = ProjectSpec(id="mobility", name="M", tier="pro", floor_cpu=0.75, cap_cpu=2.0)
SLEEPY = ProjectSpec(id="free", name="F", tier="free", floor_cpu=0.5, cap_cpu=1.0, warm_min=0)


def rep(rid, project="mobility", state="ready", nano=550_000_000, shares=7680, mem=192, borrowed=False,
        started=0.0, tenants=("E", "G", "K")):
    return ReplicaView(id=rid, project=project, url=f"http://{rid}:8100", state=state, borrowed=borrowed,
                       nano_cpus=nano, cpu_shares=shares, mem_mb=mem, started_at=started, tenants=list(tenants))


def test_replica_memory_from_config_values():
    assert replica_memory_mb(2, CFG) == 160
    assert replica_memory_mb(3, CFG) == 192


def test_own_replicas_and_golden_counts():
    assert own_replicas(P, CFG) == 1
    assert own_replicas(ProjectSpec(id="h", name="h", tier="enterprise", floor_cpu=1.25, cap_cpu=3), CFG) == 2
    # docs/v2/03 §5.3: alloc 1.94 -> 3 replicas, 1.095 -> 2, idle warm -> 1
    big = ProjectSpec(id="h", name="h", tier="enterprise", floor_cpu=1.25, cap_cpu=3.0)
    assert replicas_needed(big, 0, 3.0, 1.94, Hyst(), CFG)[0] == 3
    assert replicas_needed(P, 0, 2.0, 1.095, Hyst(), CFG)[0] == 2
    assert replicas_needed(P, 0, 0.1, 0.1, Hyst(), CFG)[0] == 1


def test_hysteresis_up_two_ticks_down_thirty():
    n, h = replicas_needed(P, 1, 2.0, 1.1, Hyst(), CFG)
    assert n == 1 and h.up == 1
    n, h = replicas_needed(P, 1, 2.0, 1.1, h, CFG)
    assert n == 2
    h = Hyst()
    for i in range(29):
        n, h = replicas_needed(P, 2, 0.1, 0.1, h, CFG)
        assert n == 2
    n, h = replicas_needed(P, 2, 0.1, 0.1, h, CFG)
    assert n == 1


def test_sleep_only_with_warm_min_zero_and_wake_at_once():
    h = Hyst()
    for _ in range(299):
        n, h = replicas_needed(SLEEPY, 1, 0.0, 0.0, h, CFG)
        assert n == 1
    n, h = replicas_needed(SLEEPY, 1, 0.0, 0.0, h, CFG)
    assert n == 0
    n, _ = replicas_needed(SLEEPY, 0, 0.3, 0.3, h, CFG)
    assert n == 1                                     # a sleeping project with demand starts now
    for _ in range(400):
        n, h = replicas_needed(P, 1, 0.0, 0.0, h, CFG)
    assert n == 1                                     # warm_min 1 never sleeps


def test_cpu_settings_by_mode():
    assert cpu_settings(P, 1.095, 2, CFG, "tenantguard") == (547_500_000, 3840)
    assert cpu_settings(P, 0.1, 1, CFG, "tenantguard")[0] == 750_000_000     # idle: cap stays at the floor
    assert cpu_settings(P, 3.0, 1, CFG, "tenantguard")[0] == 1_000_000_000   # one replica <= 1 core
    assert cpu_settings(P, 3.0, 1, CFG, "industry")[0] == 750_000_000
    assert cpu_settings(P, 3.0, 1, CFG, "off") == (0, 1024)


def target(n, nano=550_000_000, shares=7680, spec=P, tenants=("E", "G", "K")):
    return Target(spec=spec, tenants=list(tenants), replicas=n, nano_cpus=nano, cpu_shares=shares)


def test_plan_starts_with_new_ids_and_borrow_flag():
    fleet = Fleet({"tg-mobility-1": rep("tg-mobility-1")})
    acts = plan(fleet, {"mobility": target(3)}, 2000, 0.0, CFG)
    starts = [a for a in acts if a.kind == "start"]
    assert [a.replica_id for a in starts] == ["tg-mobility-2", "tg-mobility-3"]
    assert [a.borrowed for a in starts] == [True, True]      # own allowance of mobility is 1
    assert all(a.mem_mb == 192 and a.tenants == ["E", "G", "K"] for a in starts)


def test_plan_drains_borrowed_then_youngest_and_stops_after_timeout():
    fleet = Fleet({"a": rep("a", started=1), "b": rep("b", started=3), "c": rep("c", started=2, borrowed=True)})
    acts = plan(fleet, {"mobility": target(1)}, 2000, 0.0, CFG)
    assert [(a.kind, a.replica_id) for a in acts] == [("drain", "c"), ("drain", "b")]
    fleet = Fleet({"c": rep("c", state="draining")}, drain_since={"c": 10.0}, inflight={"c": 3})
    assert plan(fleet, {"mobility": target(0)}, 2000, 11.0, CFG) == []
    assert [a.kind for a in plan(fleet, {"mobility": target(0)}, 2000, 12.0, CFG)] == ["stop"]
    fleet.inflight["c"] = 0
    assert [a.kind for a in plan(fleet, {"mobility": target(0)}, 2000, 10.5, CFG)] == ["stop"]


def test_plan_replaces_replicas_whose_tenant_set_changed():
    fleet = Fleet({"tg-mobility-1": rep("tg-mobility-1", tenants=("E", "G"))})
    acts = plan(fleet, {"mobility": target(1)}, 2000, 0.0, CFG)
    assert [(a.kind, a.replica_id) for a in acts] == [("drain", "tg-mobility-1"), ("start", "tg-mobility-2")]


def test_plan_order_lower_before_start_before_raise():
    fleet = Fleet({"m": rep("m", nano=900_000_000),
                   "h": rep("h", project="health", nano=300_000_000, shares=12800, tenants=("A", "D"))})
    health = ProjectSpec(id="health", name="H", tier="enterprise", floor_cpu=1.25, cap_cpu=3.0)
    acts = plan(fleet, {"mobility": target(1, nano=500_000_000),
                        "health": target(2, nano=600_000_000, shares=6400, spec=health, tenants=("A", "D"))},
                5000, 0.0, CFG)
    kinds = [(a.kind, a.replica_id) for a in acts]
    assert kinds.index(("update", "m")) < kinds.index(("start", "tg-health-1")) < kinds.index(("update", "h"))


def test_plan_owner_reclaims_borrowed_memory_when_full():
    fleet = Fleet({"x": rep("x", project="health", borrowed=True, mem=160, tenants=("A", "D"))})
    health = ProjectSpec(id="health", name="H", tier="enterprise", floor_cpu=1.25, cap_cpu=3.0)
    acts = plan(fleet, {"health": target(1, spec=health, tenants=("A", "D")), "mobility": target(1)}, 200, 0.0, CFG)
    assert ("drain", "x") in [(a.kind, a.replica_id) for a in acts]
    assert not [a for a in acts if a.kind == "start"]                 # 160 + 192 > 200: waits for the stop


def test_plan_never_exceeds_usable_memory_random():
    rng = random.Random(3)
    for _ in range(300):
        fleet = Fleet({f"r{i}": rep(f"r{i}", borrowed=rng.random() < 0.5, mem=192) for i in range(rng.randint(0, 5))})
        usable = rng.randint(0, 2000)
        acts = plan(fleet, {"mobility": target(rng.randint(0, 6))}, usable, 0.0, CFG)
        used = sum(r.mem_mb for r in fleet.replicas.values())
        started = sum(a.mem_mb for a in acts if a.kind == "start")
        assert started == 0 or used + started <= usable
        first_start = next((i for i, a in enumerate(acts) if a.kind == "start"), len(acts))
        assert all(a.kind != "drain" for a in acts[first_start:])     # drains always come first
