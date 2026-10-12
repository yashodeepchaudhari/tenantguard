from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import demo  # noqa: E402

GW, BRAIN, LAB = "http://gw", "http://brain", "http://lab"


class Fake:
    """Records every request; serves brain state, lab metrics/results, attacks (501 for listed ones)."""

    def __init__(self, unavailable=(), attack_status: int = 200, results=None):
        self.calls: List[httpx.Request] = []
        self.unavailable, self.attack_status = set(unavailable), attack_status
        self.results = results or []
        self.n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        p = req.url.path
        if p == "/admin/mode":
            return httpx.Response(200, json=json.loads(req.content))
        if p == "/lab/attack":
            body = json.loads(req.content)
            if body["attack"] in self.unavailable:
                return httpx.Response(501, json={"detail": "ships in Wave 2"})
            if body.get("tenant") == demo.PROBE:                     # like the real lab: unknown tenant
                return httpx.Response(404, json={"detail": "unknown tenant"})
            if self.attack_status != 200:
                return httpx.Response(self.attack_status, text="boom")
            self.n += 1
            return httpx.Response(200, json={"attack_id": f"a{self.n}", **body})
        if p == "/api/state":
            return httpx.Response(200, json={
                "mode": "tenantguard", "fairness": 0.93, "utilization": 0.41, "status_sentence": "K is noisy",
                "tenants": [{"tenant": "A", "p95_ms": 220.0, "level": "normal", "factor": 1.0},
                            {"tenant": "K", "p95_ms": 900.0, "level": "throttle", "factor": 0.5}]})
        if p == "/lab/metrics":
            return httpx.Response(200, json={"tenants": {
                "A": {"calm": {"count": 50, "p95_ms": 240.0}},
                "K": {"calm": {"count": 10, "p95_ms": 5000.0}, "ok_per_s": 12.0, "r429_per_s": 88.0}}})
        if p == "/api/incidents":
            return httpx.Response(200, json=[{"incident_id": "srv-1", "stage": "named", "ts": 1.0,
                                              "detail": "Named KaamKaro (K) with 100% confidence"}])
        if p == "/lab/results":
            return httpx.Response(200, json=self.results)
        return httpx.Response(200, json={})

    def seq(self):
        out = []
        for r in self.calls:
            if r.url.path == "/admin/mode":
                out.append(("mode", json.loads(r.content)["mode"]))
            elif r.url.path == "/lab/attack":
                b = json.loads(r.content)
                if b.get("tenant") != demo.PROBE:                     # preflight probes are not attacks
                    out.append(("attack", b["attack"], b["duration_s"]))
            elif r.method == "DELETE":
                out.append(("stop",))
        return out


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(fake: Fake, **kw):
    clk = Clock()
    lines: List[str] = []
    d = demo.Demo(GW, BRAIN, LAB, "tok", client=httpx.Client(transport=httpx.MockTransport(fake)),
                  sleep=clk.sleep, clock=clk, out=lines.append, **kw)
    return d, clk, lines


def test_full_sequence_modes_attacks_durations_and_cleanup():
    fake = Fake()
    d, clk, lines = make(fake)
    steps = demo.plan()
    d.run(steps)
    seq = fake.seq()
    modes = [x[1] for x in seq if x[0] == "mode"]
    assert modes == ["tenantguard", "off", "industry", "industry", "tenantguard", "tenantguard", "tenantguard",
                     "tenantguard"]                                  # last = cleanup
    attacks = [(x[1], x[2]) for x in seq if x[0] == "attack"]
    assert attacks == [("flood", 30), ("flood", 15), ("blackbox_bomb", 30), ("blackbox_bomb", 75),
                       ("faulty_server", 20), ("honest_spike", 20)]
    # every attack is launched right after its step stopped the previous ones and set the mode
    for i, x in enumerate(seq):
        if x[0] == "attack":
            assert seq[i - 1][0] == "mode" and seq[i - 2] == ("stop",)
    assert seq[-2:] == [("stop",), ("mode", "tenantguard")]
    assert clk.t == pytest.approx(sum(s.seconds for s in steps))     # timed exactly as the script
    assert fake.calls[0].url.path == "/lab/calm/start"
    mode_req = next(r for r in fake.calls if r.url.path == "/admin/mode")
    assert mode_req.headers[demo.H_CONTROL_TOKEN] == "tok"
    assert any(l.lstrip().startswith("[5/9] TenantGuard") for l in lines)


