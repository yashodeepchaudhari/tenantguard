"""Scorecard runner against a mocked lab + brain (httpx.MockTransport) with a fake clock."""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import httpx
import pytest

from tenantguard_common.contracts import PhaseP99, RunResult
from tenantguard_lab import scorecard as sc
from tenantguard_lab.settings import load_config

T0 = 1_800_000_000.0
P99 = {  # victim p99 (baseline, attack, after) per mode: OFF suffers, TenantGuard recovers
    "off": (20.0, 400.0, 30.0), "industry": (20.0, 150.0, 25.0), "tenantguard": (20.0, 40.0, 21.0)}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeLab:
    """POST /lab/run -> running; GET /lab/run/{id} -> done after `polls` polls. 501 for unregistered."""

    def __init__(self, clock: Clock, unregistered=("blackbox_bomb",), polls: int = 2, busy_once=False,
                 never_done=False, brain_events: Optional[List[dict]] = None, brain_up=True, results=None):
        self.clock, self.unregistered, self.polls = clock, set(unregistered), polls
        self.busy_once, self.never_done, self.brain_up = busy_once, never_done, brain_up
        self.brain_events = brain_events if brain_events is not None else []
        self.runs: Dict[str, dict] = {}
        self.posted: List[dict] = []
        self.deleted = 0
        self.status_calls = 0
        self.results = results or []

    def view(self, rid):
        r = self.runs[rid]
        return {k: v for k, v in r.items() if not k.startswith("_")}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "brain":
            if not self.brain_up:
                raise httpx.ConnectError("down", request=req)
            return httpx.Response(200, json=self.brain_events)
        if path == "/lab/run" and req.method == "POST":
            body = json.loads(req.content)
            if body["scenario"] in self.unregistered:
                return httpx.Response(501, json={"detail": f"attack {body['scenario']!r} is not included in this build"})
            if self.busy_once:
                self.busy_once = False
                return httpx.Response(409, json={"detail": "run run-x is in progress"})
            self.posted.append(body)
            rid = f"run-{len(self.posted)}"
            now = self.clock()
            self.runs[rid] = {"run_id": rid, "state": "running", "phase": "calm", "scenario": body["scenario"],
                              "mode": body["mode"], "attacker": body.get("tenant") or "K", "started_at": now,
                              "planned_end": now + 80, "error": None, "marks": None, "result": None, "_polls": 0}
            return httpx.Response(202, json=self.view(rid))
        if path.startswith("/lab/run/"):
            rid = path.rsplit("/", 1)[1]
            r = self.runs[rid]
            r["_polls"] += 1
            if r["_polls"] >= self.polls and not self.never_done:
                n = len(self.posted)
                b, a, af = P99[r["mode"]]
                st = r["started_at"]
                r.update(state="done", phase="done",
                         marks={"t0": st, "baseline_start": st + 3, "attack_start": st + 23, "attack_end": st + 63,
                                "end": st + 83},
                         result=RunResult(run_id=rid, scenario=r["scenario"], mode=r["mode"], attacker=r["attacker"],
                                          started_at=st, victim_p99_ms=PhaseP99(baseline=b, attack=a + n, after=af),
                                          time_to_detect_s=2.0 + n if r["mode"] == "tenantguard" else None,
                                          time_to_recover_s=5.0 if r["mode"] != "off" else None,
                                          attacker_429=10 * n if r["mode"] != "off" else 0,
                                          file=f"lab/results/{rid}.json").model_dump())
            return httpx.Response(200, json=self.view(rid))
        if path == "/lab/run" and req.method == "DELETE":
            self.deleted += 1
            return httpx.Response(200, json={})
        if path == "/lab/status":
            self.status_calls += 1
            return httpx.Response(200, json={"attacks": [], "calm_running": True,
                                             "run": None if self.status_calls >= 2 else "run-x"})
        if path == "/lab/results":
            return httpx.Response(200, json=self.results)
        return httpx.Response(404)


def runner(fake: FakeLab, clock: Clock, lines: List[str], **kw) -> sc.Scorecard:
    return sc.Scorecard("http://lab", "http://brain", client=httpx.Client(transport=httpx.MockTransport(fake)),
                        sleep=clock.sleep, clock=clock, out=lines.append, **kw)


def ev(ts, stage, tenant=None, level=None, iid="inc-1-K-1", suspects=None, report=None):
    return {"incident_id": iid, "stage": stage, "ts": ts, "tenant": tenant, "level": level,
            "suspects": suspects or [], "report": report, "type": "incident"}


