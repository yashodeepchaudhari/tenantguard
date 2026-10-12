"""Background jobs charged to the tenant that caused them.

`submit` copies the caller's contextvars (tenant included) into the job, runs it on a
thread pool and measures the job's own thread CPU, which is charged to that tenant as
bg_cpu_ms. Background CPU is NOT part of X-TenantGuard-Cpu-Ms (handler only).

The backlog is capped per tenant and in total (BgBacklog → 503), so one tenant cannot
queue unbounded work in front of everyone else.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict

from .cpuclock import thread_cpu
from .meter import Meters
from .tenancy import current_tenant, request_var

log = logging.getLogger("worker.jobs")


class BgBacklog(Exception):
    """The tenant's (or the worker's) background backlog is full."""


class BackgroundJobs:
    def __init__(self, meters: Meters, threads: int, per_tenant_max: int = 200, total_max: int = 1000) -> None:
        self.meters = meters
        self.pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="bg")
        self.per_tenant_max = per_tenant_max
        self.total_max = total_max
        self._pending: Dict[str, int] = defaultdict(int)  # queued + running, per tenant
        self._total = 0
        self._running = 0
        self._cv = threading.Condition()

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        tenant = current_tenant()  # a background job must belong to a tenant
        with self._cv:
            if self._pending[tenant] >= self.per_tenant_max:
                raise BgBacklog(f"tenant {tenant} has {self._pending[tenant]} pending background jobs "
                                f"(max {self.per_tenant_max})")
            if self._total >= self.total_max:
                raise BgBacklog(f"worker has {self._total} pending background jobs (max {self.total_max})")
            self._pending[tenant] += 1
            self._total += 1
        ctx = contextvars.copy_context()
        try:
            fut = self.pool.submit(ctx.run, self._run, fn, *args)
        except RuntimeError:  # pool shut down
            self._done(tenant)
            raise
        fut.add_done_callback(lambda f: f.cancelled() and self._done(tenant))
        return fut

    def _done(self, tenant: str) -> None:
        with self._cv:
            self._pending[tenant] -= 1
            self._total -= 1
            self._cv.notify_all()

    def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        tenant = current_tenant()
        request_var.set(None)  # the request is over; nothing here belongs in its header
        with self._cv:
            self._running += 1
        t0 = thread_cpu()
        try:
            return fn(*args)
        except Exception:
            log.exception("background job failed for tenant %s", tenant)
            raise
        finally:
            self.meters.add_bg(tenant, (thread_cpu() - t0) * 1000)
            with self._cv:
                self._running -= 1
            self._done(tenant)

    @property
    def pending(self) -> int:
        return self._total

    def pending_by_tenant(self) -> Dict[str, int]:
        with self._cv:
            return {t: n for t, n in self._pending.items() if n}

    def drain(self, timeout: float = 10.0) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._total == 0, timeout)

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Cancel queued jobs and wait (bounded) for running ones, so storage can close safely."""
        self.pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + timeout
        with self._cv:
            return self._cv.wait_for(lambda: self._running == 0, max(0.0, deadline - time.monotonic()))