def test_attack_is_preceded_by_stop_and_mode_in_each_step():
    fake = Fake()
    d, _, _ = make(fake)
    d.run(demo.plan()[:3])
    seq = [x for x in fake.seq()]
    # step 2 (OFF + flood): stop, mode off, attack flood
    i = seq.index(("mode", "off"))
    assert seq[i - 1] == ("stop",) and seq[i + 1] == ("attack", "flood", 30)


def test_unshipped_attack_falls_back_to_whale():
    fake = Fake(unavailable={"blackbox_bomb"})
    d, _, lines = make(fake)
    d.run([s for s in demo.plan() if s.key in ("calm", "tenantguard")])
    # the preflight already knows: no failed launch, straight to the fallback
    assert [x[1] for x in fake.seq() if x[0] == "attack"] == ["whale"]
    assert any("uses whale" in l for l in lines)


def test_failed_attack_does_not_abort_the_demo():
    fake = Fake(attack_status=500)
    d, clk, lines = make(fake)
    steps = demo.plan()
    d.run(steps)
    assert clk.t == pytest.approx(sum(s.seconds for s in steps))
    assert any("HTTP 500" in l and "step continues" in l for l in lines)


def test_cleanup_runs_when_interrupted():
    fake = Fake()
    d, clk, _ = make(fake)

    def boom(_s):
        raise KeyboardInterrupt
    d.sleep = boom
    with pytest.raises(KeyboardInterrupt):
        d.run(demo.plan())
    assert fake.seq()[-2:] == [("stop",), ("mode", "tenantguard")]
    assert [x[1] for x in fake.seq() if x[0] == "mode"][:2] == ["tenantguard", "tenantguard"]


def test_dry_run_sends_nothing_and_prints_every_call():
    fake = Fake()
    d, clk, lines = make(fake, dry_run=True)
    d.run(demo.plan())
    assert fake.calls == [] and clk.t == 0
    assert any("POST http://gw/admin/mode {'mode': 'off'}" in l for l in lines)
    assert any("POST http://lab/lab/attack" in l and "'faulty_server'" in l for l in lines)


def test_live_readout_shows_numbers_and_each_incident_once():
    fake = Fake()
    d, _, lines = make(fake, tick_s=5)
    d.run([s for s in demo.plan() if s.key == "tenantguard"])     # 75 s at 5 s ticks = 15 readouts
    text = "\n".join(lines)
    assert "fairness 0.93" in text and "util 41%" in text
    assert "worst victim A p95 220 ms" in text and "K level throttle x0.50" in text
    assert "client victim p95 max 240 ms" in text and "K ok/s 12 429/s 88" in text
    assert "“K is noisy”" in text
    assert sum(1 for l in lines if l.strip().startswith("> named")) == 1


def test_scorecard_table_uses_latest_run_per_scenario_and_mode():
    res = [
        {"scenario": "whale", "mode": "tenantguard", "victim_p99_ms": {"baseline": 20, "attack": 35, "after": 21}},
        {"scenario": "whale", "mode": "off", "victim_p99_ms": {"baseline": 20, "attack": 900, "after": 25}},
        {"scenario": "whale", "mode": "off", "victim_p99_ms": {"baseline": 20, "attack": 111, "after": 25}},
    ]
    fake = Fake(results=res)
    d, _, lines = make(fake)
    d.scorecard()
    row = next(l for l in lines if l.strip().startswith("whale"))
    assert row.split() == ["whale", "900", "-", "35"]


def test_main_only_and_speed_in_dry_run(capsys):
    assert demo.main(["--dry-run", "--only", "faulty", "--speed", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "[1/2] Calm (8 s)" in out and "[2/2] Faulty server (10 s)" in out
    from tenantguard_common.config import load_settings
    svc = load_settings()["services"]
    assert svc["gateway"] in out and svc["lab"] in out
    assert "100.112.232.8" not in open(demo.__file__, encoding="utf-8").read()
    with pytest.raises(SystemExit):
        demo.main(["--dry-run", "--only", "nope"])
