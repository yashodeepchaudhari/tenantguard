"""Background jobs charged to the tenant that caused them.

`submit` copies the caller's contextvars (tenant included) into the job, runs it on a
thread pool and measures the job's own thread CPU, which is charged to that tenant as
bg_cpu_ms. Background CPU is NOT part of X-TenantGuard-Cpu-Ms (handler only).
"""
from __future__ import annotations

import contextvars
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .cpuclock import thread_cpu
from .meter import Meters
from .tenancy import current_tenant, request_var

log = logging.getLogger("worker.jobs")


class BackgroundJobs:
    def __init__(self, meters: Meters, threads: int) -> None:
        self.meters = meters
        self.pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="bg")
        self._pending = 0
        self._cv = threading.Condition()

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        current_tenant()  # a background job must belong to a tenant
        ctx = contextvars.copy_context()
        with self._cv:
            self._pending += 1
        return self.pool.submit(ctx.run, self._run, fn, *args)

    def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        tenant = current_tenant()
        request_var.set(None)  # the request is over; nothing here belongs in its header
        t0 = thread_cpu()
        try:
            return fn(*args)
        except Exception:
            log.exception("background job failed for tenant %s", tenant)
            raise
        finally:
            self.meters.add_bg(tenant, (thread_cpu() - t0) * 1000)
            with self._cv:
                self._pending -= 1
                self._cv.notify_all()

    @property
    def pending(self) -> int:
        return self._pending

    def drain(self, timeout: float = 10.0) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0, timeout)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
