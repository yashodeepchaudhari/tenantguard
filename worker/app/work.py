"""Pure-CPU work kernels. Pure Python on purpose: they hold the GIL and the cgroup CPU,
so a heavy tenant slows every other tenant on the same worker, as on a real shared host."""
from __future__ import annotations

import hashlib
import heapq
import re
from typing import List, Tuple

WORD_RE = re.compile(r"[a-z0-9]+")


def light() -> int:
    """~1 ms of request-shaped work (parse, validate, render a small object)."""
    acc = 0
    for i in range(6000):
        acc = (acc * 31 + i) & 0xFFFFFFFF
    return acc


def search(n: int, q: str, k: int = 10) -> List[Tuple[int, int]]:
    """Score n synthetic documents against q and keep the top k. Cost is linear in n."""
    qh = int.from_bytes(hashlib.blake2b(q.encode(), digest_size=4).digest(), "big")
    top: List[Tuple[int, int]] = []
    for doc in range(n):
        x = (doc * 2654435761 ^ qh) & 0xFFFFFFFF
        x ^= x >> 13
        score = (x * 0x5BD1E995) & 0xFFFF
        if len(top) < k:
            heapq.heappush(top, (score, doc))
        elif score > top[0][0]:
            heapq.heapreplace(top, (score, doc))
    return sorted(top, reverse=True)


def index_terms(text: str, rounds: int) -> List[str]:
    """Background indexing: tokenize, then `rounds` hashing passes (the cost knob)."""
    terms = sorted(set(WORD_RE.findall(text.lower())))[:64]
    digest = text.encode()
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
        for t in terms:
            digest = hashlib.blake2b(digest + t.encode(), digest_size=16).digest()
    return terms
