"""Scorecard runner: the lab's existing scenarios in every mode, summarised from MEASURED runs.

    python -m tenantguard_lab scorecard --scenarios whale,flood --modes off,industry,tenantguard --runs 3
    python -m tenantguard_lab scorecard --aggregate-only          # summarise existing lab/results, run nothing

It only drives the lab's HTTP API (POST /lab/run, GET /lab/run/{id}, DELETE /lab/run, GET /lab/status,
GET /lab/results) and reads the brain's GET /api/incidents. It sends no traffic and starts no attack itself.
Runs are sequential; modes are interleaved per repetition (off, industry, tenantguard, off, ...) so slow drift
on the host does not favour one mode. A scenario the lab has not registered (HTTP 501) is skipped and listed.

Output: lab/results/summary-<ts>.json, one cell per scenario x mode: median / mean victim p99 per phase,
time to detect / recover, attacker 429 / 503, and, from brain incidents inside each run's marks, whether the
attacker was named and which innocent tenants were acted on.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import httpx
from pydantic import BaseModel, Field

from tenantguard_common.contracts import AttackName, CellStats, CrashSummary, PhaseP99, RunReq, RunResult

from .settings import LabConfig, load_config

ACTED_LEVELS = {"throttle", "deprioritize", "quarantine", "blocked"}
MODES_ALL = ["off", "industry", "tenantguard"]


# ---- per-run brain check (internal; the summary models are CellStats / CrashSummary in contracts) ----
class BrainCheck(BaseModel):
    """What the brain did inside one run's [attack_start, end] window."""
    reachable: bool
    culprit_named: bool = False             # attacker = ladder incident tenant or a named suspect
    first_named_after_s: Optional[float] = None
    wrongly_named: List[str] = Field(default_factory=list)       # non-attackers named as suspects / ladder tenants
    innocents_penalised: List[str] = Field(default_factory=list)  # non-attackers moved to throttle or above


# ---- pure helpers ----
def median(xs: Sequence[float]) -> Optional[float]:
    return round(statistics.median(xs), 3) if xs else None


def mean(xs: Sequence[float]) -> Optional[float]:
    return round(statistics.fmean(xs), 3) if xs else None


def phase_stat(results: Sequence[RunResult], fn: Callable[[Sequence[float]], Optional[float]]) -> Optional[PhaseP99]:
    if not results:
        return None
    return PhaseP99(baseline=fn([r.victim_p99_ms.baseline for r in results]),
                    attack=fn([r.victim_p99_ms.attack for r in results]),
                    after=fn([r.victim_p99_ms.after for r in results]))


def check_brain(events: Optional[list], attacker: Optional[str], marks: Optional[dict]) -> BrainCheck:
    """Align brain IncidentEvents (dicts) with one run's marks."""
    if events is None or not marks:
        return BrainCheck(reachable=events is not None)
    a, b = float(marks["attack_start"]), float(marks["end"])
    named, wrong, acted = [], set(), set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = ev.get("ts")
        if not isinstance(ts, (int, float)) or not (a <= ts <= b):
            continue
        iid = str(ev.get("incident_id", ""))
        ladder = ev.get("tenant") if iid.startswith("inc-") else None
        suspects = [s.get("tenant") for s in ev.get("suspects") or [] if isinstance(s, dict)]
        if ev.get("stage") == "named":
            suspects += [c.get("tenant") for c in (ev.get("report") or {}).get("culprits") or [] if isinstance(c, dict)]
        who = {t for t in suspects + [ladder] if t}
        if attacker in who:
            named.append(ts)
        wrong |= {t for t in who if t != attacker}
        if ev.get("level") in ACTED_LEVELS and ev.get("tenant") and ev.get("tenant") != attacker:
            acted.add(ev["tenant"])
    return BrainCheck(reachable=True, culprit_named=bool(named),
                      first_named_after_s=round(min(named) - a, 3) if named else None,
                      wrongly_named=sorted(wrong), innocents_penalised=sorted(acted))


