"""Run the bridge: python -m tenantguard_handoff [--host 0.0.0.0] [--port 8400]."""
from __future__ import annotations

import argparse
import os

import uvicorn
from tenantguard_common.config import load_settings

from .settings import HandoffSettings


def main() -> None:
    s = HandoffSettings.from_dict(load_settings())
    p = argparse.ArgumentParser(prog="tenantguard_handoff")
    p.add_argument("--host", default=os.environ.get("HANDOFF_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("HANDOFF_PORT", s.port)))
    a = p.parse_args()
    uvicorn.run("tenantguard_handoff.app:app", host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
