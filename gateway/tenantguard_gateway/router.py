"""Pick one of a tenant's servers. Pure."""
from __future__ import annotations

import random
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

Candidates = List[Tuple[str, float]]


def candidates(servers: Sequence[str], known: Iterable[str], weights: Optional[Dict[str, float]] = None,
               relocation: Optional[str] = None, drained: Optional[Set[str]] = None) -> Candidates:
    """Weighted candidate servers for one request.

    - relocation target (if a known server) wins outright;
    - route weights (if given) replace the default equal weights and may include servers outside the barcode;
    - drained servers are skipped, but a request is never black-holed: if every candidate is drained or
      zero-weight we fall back to the undrained barcode, then to the whole barcode.
    """
    known = set(known)
    drained = drained or set()
    if relocation and relocation in known:
        return [(relocation, 1.0)]
    base = [s for s in servers if s in known]
    if weights:
        order = base + sorted(s for s in weights if s not in base and s in known)
        w = [(s, float(weights.get(s, 0.0))) for s in order]
    else:
        w = [(s, 1.0) for s in base]
    pos = [(s, x) for s, x in w if x > 0 and s not in drained]
    if not pos:
        pos = [(s, 1.0) for s in base if s not in drained]
    if not pos:
        pos = [(s, 1.0) for s in base]
    return pos


def choose(cands: Candidates, has_capacity: Callable[[str], bool] = lambda s: True,
           rng: Optional[random.Random] = None) -> Optional[str]:
    """Weighted random pick among candidates that have dispatch capacity; None if all are full."""
    free = [(s, w) for s, w in cands if has_capacity(s)]
    if not free:
        return None
    if len(free) == 1:
        return free[0][0]
    r = (rng or random).random() * sum(w for _, w in free)
    for s, w in free:
        r -= w
        if r < 0:
            return s
    return free[-1][0]


def normalize(weights: Dict[str, float]) -> Dict[str, float]:
    tot = sum(weights.values())
    return {s: w / tot for s, w in weights.items()} if tot > 0 else dict(weights)
