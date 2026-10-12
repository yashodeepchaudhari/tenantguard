"""Reproduce worker memory retention without the web stack (Linux, stdlib only).

Runs the worker's real search kernel and /api/memory-shaped allocations on a thread pool (like the
worker's request threads), then reports RSS: baseline, after the load, and after malloc_trim(0).

    python3 tools/memrepro.py --threads 16 --rounds 6 --hold-mb 30 [--mode bytearray|mmap]
Compare with glibc tuning:  MALLOC_ARENA_MAX=2 MALLOC_MMAP_THRESHOLD_=131072 python3 tools/memrepro.py ...
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import mmap
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import work  # noqa: E402

PAGE = 4096


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return float("nan")


def trim() -> bool:
    try:
        return bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except (OSError, AttributeError):
        return False


def hold(mb: float, mode: str, hold_s: float) -> int:
    n = int(mb * 1024 * 1024)
    if mode == "mmap":
        buf = mmap.mmap(-1, n)
        for off in range(0, n, PAGE):
            buf[off] = 1
        time.sleep(hold_s)
        s = buf[0]
        buf.close()
        return s
    buf = bytearray(n)
    buf[::PAGE] = b"\x01" * len(range(0, n, PAGE))
    time.sleep(hold_s)
    s = buf[0]
    del buf
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--hold-mb", type=float, default=30)
    ap.add_argument("--searches", type=int, default=40)
    ap.add_argument("--n", type=int, default=200_000)
    ap.add_argument("--mode", choices=["bytearray", "mmap"], default="bytearray")
    a = ap.parse_args()
    env = {k: v for k, v in os.environ.items() if k.startswith("MALLOC_")}
    base = rss_mb()
    with ThreadPoolExecutor(a.threads) as pool:
        for r in range(a.rounds):
            futs = [pool.submit(hold, a.hold_mb, a.mode, 0.05) for _ in range(a.threads)]
            futs += [pool.submit(work.search, a.n, f"q{r}-{i}") for i in range(a.searches // a.rounds or 1)]
            for f in futs:
                f.result()
        time.sleep(0.2)
        gc.collect()
        after = rss_mb()
        trimmed = trim()
        after_trim = rss_mb()
    print(f"mode={a.mode} env={env or '-'} threads={a.threads} rounds={a.rounds} hold={a.hold_mb}MB "
          f"| RSS base {base:.1f} MB, after load {after:.1f} MB (+{after - base:.1f}), "
          f"after malloc_trim {after_trim:.1f} MB (+{after_trim - base:.1f}){'' if trimmed else ' [trim n/a]'}")


if __name__ == "__main__":
    main()
