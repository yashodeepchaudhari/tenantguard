"""The "vendor indexer": a black-box child process whose CPU no tenant meter can see.

Handlers only enqueue a doc count (cheap). The child burns a fixed CPU amount per doc.
Its CPU is read with psutil on the child and reported only as the cumulative
WorkerStats.blackbox_cpu_ms, never per tenant. Inside a container it shares the
cgroup CPU limit with the worker, so heavy ingest slows every co-located tenant.

- the queue is bounded in docs (queue_max): excess docs are dropped and counted
- the child is spawned at startup by a monitor thread (never on a request's CPU) and
  restarted if it dies, with backoff; queued docs of the dead child are lost
- the child exits by itself if the parent process disappears
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue as queue_mod
import threading
import time
from typing import Dict, Optional

import psutil

log = logging.getLogger("worker.blackbox")

_CTX = mp.get_context("spawn")  # no fork-with-threads surprises; same behaviour on Windows and Linux


def _indexer_main(q, processed, cpu_ms_per_doc: float, parent_pid: int) -> None:
    from .cpuclock import thread_cpu  # precise per-thread CPU on Windows too

    while True:
        try:
            docs = q.get(timeout=1.0)
        except queue_mod.Empty:
            if not psutil.pid_exists(parent_pid):
                return
            continue
        if docs is None:
            return
        for _ in range(docs):
            end = thread_cpu() + cpu_ms_per_doc / 1000
            x = 0
            while thread_cpu() < end:
                for i in range(200):
                    x = (x * 1103515245 + i) & 0x7FFFFFFF
            with processed.get_lock():
                processed.value += 1


def _kill(proc) -> None:
    try:
        proc.kill()
    except OSError:  # already exited (Windows reports Access denied)
        pass


class BlackBox:
    MONITOR_S = 0.25
    BACKOFF_S = (0.25, 1.0, 4.0)  # restart delay after consecutive quick deaths
    QUICK_DEATH_S = 5.0

    def __init__(self, cpu_ms_per_doc: float, queue_max: int) -> None:
        self.cpu_ms_per_doc = cpu_ms_per_doc
        self.queue_max = queue_max
        self._lock = threading.Lock()
        self._proc = None
        self._psproc: Optional[psutil.Process] = None
        self._q = None
        self._processed = None
        self._enqueued = 0          # docs accepted into the current child's queue
        self._dead_cpu_ms = 0.0     # CPU of children that already exited
        self._last_cpu_ms = 0.0     # last reading of the current child
        self._done_before = 0       # docs processed by children that already exited
        self.accepted = 0
        self.dropped = 0
        self.docs_by_tenant: Dict[str, int] = {}   # docs accepted per tenant (lets the brain split this CPU)
        self.restarts = 0
        self._started_at = 0.0
        self._quick_deaths = 0
        self._stop = threading.Event()
        self._new_queue()
        self._monitor = threading.Thread(target=self._watch, name="blackbox-monitor", daemon=True)
        self._monitor.start()

    # ---- lifecycle ----
    def _new_queue(self) -> None:
        self._q = _CTX.Queue()
        self._processed = _CTX.Value("q", 0)
        self._enqueued = 0
        self._last_cpu_ms = 0.0

    def _spawn(self) -> None:
        self._started_at = time.monotonic()
        self._proc = _CTX.Process(target=_indexer_main, name="vendor-indexer", daemon=True,
                                  args=(self._q, self._processed, self.cpu_ms_per_doc, os.getpid()))
        self._proc.start()
        self._psproc = psutil.Process(self._proc.pid)
        try:  # share the parent's cores (local --pin-cores; a container shares the cgroup anyway)
            self._psproc.cpu_affinity(psutil.Process().cpu_affinity())
        except (psutil.Error, AttributeError):
            pass

    def _watch(self) -> None:
        with self._lock:
            if not self._stop.is_set():
                self._spawn()
        while not self._stop.wait(self.MONITOR_S):
            delay = 0.0
            with self._lock:
                self._sample_locked()
                if self._proc is None or self._proc.is_alive() or self._stop.is_set():
                    continue
                log.warning("vendor indexer died (exit %s); restarting", self._proc.exitcode)
                quick = time.monotonic() - self._started_at < self.QUICK_DEATH_S
                self._quick_deaths = self._quick_deaths + 1 if quick else 0
                delay = self.BACKOFF_S[min(self._quick_deaths, len(self.BACKOFF_S)) - 1] if self._quick_deaths else 0.0
                self._retire_locked()
                self._new_queue()
                self._proc = None
            if delay and self._stop.wait(delay):
                return
            with self._lock:
                if not self._stop.is_set():
                    self.restarts += 1
                    self._spawn()

    def _retire_locked(self) -> None:
        self._dead_cpu_ms += self._last_cpu_ms
        self._last_cpu_ms = 0.0
        self._psproc = None
        self._done_before += self._processed.value
        self._q.close()
        self._q.cancel_join_thread()

    def _sample_locked(self) -> None:
        if self._psproc is None:
            return
        try:
            t = self._psproc.cpu_times()
            self._last_cpu_ms = (t.user + t.system) * 1000
        except psutil.Error:
            pass  # dead: keep the last reading

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            if self._proc is not None:
                self._sample_locked()
                try:
                    self._q.put_nowait(None)
                except Exception:
                    pass
                self._proc.join(timeout=1)
                if self._proc.is_alive():
                    _kill(self._proc)
                    self._proc.join(timeout=1)
                self._retire_locked()
                self._proc = None
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=2)

    # ---- API ----
    def submit(self, docs: int, tenant: str = "") -> dict:
        with self._lock:
            pending = self._enqueued - self._processed.value
            take = max(0, min(docs, self.queue_max - pending))
            if take:
                self._q.put(take)
                self._enqueued += take
                if tenant:
                    self.docs_by_tenant[tenant] = self.docs_by_tenant.get(tenant, 0) + take
            self.accepted += take
            self.dropped += docs - take
            return {"accepted": take, "dropped": docs - take, "queued": pending + take}

    def cpu_ms(self) -> float:
        with self._lock:
            self._sample_locked()
            return self._dead_cpu_ms + self._last_cpu_ms

    def kill_child(self) -> None:
        """Test / chaos hook: hard-kill the child (the monitor restarts it)."""
        with self._lock:
            if self._proc is not None:
                self._sample_locked()
                _kill(self._proc)

    def status(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.is_alive()
            processed = self._done_before + self._processed.value
            pending = self._enqueued - self._processed.value
            return {"running": running, "pid": self._proc.pid if self._proc is not None else None,
                    "queued": pending, "queue_max": self.queue_max, "accepted": self.accepted,
                    "processed": processed, "dropped": self.dropped, "restarts": self.restarts,
                    "cpu_ms_per_doc": self.cpu_ms_per_doc}
