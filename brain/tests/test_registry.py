"""Registry: invariants I1-I3, keys, seeding from the yaml, atomic save."""
import random

import pytest

from tenantguard_brain import registry as R
from tenantguard_brain.registry_store import load, save
from tenantguard_brain.scaler import ScaleCfg
from tenantguard_common.config import load_pool, load_projects, load_settings, load_tenants
from tenantguard_common.contracts import PoolSpec, ProjectPatch, ProjectSpec, TenantCreate

CFG = ScaleCfg()


@pytest.fixture
def reg():
    s = load_settings()
    return R.seed(load_pool(s), load_projects(s), load_tenants(s), CFG)


def test_seed_matches_the_yaml(reg):
    assert sorted(reg.projects) == ["commerce", "free", "health", "media", "mobility"]
    assert R.tenants_of(reg, "mobility") == ["E", "G", "K"]
    assert reg.tenants["A"].key_sha256 == R.hash_key("tk_A_demo")
    assert R.check_capacity(reg, CFG) == []


def test_floors_over_usable_are_rejected(reg):
    spec = ProjectSpec(id="big", name="Big", tier="pro", floor_cpu=1.0, cap_cpu=2.0)   # 4.5 + 1.0 > 5.175
    with pytest.raises(R.Invalid, match="sum of floors"):
        R.create_project(reg, spec, CFG)


def test_floor_cap_order_and_ids(reg):
    with pytest.raises(R.Invalid, match="floor_cpu <= cap_cpu"):
        R.update_project(reg, "media", ProjectPatch(cap_cpu=0.5), CFG)
    with pytest.raises(R.Invalid, match="project id"):
        R.create_project(reg, ProjectSpec(id="Bad Id", name="x", tier="free", floor_cpu=0.1, cap_cpu=1), CFG)
    with pytest.raises(R.Conflict):
        R.create_project(reg, ProjectSpec(id="media", name="x", tier="free", floor_cpu=0.1, cap_cpu=1), CFG)


def test_add_tenant_key_once_and_unique(reg):
    reg2, key = R.add_tenant(reg, "media", TenantCreate(id="M1", name="New"), CFG)
    assert key.startswith("tk_") and reg2.tenants["M1"].key_sha256 == R.hash_key(key)
    assert key not in reg2.model_dump_json()                         # only the hash is stored
    with pytest.raises(R.Conflict):
        R.add_tenant(reg2, "free", TenantCreate(id="m1", name="dup"), CFG)   # case-insensitive (I1)
    with pytest.raises(R.Conflict):
        R.add_tenant(reg2, "free", TenantCreate(id="Z", name="z", api_key="tk_A_demo"), CFG)
    with pytest.raises(R.NotFound):
        R.add_tenant(reg2, "nope", TenantCreate(id="Z", name="z"), CFG)


def test_remove_project_needs_no_tenants(reg):
    with pytest.raises(R.Conflict):
        R.remove_project(reg, "free")
    for t in ("C", "F", "I"):
        reg = R.remove_tenant(reg, t)
    assert "free" not in R.remove_project(reg, "free").projects


def test_memory_reservation_is_checked(reg):
    with pytest.raises(R.Invalid, match="memory"):
        R.set_pool(reg, PoolSpec(cpu_total=5.75, mem_total_mb=900), CFG)


def test_random_accepted_registries_hold_invariants(reg):
    rng = random.Random(11)
    for i in range(200):
        spec = ProjectSpec(id=f"p{i}", name="x", tier=rng.choice(["enterprise", "pro", "free"]),
                           floor_cpu=round(rng.uniform(0.01, 1.5), 3), cap_cpu=round(rng.uniform(0.01, 6), 3))
        try:
            reg = R.create_project(reg, spec, CFG)
        except R.Invalid:
            continue
        assert R.check_capacity(reg, CFG) == []


def test_store_round_trip_and_atomic(tmp_path, reg):
    path = tmp_path / "state" / "registry.json"
    assert load(path, lambda: reg) == reg and path.exists()
    reg2, _ = R.add_tenant(reg, "media", TenantCreate(id="N", name="n"), CFG)
    save(reg2, path)
    assert load(path, lambda: reg) == reg2
    assert not path.with_suffix(".json.tmp").exists()
