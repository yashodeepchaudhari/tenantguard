from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.main import create_app  # noqa: E402
from tenantguard_common.contracts import H_CLASS, H_TENANT  # noqa: E402


def hdr(tenant: str, cls: str | None = None) -> dict:
    h = {H_TENANT: tenant}
    if cls:
        h[H_CLASS] = cls
    return h


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(**overrides) -> TestClient:
        cfg = load_config(server=overrides.pop("server", "wt"), data_dir=tmp_path / f"d{len(clients)}", **overrides)
        c = TestClient(create_app(cfg))
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client(seed_rows=50)


@pytest.fixture
def worker(client):
    return client.app.state.worker
