import asyncio
import json

import httpx
import pytest

from tenantguard_common.contracts import AttackReq, RunReq, RunResult
from tenantguard_lab.core import Lab
from tenantguard_lab.scenario import Marks, ScenarioDriver, Timeline, compute_result, run_timeline
from tenantguard_lab.settings import load_config
from tenantguard_lab.stats import Recorder, Sample


class FakeDriver(ScenarioDriver):
    def __init__(self, calm_running=False, fail_at=None):
        self.t = 1000.0
        self.log = []
        self.calm = calm_running
        self.fail_at = fail_at

    def now(self):
        return self.t

    async def sleep(self, s):
        if self.fail_at is not None and self.t + s > self.fail_at:
            raise asyncio.CancelledError
        self.t += s

    async def set_mode(self, mode):
        self.log.append((self.t, "mode", mode))

    async def ensure_calm(self):
        was, self.calm = self.calm, True
        self.log.append((self.t, "calm_on", was))
        return was

    async def restore_calm(self, was_running):
        self.calm = was_running
        self.log.append((self.t, "restore", was_running))

    async def start_attack(self, req: AttackReq):
        self.log.append((self.t, "attack_start", req.attack, req.tenant, req.intensity))
        return "atk-1"

    async def stop_attack(self, attack_id):
        self.log.append((self.t, "attack_stop", attack_id))


def test_timeline_offsets():
    tl = Timeline(warmup_s=3, calm_s=20, attack_s=40)
    assert (tl.baseline_start, tl.attack_start, tl.attack_end, tl.end) == (3, 23, 63, 83)
    assert [p[0] for p in tl.phases()] == ["warmup", "baseline", "attack", "after"]


async def test_run_timeline_order_and_timing():
    d = FakeDriver()
    req = RunReq(scenario="whale", mode="industry", tenant="K", intensity=2.0, calm_s=20, attack_s=40)
    m = await run_timeline(d, req, "K", Timeline(3, 20, 40))
    assert [e[1] for e in d.log] == ["mode", "calm_on", "attack_start", "attack_stop", "restore"]
    assert d.log[0] == (1000.0, "mode", "industry")       # mode is set before any traffic
    assert d.log[2] == (1023.0, "attack_start", "whale", "K", 2.0)
    assert d.log[3][0] == 1063.0
    assert d.log[4] == (1083.0, "restore", False)          # calm was off before -> turned off again
    assert (m.t0, m.baseline_start, m.attack_start, m.attack_end, m.end) == (1000, 1003, 1023, 1063, 1083)


async def test_run_timeline_cancel_stops_attack_and_restores():
    d = FakeDriver(calm_running=True, fail_at=1040.0)
    with pytest.raises(asyncio.CancelledError):
        await run_timeline(d, RunReq(scenario="flood", mode="off"), "K", Timeline(3, 20, 40))
    kinds = [e[1] for e in d.log]
    assert kinds[-2:] == ["attack_stop", "restore"] and d.log[-1][2] is True


def _rec_with(phases, attacker="K"):
    rec = Recorder(retention_s=10_000)
    for tenant in ["A", "B", attacker]:
        for t0, t1, lat in phases:
            t = t0
            while t < t1:
                rec.add(tenant, Sample(t, lat if tenant != attacker else 1.0, 200, "calm"))
                t += 0.05
    return rec


def test_compute_result_phases_recovery_and_attacker_counts():
    m = Marks(t0=0, baseline_start=3, attack_start=23, attack_end=63, end=83)
    # victims: 10 ms baseline, 200 ms for the first 10 s of the attack, then 10 ms (mitigated)
    rec = _rec_with([(3, 23, 10.0), (23, 33, 200.0), (33, 83, 10.0)])
    for i in range(30):
        rec.add("K", Sample(30 + i * 0.1, 1.0, 429, "attack"))
    rec.add("K", Sample(40, 1.0, 503, "attack"))
    rec.add("K", Sample(90, 1.0, 429, "attack"))          # after the run: not counted
    rec.add("A", Sample(40, 9999.0, 200, "attack"))       # only victims' calm traffic counts
    res = compute_result(rec, ["A", "B", "K"], RunReq(scenario="whale", mode="off"), "K", m, "run-x")
    assert res.victim_p99_ms.baseline == 10.0
    assert res.victim_p99_ms.attack == 200.0
    assert res.victim_p99_ms.after == 10.0
    assert set(res.victim_p99_by_tenant) == {"A", "B"}   # attacker excluded
    assert res.time_to_recover_s == 10.0
    assert (res.attacker_429, res.attacker_503) == (30, 1)
    assert res.attacker == "K" and res.started_at == 0


def test_compute_result_never_recovers():
    m = Marks(t0=0, baseline_start=3, attack_start=23, attack_end=63, end=83)
    rec = _rec_with([(3, 23, 10.0), (23, 83, 200.0)])
    res = compute_result(rec, ["A", "B", "K"], RunReq(scenario="whale", mode="off"), "K", m, "r")
    assert res.time_to_recover_s is None


