"""One-command TenantGuard demo driver (the 4-minute script in docs/FINAL-PLAN.md section 10).

    python tools/demo.py --dry-run                 # print the plan and every HTTP call; touches nothing
    python tools/demo.py                           # run it, timed (default target: the Docker host)
    python tools/demo.py --step                    # wait for Enter before each step (live narration)
    python tools/demo.py --only tenantguard        # run a single step (after calm)

Targets come from env: TENANTGUARD_GATEWAY_URL, TENANTGUARD_BRAIN_URL, TENANTGUARD_LAB_URL
(default http://100.112.232.8:{8000,8200,8300}), TENANTGUARD_CONTROL_TOKEN (default "dev").
Only HTTP APIs are used: gateway POST /admin/mode, lab /lab/calm/start, /lab/attack, /lab/attacks, /lab/metrics,
/lab/results, brain /api/state, /api/incidents. On exit (normal, error or Ctrl-C) every attack is stopped and the
mode is set back to tenantguard.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import httpx

HOST = "100.112.232.8"
H_CONTROL_TOKEN = "X-TenantGuard-Control-Token"     # same value as tenantguard_common.contracts.H_CONTROL_TOKEN


@dataclass
class Attack:
    attack: str
    tenant: Optional[str] = None
    server: Optional[str] = None
    intensity: float = 1.0
    fallback: Optional[str] = None          # used if the lab answers 501 (attack not shipped yet)

    def body(self, duration_s: float, attack: Optional[str] = None) -> dict:
        b = {"attack": attack or self.attack, "intensity": self.intensity, "duration_s": duration_s}
        if self.tenant:
            b["tenant"] = self.tenant
        if self.server:
            b["server"] = self.server
        return b


@dataclass
class Step:
    key: str
    title: str
    seconds: float
    narration: str
    mode: Optional[str] = None
    attacks: List[Attack] = field(default_factory=list)
    attacker: Optional[str] = None           # tenant to show separately from the victims
    watch_incidents: bool = False
    scorecard: bool = False


def plan(attacker: str = "K", bomb: str = "blackbox_bomb", faulty: str = "w4", spiker: str = "J") -> List[Step]:
    """The demo, step by step (FINAL-PLAN section 10; coordinator order for T14)."""
    label = "black-box bomb" if bomb == "blackbox_bomb" else "whale"
    what = ("cheap-looking requests that trigger hidden background work" if bomb == "blackbox_bomb"
            else "a few very heavy requests")
    return [
        Step("calm", "Calm", 15, "12 tenants, calm traffic. Every tile green, fairness near 1.", mode="tenantguard"),
        Step("off", "OFF + flood", 30, f"Protection OFF. {attacker} floods the fleet: everyone sharing its servers "
             "turns red. This is the noisy-neighbour problem.", mode="off",
             attacks=[Attack("flood", attacker)], attacker=attacker),
        Step("industry_flood", "Industry + same flood", 15, f"Industry-standard limits: rate, cost, fair queue. "
             f"The same flood from {attacker} is now refused with 429s; the victims stay green.", mode="industry",
             attacks=[Attack("flood", attacker)], attacker=attacker),
        Step("industry", f"Industry + {label}", 30, f"Now {attacker} sends {what} ... the rate limiter barely "
             "sees it, and the victims turn red. This is where rate limiting stops.",
             mode="industry", attacks=[Attack(bomb, attacker, fallback="whale")], attacker=attacker),
        Step("tenantguard", "TenantGuard + same attack", 75, "Same attack, TenantGuard on: victims rescued in about "
             "a second, the barcode names the culprit with a confidence, the proof follows it, then the verdict.",
             mode="tenantguard", attacks=[Attack(bomb, attacker, fallback="whale")], attacker=attacker,
             watch_incidents=True),
        Step("faulty", "Faulty server", 20, f"Server {faulty} turns faulty by itself: TenantGuard drains it and "
             "blames no tenant (host fault).", mode="tenantguard", attacks=[Attack("faulty_server", server=faulty)],
             watch_incidents=True),
        Step("spike", "Honest spike", 20, f"{spiker}, an Enterprise tenant, has its sale day: a genuine burst. It "
             "gets room to grow, with no throttling.", mode="tenantguard", attacks=[Attack("honest_spike", spiker)],
             attacker=spiker, watch_incidents=True),
        Step("scorecard", "Scorecard", 25, "OFF vs Industry vs TenantGuard, measured victim p99 per scenario.",
             scorecard=True),
        Step("close", "Close", 10, "Every tenant gets its fair share. Every slowdown gets a name."),
    ]


class Demo:
    def __init__(self, gateway: str, brain: str, lab: str, token: str, client: Optional[httpx.Client] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 out: Callable[[str], None] = print, tick_s: float = 5.0, dry_run: bool = False,
                 wait_enter: Optional[Callable[[str], None]] = None):
        self.gw, self.brain, self.lab = gateway.rstrip("/"), brain.rstrip("/"), lab.rstrip("/")
        self.token = token
        self.http = client or httpx.Client(timeout=5.0)
        self.sleep, self.clock, self.out = sleep, clock, out
        self.tick_s, self.dry_run, self.wait_enter = tick_s, dry_run, wait_enter
        self.started: List[str] = []           # attack ids we launched (stopped on exit)
        self.seen_incident_keys: set = set()

    # ----- HTTP (every call goes through here, so --dry-run can print instead) -----
    def call(self, method: str, url: str, **kw) -> Optional[httpx.Response]:
        if self.dry_run:
            body = kw.get("json")
            self.out(f"      {method} {url}" + (f" {body}" if body is not None else ""))
            return None
        try:
            return self.http.request(method, url, **kw)
        except httpx.HTTPError as e:
            self.out(f"      ! {method} {url} failed: {type(e).__name__}")
            return None

    def set_mode(self, mode: str) -> None:
        r = self.call("POST", f"{self.gw}/admin/mode", json={"mode": mode}, headers={H_CONTROL_TOKEN: self.token})
        if r is not None and r.status_code != 200:
            self.out(f"      ! mode {mode}: HTTP {r.status_code}")

    def launch(self, a: Attack, seconds: float) -> None:
        r = self.call("POST", f"{self.lab}/lab/attack", json=a.body(seconds))
        if r is not None and r.status_code == 501 and a.fallback:
            self.out(f"      {a.attack} not available on this lab (501): using {a.fallback}")
            r = self.call("POST", f"{self.lab}/lab/attack", json=a.body(seconds, a.fallback))
        if r is None:
            return
        if r.status_code == 200:
            self.started.append(r.json().get("attack_id", ""))
        else:
            self.out(f"      ! attack {a.attack}: HTTP {r.status_code} {r.text[:120]} (step continues)")

    def stop_all(self) -> None:
        self.call("DELETE", f"{self.lab}/lab/attacks")
        self.started.clear()

    # ----- live numbers -----
    def live(self, step: Step) -> None:
        if self.dry_run:
            return
        parts = []
        r = self.call("GET", f"{self.brain}/api/state")
        if r is not None and r.status_code == 200:
            s = r.json()
            victims = [t for t in s.get("tenants", []) if t.get("tenant") != step.attacker]
            worst = max(victims, key=lambda t: t.get("p95_ms", 0), default=None)
            parts.append(f"mode {s.get('mode')} | fairness {s.get('fairness', 0):.2f} | util "
                         f"{100 * s.get('utilization', 0):.0f}%")
            if worst:
                parts.append(f"worst victim {worst['tenant']} p95 {worst.get('p95_ms', 0):.0f} ms")
            if step.attacker:
                att = next((t for t in s.get("tenants", []) if t.get("tenant") == step.attacker), None)
                if att:
                    parts.append(f"{step.attacker} level {att.get('level')} x{att.get('factor', 1):.2f}")
            sent = s.get("status_sentence")
        else:
            sent = None
            parts.append("brain: unavailable")
        r = self.call("GET", f"{self.lab}/lab/metrics", params={"window_s": 5})
        if r is not None and r.status_code == 200:
            ts = r.json().get("tenants", {})
            vic = [v["calm"] for k, v in ts.items()
                   if k != step.attacker and (v.get("calm") or {}).get("count", 0) > 0]
            if vic:
                parts.append(f"client victim p95 max {max(v.get('p95_ms', 0) for v in vic):.0f} ms")
            if step.attacker and step.attacker in ts:
                a = ts[step.attacker]
                parts.append(f"{step.attacker} ok/s {a.get('ok_per_s', 0):.0f} 429/s {a.get('r429_per_s', 0):.0f}")
        self.out("      " + " | ".join(parts))
        if sent:
            self.out(f"      “{sent}”")
        if step.watch_incidents:
            self.incidents()

    def incidents(self) -> None:
        r = self.call("GET", f"{self.brain}/api/incidents", params={"limit": 20})
        if r is None or r.status_code != 200:
            return
        for ev in r.json():
            key = (ev.get("incident_id"), ev.get("stage"), ev.get("ts"))
            if key in self.seen_incident_keys:
                continue
            self.seen_incident_keys.add(key)
            detail = (ev.get("detail") or "")[:150]
            self.out(f"      > {ev.get('stage')}: {detail}")

    def scorecard(self) -> None:
        r = self.call("GET", f"{self.lab}/lab/results")
        if r is None:
            return
        if r.status_code != 200 or not r.json():
            self.out("      (no measured runs yet: run `POST /lab/run` per scenario x mode first)")
            return
        latest: Dict[tuple, dict] = {}
        for res in r.json():                                  # newest first
            latest.setdefault((res["scenario"], res["mode"]), res)
        modes = ["off", "industry", "tenantguard"]
        self.out("      victim p99 during attack (ms)   " + "  ".join(f"{m:>11}" for m in modes))
        for scen in sorted({s for s, _ in latest}):
            cells = []
            for m in modes:
                res = latest.get((scen, m))
                cells.append(f"{res['victim_p99_ms']['attack']:>11.0f}" if res else f"{'-':>11}")
            self.out(f"      {scen:<32}" + "  ".join(cells))

    # ----- the show -----
    def run(self, steps: List[Step]) -> None:
        try:
            self.out("TenantGuard demo" + ("  [dry run: nothing is sent]" if self.dry_run else ""))
            self.out(f"  gateway {self.gw}  brain {self.brain}  lab {self.lab}")
            self.call("POST", f"{self.lab}/lab/calm/start")
            self.stop_all()
            total = sum(s.seconds for s in steps)
            self.out(f"  {len(steps)} steps, {total:.0f} s")
            for i, st in enumerate(steps, 1):
                if self.wait_enter and not self.dry_run:
                    self.wait_enter(f"\n[Enter] step {i}: {st.title} ")
                self.out(f"\n[{i}/{len(steps)}] {st.title} ({st.seconds:.0f} s): {st.narration}")
                if st.attacks or st.mode:
                    self.stop_all()
                if st.mode:
                    self.set_mode(st.mode)
                for a in st.attacks:
                    self.launch(a, st.seconds)
                if st.scorecard:
                    self.scorecard()
                self._hold(st)
        finally:
            self.out("\ncleanup: stopping attacks, mode back to tenantguard")
            self.stop_all()
            self.set_mode("tenantguard")

    def _hold(self, st: Step) -> None:
        if self.dry_run:
            return
        end = self.clock() + st.seconds
        while True:
            left = end - self.clock()
            if left <= 0:
                break
            self.sleep(min(self.tick_s, left))
            if st.attacks or st.mode:
                self.live(st)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="TenantGuard demo driver")
    p.add_argument("--dry-run", action="store_true", help="print the plan and HTTP calls; send nothing")
    p.add_argument("--step", action="store_true", help="wait for Enter before each step")
    p.add_argument("--only", help="run only this step key (calm is always run first)")
    p.add_argument("--attacker", default="K")
    p.add_argument("--bomb", default="blackbox_bomb", choices=["blackbox_bomb", "whale"])
    p.add_argument("--faulty-server", default="w4")
    p.add_argument("--spiker", default="J")
    p.add_argument("--tick", type=float, default=5.0, help="seconds between live readouts")
    p.add_argument("--speed", type=float, default=1.0, help="scale every step duration (e.g. 0.5 for rehearsal)")
    a = p.parse_args(argv)
    env = os.environ.get
    steps = plan(a.attacker, a.bomb, a.faulty_server, a.spiker)
    if a.only:
        keys = [s.key for s in steps]
        if a.only not in keys:
            p.error(f"--only must be one of {keys}")
        steps = [s for s in steps if s.key in ("calm", a.only)]
    for s in steps:
        s.seconds *= a.speed
    demo = Demo(env("TENANTGUARD_GATEWAY_URL", f"http://{HOST}:8000"), env("TENANTGUARD_BRAIN_URL", f"http://{HOST}:8200"),
                env("TENANTGUARD_LAB_URL", f"http://{HOST}:8300"), env("TENANTGUARD_CONTROL_TOKEN", "dev"),
                tick_s=a.tick, dry_run=a.dry_run, wait_enter=(lambda msg: input(msg)) if a.step else None)
    try:
        demo.run(steps)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