def summarize_cell(scenario: str, mode: str, results: List[RunResult], checks: List[BrainCheck],
                   failed: int = 0) -> CellStats:
    ttd = [r.time_to_detect_s for r in results if r.time_to_detect_s is not None]
    ttr = [r.time_to_recover_s for r in results if r.time_to_recover_s is not None]
    checked = [c for c in checks if c.reachable]
    innocents = sorted({t for c in checked for t in c.innocents_penalised})
    return CellStats(
        scenario=scenario, mode=mode, runs=len(results), failed_runs=failed,
        victim_p99_ms_median=phase_stat(results, median), victim_p99_ms_mean=phase_stat(results, mean),
        time_to_detect_s_median=median(ttd), detected_runs=len(ttd),
        time_to_recover_s_median=median(ttr), recovered_runs=len(ttr),
        attacker_429_median=median([r.attacker_429 for r in results]) or 0.0,
        attacker_503_median=median([r.attacker_503 for r in results]) or 0.0,
        brain_checked_runs=len(checked), culprit_named_runs=sum(c.culprit_named for c in checked),
        innocents_penalised=innocents, innocents_penalised_runs=sum(bool(c.innocents_penalised) for c in checked),
        run_files=[r.file for r in results if r.file])


def plan(scenarios: Sequence[str], modes: Sequence[str], runs: int) -> List[Tuple[str, str, int]]:
    """Run order: per scenario, repetitions outermost and modes interleaved inside each repetition."""
    return [(s, m, i) for s in scenarios for i in range(runs) for m in modes]


