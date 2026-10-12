"""Process-tree CPU / memory and the effective CPU limit."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import psutil

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
CGROUP_MEM_MAX = Path("/sys/fs/cgroup/memory.max")


class ProcessTree:
    def __init__(self) -> None:
        self.proc = psutil.Process()
        self.started_at = self.proc.create_time()

    def _tree(self):
        procs = [self.proc]
        try:
            procs += self.proc.children(recursive=True)
        except psutil.Error:
            pass
        return procs

    def cpu_total_ms(self) -> float:
        total = 0.0
        for p in self._tree():
            try:
                t = p.cpu_times()
            except psutil.Error:
                continue
            total += t.user + t.system
            if p is self.proc:  # CPU of children already reaped
                total += getattr(t, "children_user", 0.0) + getattr(t, "children_system", 0.0)
        return total * 1000

    def rss_mb(self) -> float:
        rss = 0
        for p in self._tree():
            try:
                rss += p.memory_info().rss
            except psutil.Error:
                continue
        return rss / (1024 * 1024)


def cgroup_mem_limit_mb() -> Optional[float]:
    """MB from cgroup v2 memory.max; None if unlimited/absent."""
    try:
        raw = CGROUP_MEM_MAX.read_text().strip()
    except OSError:
        return None
    return None if raw == "max" else int(raw) / (1024 * 1024)


def cgroup_cpu_limit() -> Optional[float]:
    """Cores from cgroup v2 cpu.max ("quota period" or "max period"); None if unlimited/absent.
    Follows `docker update --cpus` live."""
    try:
        quota, period = CGROUP_CPU_MAX.read_text().split()[:2]
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    return int(quota) / int(period)
