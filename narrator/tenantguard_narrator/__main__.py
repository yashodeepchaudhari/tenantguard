"""Run the narrator: python -m tenantguard_narrator [--host 0.0.0.0] [--port 8700]."""
from __future__ import annotations

import argparse
import os

import uvicorn
from tenantguard_common.config import load_settings

from .settings import NarratorSettings


def main() -> None:
    s = NarratorSettings.from_dict(load_settings())
    p = argparse.ArgumentParser(prog="tenantguard_narrator")
    p.add_argument("--host", default=os.environ.get("NARRATOR_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("NARRATOR_PORT", s.port)))
    a = p.parse_args()
    uvicorn.run("tenantguard_narrator.app:app_factory", factory=True, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
