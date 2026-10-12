"""glibc allocator tuning and trimming (Linux; no-ops elsewhere).

Why: with default glibc settings a worker never returned memory after large buffers were freed on its
request threads. After the first large free, glibc raises its mmap threshold (up to 32 MB), so later large
buffers come from per-thread arenas (up to 8 x cores of them), and freed arena memory stays in the process.
Measured (16 threads, 30 MB buffers): +480 MB RSS kept with defaults, +0.3 MB with the settings below.
In a 256 MB container that filled the cgroup and stalled the worker.

- tune(): pin the mmap threshold (large buffers always come from mmap and go back to the OS on free) and cap
  the number of arenas. Docker also sets MALLOC_MMAP_THRESHOLD_ / MALLOC_ARENA_MAX, which covers child processes.
- trim(): malloc_trim(0) returns free heap pages to the OS.
- Trimmer: a daemon thread that trims every `interval_s`.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Optional

log = logging.getLogger("worker.malloc")

M_MMAP_THRESHOLD = -3
M_ARENA_MAX = -8
MMAP_THRESHOLD_BYTES = 128 * 1024
ARENA_MAX = 2


class _MallInfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in
                ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks", "fordblks",
                 "keepcost")]


def _libc():
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL("libc.so.6")
    except OSError:  # not glibc (e.g. musl)
        return None


_LIBC = _libc()
_tuned = False


def tune() -> bool:
    """Apply the allocator settings once; True if glibc accepted both."""
    global _tuned
    if _LIBC is None:
        return False
    if not _tuned:
        try:
            ok = (_LIBC.mallopt(M_MMAP_THRESHOLD, MMAP_THRESHOLD_BYTES) == 1) & (_LIBC.mallopt(M_ARENA_MAX, ARENA_MAX) == 1)
        except AttributeError:
            return False
        _tuned = bool(ok)
        if not _tuned:
            log.warning("mallopt refused the allocator settings")
    return _tuned


def trim() -> bool:
    """Return free heap memory to the OS; True if some was released."""
    if _LIBC is None:
        return False
    try:
        return bool(_LIBC.malloc_trim(0))
    except AttributeError:
        return False


def info() -> Optional[dict]:
    """Allocator totals in MB (glibc >= 2.33), or None."""
    if _LIBC is None or not hasattr(_LIBC, "mallinfo2"):
        return None
    _LIBC.mallinfo2.restype = _MallInfo2
    m = _LIBC.mallinfo2()
    mb = 1024 * 1024
    return {"heap_mb": round(m.arena / mb, 2), "mmapped_mb": round(m.hblkhd / mb, 2),
            "in_use_mb": round(m.uordblks / mb, 2), "free_mb": round(m.fordblks / mb, 2),
            "trimmable_mb": round(m.keepcost / mb, 2), "tuned": _tuned}


class Trimmer:
    """Daemon thread: trim() every interval_s (0 disables)."""

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self.trims = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if interval_s > 0 and _LIBC is not None:
            self._thread = threading.Thread(target=self._loop, name="malloc-trim", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.run()

    def run(self) -> bool:
        released = trim()
        self.trims += 1
        return released

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
