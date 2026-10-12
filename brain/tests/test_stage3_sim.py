"""Server health -> rescue -> decoder on the simulator, virtual clock."""
from tenantguard_brain.core import Brain
from tenantguard_brain.sim import Phase, Simulator, whale_scenario

from .conftest import T0, drive, drive_jobs


def stages(evs):
    """Stage sequence with consecutive repeats folded (the slow set may grow in two steps)."""
    out = []
    for e in evs:
        if not out or out[-1] != e.stage:
            out.append(e.stage)
    return out


def srv_events(events):
    return [e for e in events if e.incident_id.startswith("srv-")]


def host_fault_run(cfg, mode="tenantguard"):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, [Phase("calm", 120)], start=T0, mode=mode)
    drive(brain, sim, T0 + 20)
    sim.set_fault("w4", 40.0)
    ev1, cmd1 = drive_jobs(brain, sim, T0 + 50)
    sim.set_fault("w4", 0.0)
    ev2, cmd2 = drive_jobs(brain, sim, T0 + 80)
    return brain, sim, ev1 + ev2, cmd1 + cmd2


def test_host_fault_is_rescued_and_named_without_blaming_a_tenant(cfg):
    brain, sim, events, limits = host_fault_run(cfg)
    srv = srv_events(events)
    assert stages(srv) == ["detected", "rescued", "named", "proving", "verdict", "resolved", "closed"]
    by = {e.stage: e for e in srv}
    # rescue within ~8 s of the fault (5 s window at the 0.3 quantile + 4 s of persistence: a 1-3 s
    # CPU-quota stall on the host must not open an incident)
    assert by["rescued"].ts - (T0 + 20) <= 8.0
    first_route = sim.route_log[0][1]
    assert set(first_route.weights) == {"B", "E", "G", "L"}          # exactly w4's tenants
    assert all(w["w4"] == 0.0 for w in first_route.weights.values())
    # named: host suspect, nobody blamed, no throttling
    assert by["named"].suspects == [] and by["named"].tenant is None
    assert by["named"].report["host_suspects"] == ["w4"]
    assert "No tenant blamed" in by["named"].detail
    assert limits == [] and not [e for e in events if e.incident_id.startswith("inc-")]
    # proved by drain: still slow while idle -> host_fault, kept drained until it heals
    assert by["verdict"].verdict == "host_fault" and "No tenant blamed" in by["verdict"].detail
    drains = [(c.server, c.drained) for _, p, c in sim.control_log if p.endswith("/drain")]
    assert drains[0] == ("w4", True) and drains[-1] == ("w4", False)
    # resolved after the fault is cleared (+50 s), route weights back to equal
    assert T0 + 50 < by["resolved"].ts < T0 + 62
    last = sim.route_log[-1][1]
    assert all(abs(v - 1 / 3) < 0.01 for w in last.weights.values() for v in w.values())
    assert brain.s3.inc is None


def test_servers_show_slow_in_the_snapshot(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, [Phase("calm", 60)], start=T0)
    drive(brain, sim, T0 + 20)
    sim.set_fault("w4", 40.0)
    drive(brain, sim, T0 + 30)
    snap = brain.snapshot(sim.now)
    w4 = next(s for s in snap.servers if s.server == "w4")
    assert w4.status == "slow" and w4.peer_ratio > 3
    assert all(s.status == "healthy" for s in snap.servers if s.server != "w4")
    assert "w4 slow" in snap.status_sentence and "host suspect w4" in snap.status_sentence


def test_whale_is_named_from_its_barcode(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 60), quota_mult={"K": 3}, start=T0)
    events, _ = drive(brain, sim, T0 + sim.total_s)
    named = [e for e in srv_events(events) if e.stage == "named" and e.suspects]
    assert named and all([s.tenant for s in e.suspects] == ["K"] for e in named)
    assert named[0].suspects[0].confidence > 0.9 and named[0].ts - (T0 + 20) < 9
    allnamed = [e for e in srv_events(events) if e.stage == "named"]
    assert all(not e.report["host_suspects"] for e in allnamed)          # K's servers are never host suspects
    held = [e for e in allnamed if e.report.get("held_hosts")]
    assert all(set(sum(e.report["held_hosts"].values(), [])) == {"K"} for e in held)
    # the culprit itself is never moved by a rescue once named, and every rescue keeps to barcodes
    for t, rc in sim.route_log:
        for tenant, w in rc.weights.items():
            assert set(w) <= set(sim.tenants[tenant].servers)


def test_no_routing_outside_tenantguard_mode(cfg):
    brain, sim, events, _ = host_fault_run(cfg, mode="industry")
    assert srv_events(events) == [] and sim.route_log == []
    # still observed: health runs in every mode
    assert brain.s3.health.servers["w4"].peer_ratio > 0


def test_quiet_fleet_opens_no_server_incident(cfg):
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, [Phase("calm", 120)], start=T0)
    events, _ = drive(brain, sim, T0 + 120)
    assert srv_events(events) == [] and sim.route_log == []


def test_rescue_first_then_throttle_only_if_harm_persists(cfg):
    """With rescue applied, K's neighbours are moved away within ~2 s and stop being hurt, so the ladder
    never needs to throttle K: move, don't punish. The rescue holds while K is noisy and is cleared
    once the attack stops."""
    brain = Brain(cfg)
    sim = Simulator(cfg.tenants, whale_scenario("K", 20, 40, 60), quota_mult={"K": 3}, start=T0)
    events, limits = drive(brain, sim, T0 + sim.total_s)
    srv = srv_events(events)
    assert stages(srv) == ["detected", "rescued", "named", "resolved", "closed"]   # no prover: named only
    by = {e.stage: e for e in srv}
    assert by["rescued"].ts - (T0 + 20) <= 8.0
    assert T0 + 60 < by["resolved"].ts < T0 + 70                 # the attack ends at +60 s
    assert not [c for _, c in limits if c.factor < 1]            # nobody throttled
    assert brain.ladders["K"].level == "normal"
    # the culprit's own traffic was never moved
    assert all("K" not in rc.weights for _, rc in sim.route_log[:-1])
