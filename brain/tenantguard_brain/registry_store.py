"""Registry persistence (I/O adapter): one JSON file, written atomically (temp file + rename)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .registry import Registry


def load(path: Path, seed: Callable[[], Registry]) -> Registry:
    """The saved registry, or the yaml seed on first start (which is then saved)."""
    if path.exists():
        return Registry.model_validate_json(path.read_text(encoding="utf-8"))
    reg = seed()
    save(reg, path)
    return reg


def save(reg: Registry, path: Path) -> None:
    """Write to a temp file, then rename: a crash leaves either the old or the new file, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(reg.model_dump_json(indent=1), encoding="utf-8")
    os.replace(tmp, path)
