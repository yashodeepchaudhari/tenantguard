"""Adaptive per-worker dispatch window (gradient concurrency limiter). Pure: fed samples, updated per tick.

    new = old * (minRTT / sampleRTT) + sqrt(old), clamped to [min_window, max_window]

The RTT used is the worker-side *excess* latency: upstream latency minus the request's own CPU ms. A whale
request is slow because it is expensive, not because the worker is congested, so its own CPU is removed;
what remains is the time spent waiting inside the worker (contention). When that grows, the window shrinks
and the queue moves to the gateway, where DRR and the express lane can order it.
"""
from __future__ import annotations

import math
from statistics import median
from typing import List, Optional

RTT_FLOOR_MS = 1.0         # below this, latency differences are timer noise
MIN_RTT_DECAY = 0.001      # per tick: minRTT drifts up 0.1% so a permanently slower host is re-baselined (~3 min to double at 250 ms ticks)
GRADIENT_MIN = 0.2         # fixed point 1/(1-g)^2: g=0.2 -> ~1.6, so a badly congested worker can reach min_window
SMOOTHING = 0.2            # new window = (1 - s) * old + s * target


class GradientLimiter:
    def __init__(self, initial: int, min_window: int, max_window: int):
        self.min_window = min_window
        self.max_window = max_window
        self.window_f = float(initial)
        self.min_rtt: Optional[float] = None
        self._samples: List[float] = []
        self._peak_inflight = 0
        self.last_sample: Optional[float] = None
        self.last_gradient = 1.0

    @property
    def window(self) -> int:
        return int(round(self.window_f))

    def observe(self, upstream_ms: float, cpu_ms: float) -> None:
        self._samples.append(max(RTT_FLOOR_MS, upstream_ms - max(0.0, cpu_ms)))

    def note_inflight(self, n: int) -> None:
        self._peak_inflight = max(self._peak_inflight, n)

    def update(self) -> int:
        """Apply this tick's samples. No samples = no change (an idle worker keeps its window)."""
        samples, peak = self._samples, self._peak_inflight
        self._samples, self._peak_inflight = [], 0
        if not samples:
            return self.window
        sample = median(samples)
        self.last_sample = sample
        tick_min = min(samples)
        if self.min_rtt is None:
            self.min_rtt = tick_min
        else:
            self.min_rtt = min(tick_min, self.min_rtt * (1.0 + MIN_RTT_DECAY))
        gradient = max(GRADIENT_MIN, min(1.0, self.min_rtt / sample))
        self.last_gradient = gradient
        old = self.window_f
        if gradient >= 1.0 and peak < old / 2:
            return self.window       # not using half the window: no evidence it should grow
        target = old * gradient + math.sqrt(old)
        new = (1 - SMOOTHING) * old + SMOOTHING * target
        self.window_f = min(float(self.max_window), max(float(self.min_window), new))
        return self.window
