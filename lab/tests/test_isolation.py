"""The process-isolated attack path against a real local HTTP server (no mocks across processes)."""
import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI

from tenantguard_common.contracts import AttackReq
from tenantguard_lab.core import Lab
from tenantguard_lab.settings import load_config


@pytest.fixture(scope="module")
def server_url():
    app = FastAPI()

    @app.get("/api/{path}")
    async def api(path: str):
        return {"path": path}

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


async def test_attack_runs_in_child_process_and_streams_samples(server_url):
    cfg = load_config()
    cfg.gateway_url = server_url
    assert cfg.client["attack_isolation"] == "process"
    lab = Lab(cfg)
    assert lab.isolated
    st = await lab.launch(AttackReq(attack="flood", tenant="K", intensity=0.5, duration_s=2.0))
    entry = lab.attacks[st.attack_id]
    assert entry.proc is not None and entry.proc.proc.pid != __import__("os").getpid()
    await asyncio.sleep(1.0)
    mid = lab.recorder.totals.get("K", {}).get("count", 0)
    assert mid > 0                                   # samples stream in while the attack runs
    await entry.task                                 # ends by itself after duration_s
    assert entry.status.state == "done"
    assert not entry.proc.proc.is_alive()
    counts = lab.attack_counts(st.attack_id)
    assert counts["sent"] == lab.recorder.totals["K"]["count"] > mid
    assert counts["ok"] == counts["sent"]
    assert all(s.src == "attack" for s in lab.recorder.window("K", 0, time.time() + 1))
    await lab.aclose()


async def test_stop_isolated_attack_early(server_url):
    cfg = load_config()
    cfg.gateway_url = server_url
    lab = Lab(cfg)
    st = await lab.launch(AttackReq(attack="hog", tenant="C", intensity=0.2, duration_s=60))
    await asyncio.sleep(0.5)
    t = time.monotonic()
    stopped = await lab.stop_attack(st.attack_id)
    assert stopped.state == "stopped" and time.monotonic() - t < 5
    assert not lab.attacks[st.attack_id].proc.proc.is_alive()
    assert lab.attack_counts(st.attack_id)["sent"] > 0
    await lab.aclose()