# ---- the runner (HTTP only) ----
class Scorecard:
    def __init__(self, lab_url: str, brain_url: str, client: Optional[httpx.Client] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 out: Callable[[str], None] = print, poll_s: float = 2.0, cooldown_s: float = 10.0,
                 grace_s: float = 120.0):
        self.lab, self.brain = lab_url.rstrip("/"), brain_url.rstrip("/")
        self.http = client or httpx.Client(timeout=15.0)
        self.sleep, self.clock, self.out = sleep, clock, out
        self.poll_s, self.cooldown_s, self.grace_s = poll_s, cooldown_s, grace_s

    def _wait_idle(self, timeout_s: float) -> bool:
        """Wait until the lab has no run in progress (409 on start)."""
        end = self.clock() + timeout_s
        while self.clock() < end:
            try:
                st = self.http.get(f"{self.lab}/lab/status").json()
            except (httpx.HTTPError, ValueError):
                st = {}
            if not st.get("run"):
                return True
            self.sleep(self.poll_s)
        return False

    def run_one(self, req: RunReq) -> Tuple[str, Optional[dict]]:
        """('done' | 'failed' | 'unregistered' | 'unreachable', run view)."""
        for attempt in range(2):
            try:
                r = self.http.post(f"{self.lab}/lab/run", json=req.model_dump())
            except httpx.HTTPError as e:
                self.out(f"    ! lab unreachable: {type(e).__name__}")
                return "unreachable", None
            if r.status_code == 409 and attempt == 0:
                self.out("    another run is in progress; waiting for it")
                if not self._wait_idle(600):
                    return "failed", None
                continue
            break
        if r.status_code == 501:
            return "unregistered", {"detail": _detail(r)}
        if r.status_code >= 300:
            self.out(f"    ! run refused: HTTP {r.status_code} {_detail(r)}")
            return "failed", None
        info = r.json()
        deadline = self.clock() + max(0.0, info.get("planned_end", 0) - info.get("started_at", 0)) + self.grace_s
        while info.get("state") == "running":
            if self.clock() > deadline:
                self.out(f"    ! run {info['run_id']} overran; cancelling it")
                try:
                    self.http.delete(f"{self.lab}/lab/run")
                except httpx.HTTPError:
                    pass
                return "failed", info
            self.sleep(self.poll_s)
            try:
                info = self.http.get(f"{self.lab}/lab/run/{info['run_id']}").json()
            except (httpx.HTTPError, ValueError):
                continue
        return ("done" if info.get("state") == "done" and info.get("result") else "failed"), info

    def brain_events(self) -> Optional[list]:
        try:
            r = self.http.get(f"{self.brain}/api/incidents", params={"limit": 5000}, timeout=5.0)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        return body if isinstance(body, list) else None

    def run(self, scenarios: Sequence[str], modes: Sequence[str], runs: int, attacker: Optional[str] = None,
            intensity: float = 1.0, calm_s: float = 20, attack_s: float = 40) -> CrashSummary:
        results: Dict[Tuple[str, str], List[RunResult]] = {}
        checks: Dict[Tuple[str, str], List[BrainCheck]] = {}
        failed: Dict[Tuple[str, str], int] = {}
        skipped: Dict[str, str] = {}
        order = plan(scenarios, modes, runs)
        for n, (scen, mode, i) in enumerate(order, 1):
            if scen in skipped:
                continue
            self.out(f"[{n}/{len(order)}] {scen} x {mode} (run {i + 1}/{runs})")
            req = RunReq(scenario=scen, mode=mode, tenant=attacker, intensity=intensity, calm_s=calm_s,
                         attack_s=attack_s)
            state, info = self.run_one(req)
            if state == "unregistered":
                skipped[scen] = f"{scen}: not registered in this lab ({info.get('detail', 'HTTP 501')})"
                self.out(f"    skipped: {skipped[scen]}")
                continue
            if state == "unreachable":
                skipped.setdefault("lab", f"lab unreachable at {self.lab}: stopped after {n - 1} runs")
                break
            key = (scen, mode)
            if state != "done":
                failed[key] = failed.get(key, 0) + 1
                self.out(f"    ! failed: {(info or {}).get('error') or state}")
            else:
                res = RunResult(**info["result"])
                results.setdefault(key, []).append(res)
                check = check_brain(self.brain_events(), info.get("attacker"), info.get("marks"))
                checks.setdefault(key, []).append(check)
                self.out(f"    victim p99 {res.victim_p99_ms.baseline:.0f} -> {res.victim_p99_ms.attack:.0f} -> "
                         f"{res.victim_p99_ms.after:.0f} ms, attacker 429 {res.attacker_429}"
                         + (f", named {check.culprit_named}, innocents {check.innocents_penalised or '-'}"
                            if check.reachable else ", brain unreachable"))
            if n < len(order):
                self.sleep(self.cooldown_s)      # let brain incidents close before the next run
        cells = [summarize_cell(s, m, results.get((s, m), []), checks.get((s, m), []), failed.get((s, m), 0))
                 for s in scenarios for m in modes if s not in skipped]
        return CrashSummary(created_at=self.clock(), lab_url=self.lab, brain_url=self.brain,
                            scenarios=list(scenarios), modes=list(modes), runs_per_cell=runs, attacker=attacker,
                            intensity=intensity, calm_s=calm_s, attack_s=attack_s, cells=cells,
                            skipped=list(skipped.values()))

    def aggregate_only(self, scenarios: Sequence[str], modes: Sequence[str], runs: int) -> CrashSummary:
        """Summarise the newest `runs` existing results per cell (GET /lab/results); no brain alignment."""
        r = self.http.get(f"{self.lab}/lab/results")
        r.raise_for_status()
        all_res = [RunResult(**x) for x in r.json()]          # newest first
        cells = []
        for s in scenarios:
            for m in modes:
                picked = [x for x in all_res if x.scenario == s and x.mode == m][:runs]
                cells.append(summarize_cell(s, m, picked, []))
        return CrashSummary(created_at=self.clock(), lab_url=self.lab, brain_url=self.brain,
                            scenarios=list(scenarios), modes=list(modes), runs_per_cell=runs,
                            aggregate_only=True, cells=cells)


def _detail(r: httpx.Response) -> str:
    try:
        return str(r.json().get("detail", ""))[:200]
    except ValueError:
        return r.text[:200]


