"""CLI: python -m app --port 8101 [--server w1] [--host 0.0.0.0]"""
from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app", description="TenantGuard worker")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--server", help="server name (default: env SERVER_NAME or w0)")
    ap.add_argument("--data-dir", help="tenant SQLite dir (default: env WORKER_DATA_DIR or ./data/<server>)")
    ap.add_argument("--log-level", default="warning")
    ap.add_argument("--pin-cores", help="local stand-in for a container CPU limit: pin this worker and its "
                                        "black-box child to these cores, e.g. '2' or '2,3' (no Docker needed)")
    args = ap.parse_args()
    if args.server:
        os.environ["SERVER_NAME"] = args.server
    if args.data_dir:
        os.environ["WORKER_DATA_DIR"] = args.data_dir

    if args.pin_cores:
        import psutil

        cores = [int(c) for c in args.pin_cores.split(",")]
        psutil.Process().cpu_affinity(cores)  # inherited by the spawned black-box child
        os.environ.setdefault("CPU_LIMIT_CORES", str(len(cores)))

    from .main import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level, access_log=False)


if __name__ == "__main__":
    main()