# ---------------------------------------------------------------- order
def test_plan_interleaves_modes_inside_each_repetition():
    assert sc.plan(["whale", "flood"], ["off", "tenantguard"], 2) == [
        ("whale", "off", 0), ("whale", "tenantguard", 0), ("whale", "off", 1), ("whale", "tenantguard", 1),
        ("flood", "off", 0), ("flood", "tenantguard", 0), ("flood", "off", 1), ("flood", "tenantguard", 1)]


# ---------------------------------------------------------------- full run
def test_runs_every_cell_skips_unregistered_and_aggregates():
    clock, lines = Clock(), []
    fake = FakeLab(clock)
    s = runner(fake, clock, lines).run(["whale", "blackbox_bomb"], sc.MODES_ALL, runs=3, intensity=2.0)
    # only whale ran: 3 reps x 3 modes, modes interleaved, all with the same knobs
    assert [(p["scenario"], p["mode"]) for p in fake.posted] == [("whale", m) for _ in range(3) for m in sc.MODES_ALL]
    assert all(p["intensity"] == 2.0 and p["calm_s"] == 20 and p["attack_s"] == 40 for p in fake.posted)
    assert [c.mode for c in s.cells] == sc.MODES_ALL and {c.scenario for c in s.cells} == {"whale"}
    assert s.skipped and "blackbox_bomb: not registered in this lab" in s.skipped[0] and "not included in this build" in s.skipped[0]
    off, ind, tg = s.cells
    # run numbers n: off = 1,4,7 ; industry = 2,5,8 ; tenantguard = 3,6,9 (attack p99 = base + n)
    assert off.runs == 3 and off.victim_p99_ms_median.attack == 404.0 and off.victim_p99_ms_mean.attack == 404.0
    assert tg.victim_p99_ms_median.attack == 46.0 and tg.victim_p99_ms_median.baseline == 20.0
    assert tg.time_to_detect_s_median == 8.0 and tg.detected_runs == 3 and off.detected_runs == 0
    assert off.time_to_recover_s_median is None and ind.time_to_recover_s_median == 5.0
    assert ind.attacker_429_median == 50.0 and off.attacker_429_median == 0.0
    assert len(tg.run_files) == 3


def test_brain_alignment_names_culprit_and_finds_innocents():
    clock, lines = Clock(), []
    # one brain log reused for every run: events placed at the first run's attack window (T0 + 23 .. T0 + 83)
    events = [
        ev(T0 + 25, "detected", tenant="K", level="warn"),
        ev(T0 + 27, "level_change", tenant="K", level="throttle"),
        ev(T0 + 30, "level_change", tenant="A", level="throttle", iid="inc-2-A-1"),     # an innocent acted on
        ev(T0 + 5, "level_change", tenant="C", level="quarantine", iid="inc-0-C-1"),    # before the attack: ignored
        ev(T0 + 500, "level_change", tenant="D", level="throttle", iid="inc-9-D-1"),    # after the run: ignored
    ]
    fake = FakeLab(clock, brain_events=events)
    s = runner(fake, clock, lines, cooldown_s=0).run(["whale"], ["tenantguard"], runs=1)
    (c,) = s.cells
    assert c.brain_checked_runs == 1 and c.culprit_named_runs == 1
    assert c.innocents_penalised == ["A"] and c.innocents_penalised_runs == 1


def test_check_brain_uses_named_report_and_window():
    marks = {"attack_start": 100.0, "end": 200.0}
    events = [
        ev(150, "named", iid="srv-1", suspects=[{"tenant": "K", "confidence": 0.8}]),
        ev(160, "named", iid="srv-2", report={"culprits": [{"tenant": "E", "confidence": 0.7}]}),
        ev(99, "named", iid="srv-0", suspects=[{"tenant": "B", "confidence": 0.9}]),     # outside
        "garbage", ev("x", "named"),
    ]
    c = sc.check_brain(events, "K", marks)
    assert c.reachable and c.culprit_named and c.first_named_after_s == 50.0
    assert c.wrongly_named == ["E"] and c.innocents_penalised == []
    assert sc.check_brain(None, "K", marks).reachable is False
    assert sc.check_brain([], "K", marks).culprit_named is False


def test_brain_down_still_produces_a_summary():
    clock, lines = Clock(), []
    fake = FakeLab(clock, brain_up=False)
    s = runner(fake, clock, lines).run(["whale"], ["off", "tenantguard"], runs=1)
    assert all(c.runs == 1 and c.brain_checked_runs == 0 for c in s.cells)
    assert any("brain unreachable" in l for l in lines)


# ---------------------------------------------------------------- robustness
def test_busy_lab_is_waited_for_then_retried():
    clock, lines = Clock(), []
    fake = FakeLab(clock, busy_once=True)
    s = runner(fake, clock, lines).run(["whale"], ["off"], runs=1)
    assert s.cells[0].runs == 1 and fake.status_calls >= 2
    assert any("another run is in progress" in l for l in lines)


