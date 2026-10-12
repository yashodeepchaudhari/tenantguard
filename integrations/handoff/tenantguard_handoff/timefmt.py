"""Epoch <-> "HH:MM:SS" IST (UTC+05:30, no DST). The only place the bridge converts times."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def ist(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")


def from_ist(hhmmss: str, ref: Optional[float] = None) -> float:
    """The epoch of HH:MM:SS IST on the day closest to `ref` (handles midnight wrap within +-12 h)."""
    ref = time.time() if ref is None else ref
    h, m, s = (int(x) for x in hhmmss.split(":"))
    day = datetime.fromtimestamp(ref, IST)
    cand = day.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
    if cand - ref > 12 * 3600:
        cand -= 86400
    elif ref - cand > 12 * 3600:
        cand += 86400
    return cand
