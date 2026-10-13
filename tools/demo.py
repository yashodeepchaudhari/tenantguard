"""One-command TenantGuard demo driver (the 4-minute script in docs/FINAL-PLAN.md section 10).

    python tools/demo.py --dry-run                 # print the plan and every HTTP call; touches nothing
    python tools/demo.py                           # run it, timed (default target: the Docker host)
    python tools/demo.py --step                    # wait for Enter before each step (live narration)
    python tools/demo.py --only tenantguard        # run a single step (after calm)

Targets come from env TENANTGUARD_GATEWAY_URL, TENANTGUARD_BRAIN_URL, TENANTGUARD_LAB_URL, else tenantguard.yaml
`services`. The faulty-replica fallback finds the replica's address in the brain's GET /api/pool (replicas are
created on demand, so there is no fixed list); TENANTGUARD_WORKER_URLS ("tg-media-1=http://..") overrides it. TENANTGUARD_CONTROL_TOKEN (default "dev"),
TENANTGUARD_ADMIN_TOKEN (worker fault fallback, default "dev").
Only HTTP APIs are used: gateway POST /admin/mode, lab /lab/calm/start, /lab/attack, /lab/attacks, /lab/metrics,
/lab/results, brain /api/state, /api/incidents, /api/pool, and (fallback only) replica /admin/fault.
Before the show a preflight asks the lab which attacks it can run (without starting any); a step whose attack is
not available is announced with a banner and skipped. On exit (normal, error or Ctrl-C) every attack and fault is
stopped and the mode is set back to tenantguard.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import httpx

from tenantguard_common.config import load_settings
from tenantguard_common.contracts import H_CONTROL_TOKEN

PROBE = "__probe__"                 # an id no lab knows: the lab answers 501 (not shipped) or 4xx, never starts it
FAULT = {"latency_ms": 150.0, "cpu_burn_pct": 30.0}   # worker /admin/fault fallback for faulty_server


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


def plan(attacker: str = "K", bomb: str = "blackbox_bomb", faulty: str = "tg-media-1", spiker: str = "J") -> List[Step]:
    """The demo, step by step (FINAL-PLAN section 10; coordinator order for T14)."""
    label = "black-box bomb" if bomb == "blackbox_bomb" else "whale"
    what = ("cheap-looking requests that trigger hidden background work" if bomb == "blackbox_bomb"
            else "a few very heavy requests")
    return [
        Step("calm", "Calm", 15, "12 tenants in 5 projects, calm traffic. Every tile green, one warm replica per "
             "project, fairness near 1.", mode="tenantguard"),
        Step("off", "OFF + flood", 30, f"Protection OFF. {attacker} floods its project: every tenant sharing its "
             "replicas turns red. This is the noisy-neighbour problem.", mode="off",
             attacks=[Attack("flood", attacker)], attacker=attacker),
        Step("industry_flood", "Industry + same flood", 15, f"Industry-standard limits: rate, cost, fair queue. "
             f"The same flood from {attacker} is now refused with 429s; the victims stay green.", mode="industry",
             attacks=[Attack("flood", attacker)], attacker=attacker),
        Step("industry", f"Industry + {label}", 30, f"Now {attacker} sends {what} ... the rate limiter barely "
             "sees it, and the victims turn red. This is where rate limiting stops.",
             mode="industry", attacks=[Attack(bomb, attacker, fallback="whale")], attacker=attacker),
        Step("tenantguard", "TenantGuard + same attack", 75, f"Same attack, TenantGuard on: {attacker}'s share of "
             "its project is measured against its neighbours' slowdown, and the ladder steps in (warn, throttle, "
             "deprioritize, quarantine) while the neighbours recover; it relaxes by itself once they are safe.",
             mode="tenantguard", attacks=[Attack(bomb, attacker, fallback="whale")], attacker=attacker,
             watch_incidents=True),
        Step("faulty", "Faulty replica", 20, f"Replica {faulty} turns slow by itself: its tenants slow down, but "
             "nobody's share of the project grew, so TenantGuard throttles no tenant.", mode="tenantguard",
             attacks=[Attack("faulty_server", server=faulty)],
             watch_incidents=True),
        Step("spike", "Honest spike", 20, f"{spiker}, an Enterprise tenant, has its sale day: a genuine burst. Its "
             "project borrows idle cores from the pool and scales out, with no throttling.", mode="tenantguard",
             attacks=[Attack("honest_spike", spiker)],
             attacker=spiker, watch_incidents=True),
        Step("scorecard", "Scorecard", 25, "OFF vs Industry vs TenantGuard, measured victim p99 per scenario.",
             scorecard=True),
        Step("close", "Close", 10, "Every tenant gets its fair share. Every slowdown gets a name."),
    ]


class Demo:
    def __init__(self, gateway: str, brain: str, lab: str, token: str, client: Optional[httpx.Client] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 out: Callable[[str], None] = print, tick_s: float = 5.0, dry_run: bool = False,
                 wait_enter: Optional[Callable[[str], None]] = None, workers: Optional[Dict[str, str]] = None,
                 admin_token: str = "dev"):
        self.gw, self.brain, self.lab = gateway.rstrip("/"), brain.rstrip("/"), lab.rstrip("/")
        self.token, self.admin_token = token, admin_token
        self.workers = {k: v.rstrip("/") for k, v in (workers or {}).items()}
        self.http = client or httpx.Client(timeout=5.0)
        self.sleep, self.clock, self.out = sleep, clock, out
        self.tick_s, self.dry_run, self.wait_enter = tick_s, dry_run, wait_enter
        self.started: List[str] = []           # attack ids we launched (stopped on exit)
        self.faults: List[str] = []            # worker URLs we put a fault on (cleared on exit)
        self.available: Dict[str, bool] = {}   # attack -> the lab can run it (preflight)
        self.fault_urls: Dict[str, str] = {}   # server -> reachable worker URL (faulty_server fallback)
        self.skipped: Dict[str, str] = {}      # step key -> why it is skipped
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

    # ----- preflight: what can this lab (and this network) actually run? -----
    def probe(self, attack: str) -> bool:
        """Ask the lab whether it can run `attack` without starting it: an unknown tenant/server makes a shipped
        attack fail validation (4xx) while an unshipped one answers 501. Unknown (lab down, dry run) = assume yes."""
        if attack in self.available:
            return self.available[attack]
        r = self.call("POST", f"{self.lab}/lab/attack",
                      json={"attack": attack, "tenant": PROBE, "server": PROBE, "intensity": 1.0, "duration_s": 1})
        ok = r is None or r.status_code != 501
        if r is not None and 200 <= r.status_code < 300:      # a lab that started it anyway: stop it now
            aid = r.json().get("attack_id")
            if aid:
                self.call("DELETE", f"{self.lab}/lab/attack/{aid}")
        self.available[attack] = ok
        return ok

    def replica_url(self, replica: str) -> Optional[str]:
        """The address of a replica, from the brain's pool view (None if the brain does not know it)."""
        r = self.call("GET", f"{self.brain}/api/pool")
        if r is None or r.status_code != 200:
            return None
        for p in r.json().get("projects", []):
            for rep in p.get("replicas", []):
                if rep.get("id") == replica and rep.get("url"):
                    return rep["url"].rstrip("/")
        return None

    def worker_reachable(self, server: Optional[str]) -> Optional[str]:
        if not server or self.dry_run:
            return None
        url = self.workers.get(server) or self.replica_url(server)
        if not url:
            return None
        r = self.call("GET", f"{url}/healthz", timeout=1.0)
        return url if r is not None and r.status_code == 200 else None

    def preflight(self, steps: List["Step"]) -> None:
        self.out("  preflight:")
        for st in steps:
            for a in st.attacks:
                if self.probe(a.attack):
                    continue
                if a.fallback and self.probe(a.fallback):
                    self.out(f"    {a.attack}: not on this lab, step '{st.key}' uses {a.fallback}")
                    continue
                if a.attack == "faulty_server":
                    url = self.worker_reachable(a.server)
                    if url:
                        self.fault_urls[a.server] = url
                        self.out(f"    {a.attack}: not on this lab, step '{st.key}' injects the fault directly "
                                 f"on {a.server} ({url}/admin/fault)")
                        continue
                    why = (f"{a.attack} is not available on this lab and replica {a.server} is not reachable "
                           f"from here")
                else:
                    why = f"{a.attack} is not available on this lab"
                self.skipped[st.key] = why
                self.out(f"    [NOT AVAILABLE] step '{st.key}' will be skipped: {why}")
        if not self.skipped:
            self.out("    every step can run")

    def launch(self, a: Attack, seconds: float) -> None:
        name = a.attack
        if self.available.get(name) is False:
            if a.attack == "faulty_server" and a.server in self.fault_urls:
                return self.inject_fault(a.server, seconds)
            if a.fallback and self.available.get(a.fallback, True):
                name = a.fallback
        r = self.call("POST", f"{self.lab}/lab/attack", json=a.body(seconds, name))
        if r is not None and r.status_code == 501 and a.fallback and name != a.fallback:
            self.out(f"      {a.attack} not available on this lab (501): using {a.fallback}")
            r = self.call("POST", f"{self.lab}/lab/attack", json=a.body(seconds, a.fallback))
        if r is None:
            return
        if r.status_code == 200:
            self.started.append(r.json().get("attack_id", ""))
        else:
            self.out(f"      ! attack {a.attack}: HTTP {r.status_code} {r.text[:120]} (step continues)")

    def inject_fault(self, server: str, seconds: float) -> None:
        url = self.fault_urls[server]
        r = self.call("POST", f"{url}/admin/fault", json={**FAULT, "duration_s": seconds},
                      headers={"X-Admin-Token": self.admin_token})
        if r is not None and r.status_code == 200:
            self.faults.append(url)
            self.out(f"      fault injected on {server}: +{FAULT['latency_ms']:.0f} ms, {FAULT['cpu_burn_pct']:.0f}% "
                     f"CPU burn for {seconds:.0f} s")
        elif r is not None:
            self.out(f"      ! fault on {server}: HTTP {r.status_code} (step continues)")

    def stop_all(self) -> bool:
        """Stop every lab attack. Retries once, then stops our own attacks one by one; says loudly what is left."""
        for _ in range(2):
            r = self.call("DELETE", f"{self.lab}/lab/attacks")
            if self.dry_run or (r is not None and r.status_code < 300):
                self.started.clear()
                return True
        left = []
        for aid in self.started:
            r = self.call("DELETE", f"{self.lab}/lab/attack/{aid}")
            if r is None or r.status_code >= 300 and r.status_code != 404:
                left.append(aid)
        self.started = left
        if left:
            self.out(f"      ! could not stop attacks {', '.join(left)}: they end on their own when their "
                     f"duration runs out")
        return not left

    def clear_faults(self) -> None:
        left = []
        for url in self.faults:
            r = self.call("DELETE", f"{url}/admin/fault", headers={"X-Admin-Token": self.admin_token})
            if r is None or r.status_code >= 300:
                left.append(url)
        if left:
            self.out(f"      ! could not clear the fault on {', '.join(left)}: it auto-clears after its duration")
        self.faults = left

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
            self.preflight(steps)
            total = sum(s.seconds for s in steps if s.key not in self.skipped)
            self.out(f"  {len(steps)} steps, {total:.0f} s")
            for i, st in enumerate(steps, 1):
                if st.key in self.skipped:
                    self.out(f"\n[{i}/{len(steps)}] [NOT AVAILABLE] {st.title}: skipped ({self.skipped[st.key]})")
                    continue
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
            self.cleanup()

    def cleanup(self) -> None:
        """Stop attacks, clear faults, mode back to tenantguard. A second Ctrl-C cannot abort it half-way."""
        self.out("\ncleanup: stopping attacks, clearing faults, mode back to tenantguard")
        for task in (self.stop_all, self.clear_faults, lambda: self.set_mode("tenantguard")):
            for _ in range(2):
                try:
                    task()
                    break
                except KeyboardInterrupt:
                    self.out("      (Ctrl-C ignored: cleanup is still running)")

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
    p.add_argument("--faulty-server", default="tg-media-1", help="a replica id, as shown on the Pool page")
    p.add_argument("--spiker", default="J")
    p.add_argument("--tick", type=float, default=5.0, help="seconds between live readouts")
    p.add_argument("--speed", type=float, default=1.0, help="scale every step duration (e.g. 0.5 for rehearsal)")
    a = p.parse_args(argv)
    env = os.environ.get
    settings = load_settings()
    svc = settings.get("services") or {}
    steps = plan(a.attacker, a.bomb, a.faulty_server, a.spiker)
    if a.only:
        keys = [s.key for s in steps]
        if a.only not in keys:
            p.error(f"--only must be one of {keys}")
        steps = [s for s in steps if s.key in ("calm", a.only)]
    for s in steps:
        s.seconds *= a.speed
    demo = Demo(env("TENANTGUARD_GATEWAY_URL", svc.get("gateway", "http://127.0.0.1:8000")),
                env("TENANTGUARD_BRAIN_URL", svc.get("brain", "http://127.0.0.1:8200")),
                env("TENANTGUARD_LAB_URL", svc.get("lab", "http://127.0.0.1:8300")),
                env("TENANTGUARD_CONTROL_TOKEN", "dev"), tick_s=a.tick, dry_run=a.dry_run,
                wait_enter=(lambda msg: input(msg)) if a.step else None,
                workers=dict(kv.split("=", 1) for kv in env("TENANTGUARD_WORKER_URLS", "").split(",") if "=" in kv),
                admin_token=env("TENANTGUARD_ADMIN_TOKEN", "dev"))
    try:
        demo.run(steps)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