async def test_lab_run_end_to_end_writes_runresult(tmp_path):
    """Short real run against a mock gateway that is slow during the attack window."""
    cfg = load_config()
    cfg.results_dir = tmp_path
    cfg.run["warmup_s"] = 0.2
    cfg.brain_url = "http://brain.invalid"
    state = {"mode": None, "attack": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/admin/mode":
            if req.headers.get("X-TenantGuard-Control-Token") != "dev":
                return httpx.Response(401)
            state["mode"] = json.loads(req.content)["mode"]
            return httpx.Response(200, json={"mode": state["mode"]})
        if req.url.host == "brain.invalid":
            return httpx.Response(200, json=[{"type": "incident", "stage": "detected", "ts": state.get("t_att", 0) + 0.5,
                                               "tenant": "K", "incident_id": "i1"}])
        if req.url.params.get("n") == str(cfg.attacks["whale"]["params"]["n"]) and req.headers["X-API-Key"] == "tk_K_demo":   # the whale
            state["attack"] = True
            state.setdefault("t_att", __import__("time").time())
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={})

    lab = Lab(cfg, transport=httpx.MockTransport(handler))
    info = lab.start_run(RunReq(scenario="whale", mode="tenantguard", tenant="K", intensity=5, calm_s=1.0, attack_s=1.5))
    await lab.wait_run()
    await lab.aclose()
    assert info.state == "done", info.error
    assert state["mode"] == "tenantguard"
    files = list(tmp_path.glob("*-whale-tenantguard.json"))
    assert len(files) == 1
    res = RunResult.model_validate_json(files[0].read_text(encoding="utf-8"))
    assert res.file == f"lab/results/{files[0].name}"
    assert res.attacker == "K" and res.attacker_429 > 0
    assert res.victim_p99_ms.baseline > 0 and "K" not in res.victim_p99_by_tenant
    assert res.time_to_detect_s is not None and 0 <= res.time_to_detect_s < 1.5
    assert not lab.engine.calm_running                    # calm restored to its previous (off) state
    marks = info.view()["marks"]                          # actual phase timestamps exposed for analysis
    assert marks["t0"] <= marks["baseline_start"] < marks["attack_start"] < marks["attack_end"] < marks["end"]
    assert marks["attack_end"] - marks["attack_start"] >= 1.5


async def test_attack_start_marked_after_attack_is_running():
    class SlowStart(FakeDriver):
        async def start_attack(self, req):
            self.t += 4.0                                   # process spawn delay
            return await super().start_attack(req)

    d = SlowStart()
    m = await run_timeline(d, RunReq(scenario="whale", mode="off"), "K", Timeline(3, 20, 40))
    assert m.attack_start == 1027.0 and m.attack_end == 1067.0   # attack window = full 40 s of real attack
    assert m.attack_start - m.baseline_start == 24.0             # baseline window absorbs the delay


async def test_cold_start_extends_warmup_only_when_calm_was_off():
    tl = Timeline(3, 20, 40, cold_start_s=10)
    cold = await run_timeline(FakeDriver(calm_running=False), RunReq(scenario="flood", mode="off"), "K", tl)
    warm = await run_timeline(FakeDriver(calm_running=True), RunReq(scenario="flood", mode="off"), "K", tl)
    assert cold.baseline_start - cold.t0 == 13 and warm.baseline_start - warm.t0 == 3


def test_victim_kinds_filter():
    m = Marks(t0=0, baseline_start=0, attack_start=10, attack_end=20, end=30)
    rec = Recorder(retention_s=10_000)
    for i in range(300):
        t = i * 0.1
        rec.add("A", Sample(t, 5.0, 200, "calm", "light"))
        rec.add("A", Sample(t, 400.0, 200, "calm", "search"))   # slow by its own weight, not interference
    req = RunReq(scenario="whale", mode="off")
    only_light = compute_result(rec, ["A", "K"], req, "K", m, "r", victim_kinds=["light", "read"])
    everything = compute_result(rec, ["A", "K"], req, "K", m, "r")
    assert only_light.victim_p99_ms.baseline == 5.0
    assert everything.victim_p99_ms.baseline == 400.0


def test_read_and_write_keys_match_worker_seed():
    from tenantguard_lab.engine import Engine
    from tenantguard_lab.settings import load_config
    cfg = load_config()
    eng = Engine(cfg, seed=1)
    for _ in range(50):
        r = eng.build(cfg.endpoints["read"])
        assert len(r["params"]["key"]) == 5 and r["params"]["key"].startswith("k")   # k0000..k0999
        w = eng.build(cfg.endpoints["write"])
        assert set(w["json"]) == {"key", "value"} and w["json"]["key"][1:].isdigit()
