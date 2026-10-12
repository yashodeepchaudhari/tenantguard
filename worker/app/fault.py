"""Simulated faulty host: added latency on every probe/API call plus unattributed CPU burn."""
from __future__ import annotations

import threading
import time
from typing import Optional

from pydantic import BaseModel, Field


class FaultReq(BaseModel):
    latency_ms: float = Field(0.0, ge=0, le=10_000)
    cpu_burn_pct: float = Field(0.0, ge=0, le=100)
    duration_s: Optional[float] = Field(None, gt=0, le=3600)  # auto-clear; None = until DELETE


class FaultInjector:
    PERIOD_S = 0.02

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fault: Optional[dict] = None
        self._stop = threading.Event()
        self._burner: Optional[threading.Thread] = None

    def set(self, req: FaultReq) -> dict:
        self.clear()
        now = time.time()
        fault = {"latency_ms": req.latency_ms, "cpu_burn_pct": req.cpu_burn_pct, "since": now,
                 "until": now + req.duration_s if req.duration_s else None}
        with self._lock:
            self._fault = fault
            if req.cpu_burn_pct > 0:
                self._stop = threading.Event()
                self._burner = threading.Thread(target=self._burn, args=(req.cpu_burn_pct / 100, self._stop),
                                                name="fault-burn", daemon=True)
                self._burner.start()
        return dict(fault)

    def clear(self) -> None:
        with self._lock:
            self._fault = None
            self._stop.set()
            burner, self._burner = self._burner, None
        if burner is not None:
            burner.join(timeout=1)

    def current(self) -> Optional[dict]:
        with self._lock:
            f = self._fault
        if f and f["until"] is not None and time.time() >= f["until"]:
            self.clear()
            return None
        return dict(f) if f else None

    def apply_latency(self) -> None:
        f = self.current()
        if f and f["latency_ms"] > 0:
            time.sleep(f["latency_ms"] / 1000)

    def _burn(self, duty: float, stop: threading.Event) -> None:
        # Busy for duty*PERIOD, idle for the rest: holds the GIL and the cgroup CPU like a bad host.
        while not stop.is_set():
            t0 = time.perf_counter()
            busy = self.PERIOD_S * duty
            while time.perf_counter() - t0 < busy:
                pass
            rest = self.PERIOD_S - (time.perf_counter() - t0)
            if rest > 0:
                stop.wait(rest)
            if self._fault and self._fault["until"] is not None and time.time() >= self._fault["until"]:
                return
