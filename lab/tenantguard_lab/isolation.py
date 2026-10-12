"""Run one attack in its own OS process.

Attack traffic can saturate a Python event loop (httpx costs ~3 ms CPU per request on the dev laptop).
If it shared the loop with the victims' calm traffic, the lab itself would inflate victim latency and
the results would measure the lab, not the platform. So each attack gets its own process (own loop,
own GIL); it streams its samples and counters back over a multiprocessing queue, and the parent folds
them into the shared Recorder and AttackCtx.counts."""
from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import queue as queue_mod
import time
from typing import Any, Dict, List, Optional, Tuple

from tenantguard_common.contracts import AttackReq

from .attacks import RUNNERS, AttackCtx
from .settings import LabConfig
from .stats import Recorder, Sample

log = logging.getLogger("tenantguard_lab.isolation")

FLUSH_S = 0.25
_CTX = mp.get_context("spawn")      # Windows-safe; same behaviour on Linux


class _BufferRecorder(Recorder):
    """Child-side recorder: buffers samples for shipping to the parent."""

    def __init__(self) -> None:
        super().__init__()
        self.buf: List[Tuple[str, float, float, int, str, str]] = []

    def add(self, tenant: str, sample: Sample) -> None:
        self.buf.append((tenant, sample.ts, sample.latency_ms, sample.status, sample.src, sample.kind))


def _child(cfg: LabConfig, req_json: str, attack_id: str, q, stop_ev, ready_ev) -> None:
    from .engine import Engine   # imported in the child after spawn

    async def main() -> None:
        rec = _BufferRecorder()
        engine = Engine(cfg, rec)
        ctx = AttackCtx(attack_id=attack_id, engine=engine, req=AttackReq.model_validate_json(req_json),
                        stop=asyncio.Event())
        task = asyncio.ensure_future(RUNNERS[ctx.req.attack](ctx))
        ready_ev.set()

        def flush(final: bool = False) -> None:
            batch, rec.buf = rec.buf, []
            q.put(("done" if final else "batch", batch, dict(ctx.counts)))

        try:
            while not stop_ev.is_set() and not task.done():
                await asyncio.sleep(FLUSH_S)
                flush()
            ctx.stop.set()
            try:
                await asyncio.wait_for(task, timeout=float(cfg.client["timeout_s"]) + 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        finally:
            await engine.aclose()
            flush(final=True)

    asyncio.run(main())


class ProcessAttack:
    def __init__(self, cfg: LabConfig, ctx: AttackCtx, recorder: Recorder):
        self.cfg, self.ctx, self.recorder = cfg, ctx, recorder
        self.q = _CTX.Queue()
        self.stop_ev = _CTX.Event()
        self.ready_ev = _CTX.Event()
        self.proc = _CTX.Process(target=_child, name=f"lab-{ctx.attack_id}", daemon=True,
                                 args=(cfg, ctx.req.model_dump_json(), ctx.attack_id, self.q, self.stop_ev, self.ready_ev))
        self.finished = False

    async def start(self, timeout_s: float = 30.0) -> None:
        """Spawn and wait until the child's traffic is actually running."""
        self.proc.start()
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self.ready_ev.wait, timeout_s)
        if not ok:
            self.proc.kill()
            raise RuntimeError(f"attack process {self.ctx.attack_id} did not start in {timeout_s}s")

    def drain(self) -> None:
        while True:
            try:
                kind, batch, counts = self.q.get_nowait()
            except queue_mod.Empty:
                return
            for tenant, ts, lat, status, src, kind in batch:
                self.recorder.add(tenant, Sample(ts, lat, status, src, kind))
            self.ctx.counts.update(counts)
            if kind == "done":
                self.finished = True

    async def pump_until(self, stop: asyncio.Event, duration_s: float) -> bool:
        """Fold child output in until `stop` is set or duration elapses. Returns True if it timed out."""
        end = time.monotonic() + duration_s
        while not stop.is_set():
            self.drain()
            if not self.proc.is_alive():
                return False
            left = end - time.monotonic()
            if left <= 0:
                return True
            try:
                await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=min(FLUSH_S, left))
            except asyncio.TimeoutError:
                pass
        return False

    async def stop(self) -> None:
        self.stop_ev.set()
        deadline = time.monotonic() + float(self.cfg.client["timeout_s"]) + 5
        while not self.finished and time.monotonic() < deadline and self.proc.is_alive():
            self.drain()
            await asyncio.sleep(0.05)
        self.drain()
        # After its final flush the child still has to shut down its interpreter and queue feeder thread.
        await asyncio.get_running_loop().run_in_executor(None, self.proc.join, 5.0)
        if self.proc.is_alive():
            log.warning("attack process %s did not exit; killing", self.ctx.attack_id)
            self.proc.kill()
        self.proc.join(timeout=2)
