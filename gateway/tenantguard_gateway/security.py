"""Input hardening for the data plane (pure functions)."""
from __future__ import annotations

import posixpath
import re
from typing import Optional

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Raw (still percent-encoded) sequences that would let a path escape /api/ after the worker decodes it.
_BAD_RAW = ("%2f", "%5c", "%00", "%23", "%3f")


def safe_api_path(raw_path: bytes, decoded: str) -> Optional[str]:
    """Return the raw path to forward if it provably stays under /api/, else None (-> 400).

    Rejects: encoded '/', '\\', NUL, '#', '?' in the raw path; '\\', control characters, '.' or '..'
    segments, empty segments ('//') in the decoded path; anything that does not normalize under /api/.
    The RAW path is forwarded, so the worker decodes exactly what was validated here.
    """
    raw = raw_path.split(b"?", 1)[0]
    try:
        raw_s = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    low = raw_s.lower()
    if any(b in low for b in _BAD_RAW):
        return None
    if "\\" in decoded or "#" in decoded or any(ord(c) < 0x20 or ord(c) == 0x7F for c in decoded):
        return None
    segs = decoded.split("/")
    if any(s in (".", "..") for s in segs) or ".." in decoded:
        return None
    if "//" in decoded:
        return None
    norm = posixpath.normpath(decoded)
    if not (norm.startswith("/api/") and decoded.startswith("/api/")) or not raw_s.startswith("/api/"):
        return None
    return raw_s


def valid_request_id(v: Optional[str]) -> bool:
    return bool(v) and REQUEST_ID_RE.match(v) is not None
