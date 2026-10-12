"""Per-thread CPU clock in seconds.

Linux (Docker): time.thread_time() (CLOCK_THREAD_CPUTIME_ID, ns resolution).
Windows (local dev): time.thread_time() uses GetThreadTimes, which only advances in
15.625 ms scheduler ticks, so a 1 ms handler reads as 0 or 15.6 ms. There we use
QueryThreadCycleTime (exact cycles charged to the thread) divided by a cycles-per-second
rate calibrated once at import against perf_counter on a busy loop.
"""
from __future__ import annotations

import sys
import time


def _windows_clock():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentThread.restype = wintypes.HANDLE
    k32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulonglong)]
    k32.QueryThreadCycleTime.restype = wintypes.BOOL

    def cycles() -> int:
        c = ctypes.c_ulonglong()
        if not k32.QueryThreadCycleTime(k32.GetCurrentThread(), ctypes.byref(c)):
            raise OSError(ctypes.get_last_error())
        return c.value

    rates = []
    for _ in range(5):
        c0, t0 = cycles(), time.perf_counter()
        while time.perf_counter() - t0 < 0.02:
            pass
        rates.append((cycles() - c0) / (time.perf_counter() - t0))
    rates.sort()
    hz = rates[-2]  # near-max: preemption during a sample only lowers the rate

    def thread_cpu() -> float:
        return cycles() / hz

    return thread_cpu


thread_cpu = time.thread_time
if sys.platform == "win32":
    try:
        thread_cpu = _windows_clock()
    except Exception:  # pragma: no cover - fall back to the coarse clock
        thread_cpu = time.thread_time
