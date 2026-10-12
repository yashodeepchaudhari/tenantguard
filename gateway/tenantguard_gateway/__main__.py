"""Run the gateway: python -m tenantguard_gateway [--host 0.0.0.0] [--port 8000]."""
from __future__ import annotations

import argparse
import os
from urllib.parse import urlparse

import uvicorn
from tenantguard_common.config import load_settings


def main() -> None:
    settings = load_settings()
    default_port = urlparse(settings["services"]["gateway"]).port or 8000
    p = argparse.ArgumentParser(prog="tenantguard_gateway")
    p.add_argument("--host", default=os.environ.get("TENANTGUARD_GATEWAY_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("TENANTGUARD_GATEWAY_PORT", default_port)))
    a = p.parse_args()
    uvicorn.run("tenantguard_gateway.app:app", host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
