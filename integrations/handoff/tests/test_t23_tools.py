"""T23: the partner tools send X-Handoff-Token (fail clearly without it); history_keep / id_map_max from yaml."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import mock_web  # noqa: E402
import prove_handoff  # noqa: E402

from tenantguard_handoff import wire  # noqa: E402
from tenantguard_handoff.app import create_app  # noqa: E402
from tenantguard_handoff.settings import HandoffSettings  # noqa: E402
from test_bridge_api import Control, Web, incident, make  # noqa: E402


class Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        return SimpleNamespace(status_code=200, text='{"ok": true}', json=lambda: {"ok": True})


def test_partner_headers_require_the_token(monkeypatch):
    monkeypatch.delenv("HANDOFF_PARTNER_TOKEN", raising=False)
    with pytest.raises(SystemExit) as e:
        mock_web.partner_headers()
    assert "HANDOFF_PARTNER_TOKEN is not set" in str(e.value)
    monkeypatch.setenv("HANDOFF_PARTNER_TOKEN", "p4ss")
    assert mock_web.partner_headers() == {wire.PARTNER_TOKEN_HEADER: "p4ss"} == {"X-Handoff-Token": "p4ss"}


def test_mock_web_decide_and_owner_send_the_token(monkeypatch):
    monkeypatch.setenv("HANDOFF_PARTNER_TOKEN", "p4ss")
    cap = Capture()
    monkeypatch.setattr(mock_web.httpx, "post", cap)
    monkeypatch.setattr(sys, "argv", ["mock_web.py", "decide", "INC-0001", "A1", "approve", "Rahul"])
    mock_web.main()
    monkeypatch.setattr(sys, "argv", ["mock_web.py", "owner", "INC-0001", "Rahul"])
    mock_web.main()
    assert [c["headers"] for c in cap.calls] == [{"X-Handoff-Token": "p4ss"}] * 2
    assert cap.calls[0]["url"].endswith("/control/case-update") and cap.calls[0]["json"]["decision"] == "approve"
    assert cap.calls[1]["json"]["type"] == "owner"


def test_mock_web_without_token_fails_before_sending(monkeypatch):
    monkeypatch.delenv("HANDOFF_PARTNER_TOKEN", raising=False)
    cap = Capture()
    monkeypatch.setattr(mock_web.httpx, "post", cap)
    monkeypatch.setattr(sys, "argv", ["mock_web.py", "decide", "INC-0001", "A1", "approve", "Rahul"])
    with pytest.raises(SystemExit):
        mock_web.main()
    assert cap.calls == []


def test_prove_handoff_update_sends_the_token(monkeypatch):
    monkeypatch.setenv("HANDOFF_PARTNER_TOKEN", "p4ss")
    cap = Capture()
    monkeypatch.setattr(prove_handoff.httpx, "post", cap)
    out = prove_handoff.update({"case_id": "INC-0001", "type": "owner", "owner": "R", "time": "10:00:00"})
    assert out["status"] == 200 and cap.calls[0]["headers"] == {"X-Handoff-Token": "p4ss"}


def test_tool_headers_are_accepted_by_the_real_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_PARTNER_TOKEN", "p4ss")

    async def go():
        b = make(tmp_path, Web(), Control())
        app = create_app(b, start=False)
        b.ingest(incident("detected", 1.0))
        body = {"case_id": "INC-0001", "type": "owner", "owner": "Rahul", "time": "10:00:00"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as c:
            ok = await c.post("/control/case-update", json=body, headers=mock_web.partner_headers())
            bare = await c.post("/control/case-update", json=body)
        await b.stop()
        return ok, bare
    ok, bare = asyncio.run(go())
    assert ok.status_code == 200 and bare.status_code == 401


def test_history_keep_and_id_map_max_from_yaml():
    s = HandoffSettings.from_dict({"handoff": {"history_keep": 7, "id_map_max": 11}})
    assert (s.history_keep, s.id_map_max) == (7, 11)
    d = HandoffSettings.from_dict({})
    assert (d.history_keep, d.id_map_max) == (500, 2000)