def test_overrunning_run_is_cancelled_and_counted_failed():
    clock, lines = Clock(), []
    fake = FakeLab(clock, never_done=True)
    s = runner(fake, clock, lines, grace_s=10).run(["whale"], ["off"], runs=1)
    assert fake.deleted == 1 and s.cells[0].runs == 0 and s.cells[0].failed_runs == 1
    assert any("overran" in l for l in lines)


def test_unreachable_lab_stops_cleanly():
    def down(req):
        raise httpx.ConnectError("refused", request=req)
    clock, lines = Clock(), []
    s = sc.Scorecard("http://lab", "http://brain", client=httpx.Client(transport=httpx.MockTransport(down)),
                     sleep=clock.sleep, clock=clock, out=lines.append).run(["whale"], sc.MODES_ALL, runs=2)
    assert all(c.runs == 0 for c in s.cells) and any("lab unreachable" in x for x in s.skipped)


# ---------------------------------------------------------------- aggregate-only + output
def _res(scenario, mode, attack, started):
    return RunResult(run_id=f"r{started}", scenario=scenario, mode=mode, started_at=started,
                     victim_p99_ms=PhaseP99(baseline=10, attack=attack, after=12), file=f"lab/results/{started}.json")


def test_aggregate_only_takes_newest_runs_per_cell():
    clock, lines = Clock(), []
    newest_first = [_res("whale", "off", 900, 5), _res("whale", "off", 500, 4), _res("whale", "off", 100, 3),
                    _res("whale", "tenantguard", 30, 2)]
    fake = FakeLab(clock, results=[r.model_dump() for r in newest_first])
    s = runner(fake, clock, lines).aggregate_only(["whale"], ["off", "tenantguard", "industry"], runs=2)
    off, tg, ind = s.cells
    assert s.aggregate_only and off.runs == 2 and off.victim_p99_ms_median.attack == 700.0
    assert tg.runs == 1 and ind.runs == 0 and ind.victim_p99_ms_median is None
    assert fake.posted == []                                  # nothing was run


def test_main_writes_summary_file_and_table(tmp_path, capsys):
    cfg = load_config()
    cfg.lab_url, cfg.brain_url, cfg.results_dir = "http://lab", "http://brain", tmp_path
    clock = Clock()
    fake = FakeLab(clock)
    client = httpx.Client(transport=httpx.MockTransport(fake))
    # no real sleeping: the runner's default sleep is replaced through a short cooldown and instant polls
    fake.polls = 1
    rc = sc.main(["--scenarios", "whale", "--modes", "off,tenantguard", "--runs", "1", "--cooldown-s", "0"],
                 cfg=cfg, client=client)
    assert rc == 0
    files = list(tmp_path.glob("summary-*.json"))
    assert len(files) == 1
    data = sc.CrashSummary.model_validate_json(files[0].read_text(encoding="utf-8"))
    assert [c.mode for c in data.cells] == ["off", "tenantguard"] and data.file == str(files[0])
    out = capsys.readouterr().out
    assert "whale" in out and "p99 attack" in out and "wrote" in out


def test_main_stdout_mode_is_pure_json(capsys):
    cfg = load_config()
    cfg.lab_url, cfg.brain_url = "http://lab", "http://brain"
    fake = FakeLab(Clock(), polls=1)
    rc = sc.main(["--scenarios", "whale", "--modes", "off", "--runs", "1", "--cooldown-s", "0", "--out", "-"],
                 cfg=cfg, client=httpx.Client(transport=httpx.MockTransport(fake)))
    cap = capsys.readouterr()
    assert rc == 0
    assert sc.CrashSummary.model_validate_json(cap.out).cells[0].runs == 1   # progress went to stderr
    assert "scorecard:" in cap.err


@pytest.mark.parametrize("argv", [["--modes", "off,turbo"], ["--runs", "0"], ["--scenarios", "nope"]])
def test_main_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit):
        sc.main(argv, cfg=load_config(), client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))


def test_cli_hook_dispatches_scorecard(monkeypatch):
    import tenantguard_lab.__main__ as cli
    seen = {}
    monkeypatch.setattr(sc, "main", lambda argv: seen.setdefault("argv", argv) and 0 or 0)
    assert cli.main(["scorecard", "--aggregate-only"]) == 0
    assert seen["argv"] == ["--aggregate-only"]


def test_summary_models_come_from_contracts():
    from tenantguard_common import contracts
    assert sc.CrashSummary is contracts.CrashSummary and sc.CellStats is contracts.CellStats
