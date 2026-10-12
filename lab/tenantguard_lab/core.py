"""The Lab: engine + attack manager + scenario runs. Shared by the HTTP API and the CLI."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from tenantguard_common.contracts import H_CONTROL_TOKEN, AttackReq, AttackStatus, LabStatus, RunReq, RunResult

from .attacks import RUNNERS, WAVE2, AttackCtx
from .engine import Engine
from .isolation import ProcessAttack
from .scenario import Marks, ScenarioDriver, Timeline, compute_result, run_timeline
from .settings import LabConfig
from .stats import Recorder, summarize

log = logging.getLogger("tenantguard_lab.core")

KEEP_FINISHED_ATTACKS = 20


class LabError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class AttackEntry:
    status: AttackStatus
    ctx: AttackCtx
    task: Optional[asyncio.Task] = None
    proc: Optional[ProcessAttack] = None


@dataclass
class RunInfo:
    run_id: str
    req: RunReq
    attacker: Optional[str]
    state: str = "running"            # running | done | failed | cancelled
    phase: str = "starting"
    started_at: float = field(default_factory=time.time)
    planned_end: float = 0.0
    result: Optional[RunResult] = None
    error: Optional[str] = None

    def view(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "state": self.state, "phase": self.phase, "scenario": self.req.scenario,
                "mode": self.req.mode, "attacker": self.attacker, "started_at": self.started_at,
                "planned_end": self.planned_end, "error": self.error,
                "result": self.result.model_dump() if self.result else None}


class Lab(ScenarioDriver):
    def __init__(self, cfg: LabConfig, recorder: Optional[Recorder] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.cfg = cfg
        self.recorder = recorder or Recorder()
        self.engine = Engine(cfg, self.recorder, transport=transport)
        self.attacks: Dict[str, AttackEntry] = {}
        self.runs: Dict[str, RunInfo] = {}
        self.current_run: Optional[RunInfo] = None
        self._run_task: Optional[asyncio.Task] = None
        self._admin = httpx.AsyncClient(timeout=5.0, transport=transport)

    # ---------- calm ----------
    def start_calm(self) -> bool:
        return self.engine.start_calm()

    async def stop_calm(self) -> bool:
        return await self.engine.stop_calm()

    # ---------- attacks ----------
    def _validate(self, req: AttackReq) -> None:
        if req.attack in WAVE2:
            raise LabError(501, f"attack {req.attack!r} ships in Wave 2")
        if req.attack not in RUNNERS:  # pragma: no cover - contract and RUNNERS cover every name
            raise LabError(501, f"attack {req.attack!r} not implemented")
        if not req.tenant:
            raise LabError(422, f"attack {req.attack!r} needs a tenant")
        if req.tenant not in self.cfg.personas:
            raise LabError(404, f"unknown tenant {req.tenant!r}")

    async def start_attack(self, req: AttackReq) -> str:  # ScenarioDriver
        return (await self.launch(req)).attack_id

    @property
    def isolated(self) -> bool:
        """Attacks run in their own process unless client.attack_isolation is 'inline' (tests, mocks)."""
        return self.cfg.client.get("attack_isolation", "process") == "process" and self.engine.transport is None

    async def launch(self, req: AttackReq) -> AttackStatus:
        self._validate(req)
        attack_id = f"atk-{uuid.uuid4().hex[:8]}"
        ctx = AttackCtx(attack_id=attack_id, engine=self.engine, req=req, stop=asyncio.Event())
        proc: Optional[ProcessAttack] = None
        if self.isolated:
            proc = ProcessAttack(self.cfg, ctx, self.recorder)
            try:
                await proc.start()            # returns once the attack traffic is really running
            except RuntimeError as e:
                raise LabError(500, str(e)) from e
        now = time.time()
        status = AttackStatus(attack_id=attack_id, attack=req.attack, tenant=req.tenant, server=req.server,
                              intensity=req.intensity, state="running", started_at=now, ends_at=now + req.duration_s)
        entry = AttackEntry(status=status, ctx=ctx, proc=proc)
        self.engine.start_monitor()
        self.attacks[attack_id] = entry
        entry.task = asyncio.ensure_future(self._supervise(entry))
        log.info("attack %s started%s: %s on %s x%.2f for %.0fs", attack_id, f" (pid {proc.proc.pid})" if proc else "",
                 req.attack, req.tenant, req.intensity, req.duration_s)
        self._prune_attacks()
        return status

    async def _supervise(self, entry: AttackEntry) -> None:
        ctx = entry.ctx
        if entry.proc is not None:
            try:
                if await entry.proc.pump_until(ctx.stop, ctx.req.duration_s) and entry.status.state == "running":
                    entry.status.state = "done"
            finally:
                ctx.stop.set()
                await entry.proc.stop()
                self._finish(entry)
            return
        runner = asyncio.ensure_future(RUNNERS[ctx.req.attack](ctx))
        try:
            await asyncio.wait_for(asyncio.shield(ctx.stop.wait()), timeout=ctx.req.duration_s)
        except asyncio.TimeoutError:
            if entry.status.state == "running":
                entry.status.state = "done"
        finally:
            ctx.stop.set()
            try:
                await asyncio.wait_for(runner, timeout=float(self.cfg.client["timeout_s"]) + 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                runner.cancel()
            except Exception:  # noqa: BLE001 - keep the lab alive, log the attack failure
                log.exception("attack %s failed", ctx.attack_id)
            await self.engine.close_pool(ctx.tenant, ctx.attack_id)
            self._finish(entry)

    def _finish(self, entry: AttackEntry) -> None:
        if entry.status.state == "running":
            entry.status.state = "done"
        entry.status.ends_at = min(entry.status.ends_at, time.time())
        log.info("attack %s %s: %s", entry.ctx.attack_id, entry.status.state, entry.ctx.counts)

    async def stop_attack(self, attack_id: str) -> AttackStatus:
        entry = self.attacks.get(attack_id)
        if entry is None:
            raise LabError(404, f"unknown attack {attack_id!r}")
        if entry.status.state == "running":
            entry.status.state = "stopped"
            entry.ctx.stop.set()
        if entry.task is not None:
            await asyncio.gather(entry.task, return_exceptions=True)
        return entry.status

    async def stop_all_attacks(self) -> List[AttackStatus]:
        running = [a for a, e in self.attacks.items() if e.status.state == "running"]
        return [await self.stop_attack(a) for a in running]

    def _prune_attacks(self) -> None:
        finished = [a for a, e in self.attacks.items() if e.status.state != "running"]
        for a in finished[:-KEEP_FINISHED_ATTACKS]:
            del self.attacks[a]

    def attack_counts(self, attack_id: str) -> Dict[str, int]:
        return dict(self.attacks[attack_id].ctx.counts)

    # ---------- status / metrics ----------
    def status(self) -> LabStatus:
        run = self.current_run.run_id if self.current_run and self.current_run.state == "running" else None
        return LabStatus(attacks=[e.status for e in self.attacks.values()], calm_running=self.engine.calm_running,
                         run=run)

    def metrics(self, window_s: float = 10.0) -> Dict[str, Any]:
        """Client-side per-tenant stats over the last window_s seconds (all traffic and split by source)."""
        now = time.time()
        inflight = self.engine.calm_inflight()
        tenants: Dict[str, Any] = {}
        for tid, p in self.cfg.personas.items():
            samples = self.recorder.window(tid, now - window_s, now + 1)
            calm = [s for s in samples if s.src == "calm"]
            attack = [s for s in samples if s.src == "attack"]
            tenants[tid] = {
                "tenant": tid, "name": p.tenant.name, "tier": p.tenant.tier,
                "target_rps": round(p.rps * self.cfg.rate_scale, 3), "sla_p95_ms": p.sla_p95_ms,
                **summarize(samples).as_dict(window_s),
                "calm": summarize(calm).as_dict(window_s),
                "attack": summarize(attack).as_dict(window_s) if attack else None,
                "inflight": inflight.get(tid, 0),
                "client_dropped_total": self.recorder.dropped.get(tid, 0),
                "totals": dict(self.recorder.totals.get(tid, {})),
            }
        return {"ts": now, "window_s": window_s, "gateway": self.cfg.gateway_url,
                "calm_running": self.engine.calm_running, "rate_scale": self.cfg.rate_scale,
                "client_loop_lag_ms": self.engine.loop_lag_ms(window_s),
                "attacks": {a: {**e.status.model_dump(), "counts": dict(e.ctx.counts)} for a, e in self.attacks.items()},
                "tenants": tenants}

    # ---------- ScenarioDriver ----------
    async def sleep(self, s: float) -> None:
        await asyncio.sleep(s)

    async def set_mode(self, mode: str) -> None:
        url = f"{self.cfg.gateway_url}/admin/mode"
        try:
            r = await self._admin.post(url, json={"mode": mode},
                                       headers={H_CONTROL_TOKEN: os.environ.get("TENANTGUARD_CONTROL_TOKEN", "dev")})
        except httpx.HTTPError as e:
            raise LabError(502, f"could not set gateway mode at {url}: {e!r}") from e
        if r.status_code >= 300:
            raise LabError(502, f"gateway refused mode {mode!r}: HTTP {r.status_code} {r.text[:200]}")

    async def ensure_calm(self) -> bool:
        was = self.engine.calm_running
        self.engine.start_calm()
        return was

    async def restore_calm(self, was_running: bool) -> None:
        if not was_running:
            await self.engine.stop_calm()

    def on_phase(self, name: str) -> None:
        if self.current_run is not None:
            self.current_run.phase = name
            log.info("run %s phase -> %s", self.current_run.run_id, name)

    # ---------- runs ----------
    def timeline(self, req: RunReq) -> Timeline:
        return Timeline(warmup_s=float(self.cfg.run.get("warmup_s", 3)), calm_s=req.calm_s, attack_s=req.attack_s,
                        cold_start_s=float(self.cfg.run.get("cold_start_s", 0)))

    def start_run(self, req: RunReq) -> RunInfo:
        if self.current_run is not None and self.current_run.state == "running":
            raise LabError(409, f"run {self.current_run.run_id} is in progress")
        attacker = req.tenant or self.cfg.default_attacker
        self._validate(AttackReq(attack=req.scenario, tenant=attacker, server=req.server, intensity=req.intensity))
        tl = self.timeline(req)
        info = RunInfo(run_id=f"run-{uuid.uuid4().hex[:8]}", req=req, attacker=attacker)
        info.planned_end = info.started_at + tl.end + (0.0 if self.engine.calm_running else tl.cold_start_s)
        self.runs[info.run_id] = info
        self.current_run = info
        self._run_task = asyncio.ensure_future(self._run(info, tl))
        return info

    async def _run(self, info: RunInfo, tl: Timeline) -> None:
        try:
            marks = await run_timeline(self, info.req, info.attacker, tl)
            info.phase = "computing"
            ttd = await self.time_to_detect(marks, info.attacker)
            result = compute_result(
                self.recorder, self.cfg.personas.keys(), info.req, info.attacker, marks, info.run_id,
                recover_ratio=float(self.cfg.run.get("recover_ratio", 1.5)),
                recover_hold_s=float(self.cfg.run.get("recover_hold_s", 5)),
                bucket_s=float(self.cfg.run.get("bucket_s", 1.0)), time_to_detect_s=ttd,
                victim_kinds=self.cfg.run.get("victim_kinds") or None)
            info.result = self.write_result(result, marks)
            info.state = "done"
            info.phase = "done"
            log.info("run %s done -> %s", info.run_id, info.result.file)
        except asyncio.CancelledError:
            info.state = "cancelled"
            raise
        except LabError as e:
            info.state, info.error = "failed", e.detail
            log.error("run %s failed: %s", info.run_id, e.detail)
        except Exception as e:  # noqa: BLE001
            info.state, info.error = "failed", repr(e)
            log.exception("run %s failed", info.run_id)

    async def wait_run(self) -> None:
        if self._run_task is not None:
            await asyncio.gather(self._run_task, return_exceptions=True)

    async def cancel_run(self) -> Optional[RunInfo]:
        if self._run_task is None or self._run_task.done():
            return None
        self._run_task.cancel()
        await asyncio.gather(self._run_task, return_exceptions=True)
        return self.current_run

    async def time_to_detect(self, m: Marks, attacker: Optional[str]) -> Optional[float]:
        """Seconds from attack start to the brain's first incident event in [attack_start, end] that names
        the attacker (or no tenant). None if the brain is unreachable or saw nothing."""
        url = f"{self.cfg.brain_url}/api/incidents"
        try:
            params = {"limit": 5000, **({"tenant": attacker} if attacker else {})}
            r = await self._admin.get(url, params=params, timeout=2.0)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        events = body.get("incidents", body.get("events", [])) if isinstance(body, dict) else body
        times = []
        for ev in events if isinstance(events, list) else []:
            if not isinstance(ev, dict):
                continue
            ts = ev.get("ts")
            if not isinstance(ts, (int, float)) or not (m.attack_start <= ts <= m.end):
                continue
            if ev.get("tenant") in (None, attacker):
                times.append(ts)
        return round(min(times) - m.attack_start, 3) if times else None

    def write_result(self, result: RunResult, m: Marks) -> RunResult:
        self.cfg.results_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(m.t0))
        name = f"{stamp}-{result.scenario}-{result.mode}.json"
        path = self.cfg.results_dir / name
        result = result.model_copy(update={"file": f"lab/results/{name}"})
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    def list_results(self, scenario: Optional[str] = None, mode: Optional[str] = None) -> List[RunResult]:
        """Every RunResult in the results dir, newest first (by started_at), optionally filtered."""
        out = []
        for p in self.cfg.results_dir.glob("*.json"):
            try:
                r = RunResult.model_validate_json(p.read_text(encoding="utf-8"))
            except ValueError:
                log.warning("skipping unreadable result %s", p)
                continue
            if (scenario is None or r.scenario == scenario) and (mode is None or r.mode == mode):
                out.append(r)
        out.sort(key=lambda r: r.started_at, reverse=True)
        return out

    async def aclose(self) -> None:
        await self.cancel_run()
        await self.stop_all_attacks()
        await self.engine.aclose()
        await self._admin.aclose()
