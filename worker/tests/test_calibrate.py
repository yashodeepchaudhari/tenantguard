from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.config import load_config
from app.main import create_app

WORKER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def live_worker(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(load_config(server="wcal", data_dir=tmp_path / "d", seed_rows=1000))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


def test_calibrate_cli_writes_json_to_stdout(live_worker):
    # Exactly how the Docker host runs it: `python tools/calibrate.py --where docker --out -` from worker/.
    p = subprocess.run([sys.executable, "tools/calibrate.py", "--url", live_worker, "--where", "test",
                        "--scale", "0.05", "--out", "-"], cwd=WORKER_DIR, capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)                     # stdout is pure JSON; progress went to stderr
    assert "light" in p.stderr
    assert data["meta"]["where"] == "test" and data["meta"]["server"] == "wcal"
    eps = data["endpoints"]
    assert set(eps) == {"light", "read_cold", "read_warm", "search", "write", "ingest", "memory", "export", "canary"}
    for name, e in eps.items():
        assert e["samples"] >= 3 and e["latency_ms"]["mean"] > 0, name
    assert eps["search"]["handler_cpu_ms"]["mean"] > eps["light"]["handler_cpu_ms"]["mean"]
    assert eps["ingest"]["blackbox_cpu_ms"] > 0 and eps["ingest"]["handler_cpu_ms"]["mean"] < 5
    assert eps["write"]["bg_cpu_ms"] > 0