def table(s: CrashSummary) -> str:
    rows = [f"{'scenario':<14}{'mode':<13}{'runs':>5}{'p99 base':>10}{'p99 attack':>12}{'p99 after':>11}"
            f"{'detect s':>10}{'recover s':>11}{'atk 429':>9}{'named':>7}{'innocents':>11}"]

    def f(x):
        return "-" if x is None else f"{x:.0f}" if x >= 10 else f"{x:.1f}"
    for c in s.cells:
        p = c.victim_p99_ms_median
        rows.append(f"{c.scenario:<14}{c.mode:<13}{c.runs:>5}{f(p.baseline if p else None):>10}"
                    f"{f(p.attack if p else None):>12}{f(p.after if p else None):>11}"
                    f"{f(c.time_to_detect_s_median):>10}{f(c.time_to_recover_s_median):>11}"
                    f"{c.attacker_429_median:>9.0f}"
                    f"{(f'{c.culprit_named_runs}/{c.brain_checked_runs}' if c.brain_checked_runs else '-'):>7}"
                    f"{(','.join(c.innocents_penalised) or 'none') if c.brain_checked_runs else '-':>11}")
    rows += [f"skipped: {x}" for x in s.skipped]
    return "\n".join(rows)


def write(summary: CrashSummary, results_dir: Path, out: Optional[str] = None) -> CrashSummary:
    if out == "-":
        sys.stdout.write(summary.model_dump_json(indent=2) + "\n")
        return summary
    path = Path(out) if out else results_dir / f"summary-{time.strftime('%Y%m%d-%H%M%S', time.localtime(summary.created_at))}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = summary.model_copy(update={"file": str(path)})
    path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    return summary


def main(argv: Optional[List[str]] = None, cfg: Optional[LabConfig] = None, client: Optional[httpx.Client] = None) -> int:
    p = argparse.ArgumentParser(prog="tenantguard_lab scorecard",
                                description="run the lab's scenarios in every mode and summarise measured results")
    p.add_argument("--scenarios", default="whale,flood", help="comma list (only scenarios the lab registers run)")
    p.add_argument("--modes", default=",".join(MODES_ALL))
    p.add_argument("--runs", type=int, default=3, help="runs per scenario x mode")
    p.add_argument("--tenant", default=None, help="attacker (default: the lab's default_attacker)")
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument("--calm-s", type=float, default=20.0)
    p.add_argument("--attack-s", type=float, default=40.0)
    p.add_argument("--cooldown-s", type=float, default=10.0, help="pause between runs")
    p.add_argument("--aggregate-only", action="store_true", help="summarise existing results; run nothing")
    p.add_argument("--out", default=None, help="summary path, or '-' for stdout (default lab/results/summary-<ts>.json)")
    a = p.parse_args(argv)
    cfg = cfg or load_config()
    scenarios = [x.strip() for x in a.scenarios.split(",") if x.strip()]
    modes = [x.strip() for x in a.modes.split(",") if x.strip()]
    bad = [m for m in modes if m not in MODES_ALL]
    if bad or a.runs < 1:
        p.error(f"modes must be in {MODES_ALL} and --runs >= 1")
    unknown = [s for s in scenarios if s not in AttackName.__args__]  # type: ignore[attr-defined]
    if unknown:
        p.error(f"unknown scenarios {unknown}")
    log = (lambda s: print(s, file=sys.stderr, flush=True)) if a.out == "-" else (lambda s: print(s, flush=True))
    sc = Scorecard(cfg.lab_url, cfg.brain_url, client=client, out=log, cooldown_s=a.cooldown_s)
    if a.aggregate_only:
        summary = sc.aggregate_only(scenarios, modes, a.runs)
    else:
        log(f"scorecard: {len(plan(scenarios, modes, a.runs))} runs of ~{a.calm_s * 2 + a.attack_s:.0f} s against "
            f"{cfg.lab_url} (brain {cfg.brain_url})")
        summary = sc.run(scenarios, modes, a.runs, a.tenant, a.intensity, a.calm_s, a.attack_s)
    summary = write(summary, cfg.results_dir, a.out)
    log("\n" + table(summary))
    if summary.file:
        log(f"\nwrote {summary.file}")
    return 0 if any(c.runs for c in summary.cells) else 1
