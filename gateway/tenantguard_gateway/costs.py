"""The one estimate of what a request costs (CPU ms): measured EWMA per (tenant, class), else the calibration
seed for its endpoint, else a default. Used for the DRR charge, the express lane and the brain's demand signal."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

from .settings import CLASS_COST_ALPHA, DEFAULT_CLASS_COST_MS

log = logging.getLogger("tenantguard.gateway")


def load_seed(path: str) -> Dict[str, float]:
    """'METHOD /path' -> handler CPU ms from worker/calibration.json (measured on a 0.5-core container).
    An endpoint calibrated several times (read cold / warm) keeps the highest mean."""
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("cost seed %s not loaded: %s", path, e)
        return {}
    out: Dict[str, float] = {}
    for ep in raw.get("endpoints", {}).values():
        key = f"{ep['method'].upper()} {ep['path']}"
        out[key] = max(out.get(key, 0.0), float(ep["handler_cpu_ms"]["mean"]))
    return out


class Costs:
    def __init__(self, seed: Optional[Dict[str, float]] = None):
        self.seed = dict(seed or {})
        self.measured: Dict[Tuple[str, str], float] = {}

    def reference_cost(self, tenant: str, cls: str, method: str, path: str) -> float:
        """Measured EWMA for this tenant's class > calibration seed for the endpoint > default."""
        got = self.measured.get((tenant, cls))
        if got is not None:
            return got
        return self.seed.get(f"{method.upper()} {path}", DEFAULT_CLASS_COST_MS)

    def update(self, tenant: str, cls: str, cpu_ms: float) -> None:
        old = self.measured.get((tenant, cls))
        self.measured[(tenant, cls)] = cpu_ms if old is None else old + CLASS_COST_ALPHA * (cpu_ms - old)
