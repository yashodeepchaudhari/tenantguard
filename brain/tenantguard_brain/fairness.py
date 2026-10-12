"""Fleet metrics (pure, no I/O)."""
from __future__ import annotations

from typing import Iterable, Mapping


def jain(values: Iterable[float]) -> float:
    """Jain's fairness index (sum x)^2 / (n * sum x^2), in (0, 1]. 1.0 for no / all-zero input."""
    xs = [max(0.0, float(v)) for v in values]
    sq = sum(x * x for x in xs)
    if not xs or sq <= 0:
        return 1.0
    return (sum(xs) ** 2) / (len(xs) * sq)


def normalized_throughput(rps: Mapping[str, float], weights: Mapping[str, float]) -> dict:
    """completed rps / weight for every active tenant (rps > 0)."""
    return {t: r / weights[t] for t, r in rps.items() if r > 0 and weights.get(t, 0) > 0}
