"""CLI: python -m tenantguard_billing [--port 8600] [--host 0.0.0.0]"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m tenantguard_billing", description="TenantGuard billing")
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--log-level", default="warning")
    args = ap.parse_args()

    from .main import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level, access_log=False)


if __name__ == "__main__":
    main()
