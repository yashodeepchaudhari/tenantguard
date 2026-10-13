"""T20: demo preflight + banners, faulty_server fallback on a reachable worker, robust cleanup, config defaults."""
from __future__ import annotations

import json

import httpx
import pytest
from test_demo import Fake, make

import demo
from tenantguard_common import contracts


class Net(Fake):
    """Fake plus workers: `up` workers answer /healthz and /admin/fault, others refuse connections."""

    def __init__(self, up=(), lab_delete_status: int = 200, per_id_status: int = 200, probe_starts=False, **kw):
        super().__init__(**kw)
        self.up, self.lab_delete_status, self.per_id_status = set(up), lab_delete_status, per_id_status
        self.probe_starts = probe_starts

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, p = req.url.host, req.url.path
        if host.startswith("w"):
            self.calls.append(req)
            if host not in self.up:
                raise httpx.ConnectError("refused", request=req)
            return httpx.Response(200, json={"ok": True, "fault": None})
        if p == "/api/pool":
            self.calls.append(req)
            return httpx.Response(200, json={"projects": [{"id": "media", "replicas": [
                {"id": "tg-media-1", "url": "http://w4:8100/"}]}]})
        if p == "/lab/attacks" and req.method == "DELETE":
            self.calls.append(req)
            return httpx.Response(self.lab_delete_status)
        if p.startswith("/lab/attack/") and req.method == "DELETE":
            self.calls.append(req)
            return httpx.Response(self.per_id_status, json={})
        if self.probe_starts and p == "/lab/attack":
            self.calls.append(req)
            self.n += 1
            return httpx.Response(200, json={"attack_id": f"a{self.n}"})
        return super().__call__(req)


WORKERS = {"tg-media-1": "http://w4:8100"}


def steps(*keys):
    return [s for s in demo.plan() if s.key in keys]


def test_preflight_skips_unavailable_steps_with_a_banner_and_starts_nothing():
    fake = Net(unavailable={"faulty_server", "honest_spike"})
    d, clk, lines = make(fake, workers=WORKERS)                 # w4 not reachable from here
    plan = demo.plan()
    d.run(plan)
    text = "\n".join(lines)
    assert "[NOT AVAILABLE] step 'faulty' will be skipped" in text and "replica tg-media-1 is not reachable" in text
    assert "[NOT AVAILABLE] step 'spike' will be skipped" in text
    assert "[6/9] [NOT AVAILABLE] Faulty replica: skipped" in text
    attacks = [x[1] for x in fake.seq() if x[0] == "attack"]
    assert "faulty_server" not in attacks and "honest_spike" not in attacks
    assert clk.t == pytest.approx(sum(s.seconds for s in plan if s.key not in ("faulty", "spike")))
    probes = [json.loads(r.content) for r in fake.calls if r.url.path == "/lab/attack"
              and json.loads(r.content).get("tenant") == demo.PROBE]
    assert {p["attack"] for p in probes} >= {"flood", "blackbox_bomb", "faulty_server", "honest_spike"}
    assert len(probes) == len({p["attack"] for p in probes})   # each attack probed once


def test_faulty_server_falls_back_to_worker_fault_when_reachable_and_cleanup_clears_it():
    fake = Net(unavailable={"faulty_server"}, up={"w4"})
    d, _, lines = make(fake, workers=WORKERS, admin_token="adm")
    d.run(steps("calm", "faulty"))
    posts = [r for r in fake.calls if r.url.host == "w4" and r.method == "POST"]
    assert len(posts) == 1 and posts[0].url.path == "/admin/fault"
    body = json.loads(posts[0].content)
    assert body["duration_s"] == 20 and body["latency_ms"] > 0       # TTL-bound: auto-clears by itself too
    assert posts[0].headers["X-Admin-Token"] == "adm"
    deletes = [r for r in fake.calls if r.url.host == "w4" and r.method == "DELETE"]
    assert len(deletes) == 1 and deletes[0].url.path == "/admin/fault"
    assert d.faults == []
    assert any("injects the fault directly on tg-media-1" in l for l in lines)


def test_the_faulty_replica_is_found_through_the_brains_pool_view():
    fake = Net(unavailable={"faulty_server"}, up={"w4"})
    d, _, lines = make(fake)                                         # no TENANTGUARD_WORKER_URLS at all
    d.run(steps("calm", "faulty"))
    assert any(r.url.path == "/api/pool" for r in fake.calls)
    posts = [r for r in fake.calls if r.url.host == "w4" and r.method == "POST"]
    assert len(posts) == 1 and posts[0].url.path == "/admin/fault"
    assert any("injects the fault directly on tg-media-1 (http://w4:8100/admin/fault)" in l for l in lines)


def test_lab_that_starts_a_probe_gets_it_stopped_immediately():
    fake = Net(probe_starts=True)
    d, _, _ = make(fake)
    assert d.probe("flood") is True
    first_delete = next(r for r in fake.calls if r.method == "DELETE")
    assert first_delete.url.path == "/lab/attack/a1"


def test_stop_all_retries_then_stops_our_attacks_one_by_one():
    fake = Net(lab_delete_status=500, per_id_status=200)
    d, _, lines = make(fake)
    d.started = ["a7", "a8"]
    assert d.stop_all() is True and d.started == []
    paths = [r.url.path for r in fake.calls if r.method == "DELETE"]
    assert paths == ["/lab/attacks", "/lab/attacks", "/lab/attack/a7", "/lab/attack/a8"]


def test_stop_all_failure_is_loud_and_keeps_the_ids():
    fake = Net(lab_delete_status=500, per_id_status=500)
    d, _, lines = make(fake)
    d.started = ["a7"]
    assert d.stop_all() is False and d.started == ["a7"]
    assert any("could not stop attacks a7" in l for l in lines)


def test_second_ctrl_c_during_cleanup_does_not_skip_the_rest():
    fake = Net()
    d, _, lines = make(fake)
    real_stop = d.stop_all
    calls = {"n": 0}

    def flaky_stop():
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_stop()

    d.stop_all = flaky_stop
    d.cleanup()
    assert calls["n"] == 2                                       # retried after the Ctrl-C
    assert [x for x in fake.seq() if x[0] == "mode"] == [("mode", "tenantguard")]
    assert any("Ctrl-C ignored" in l for l in lines)


def test_control_token_header_comes_from_contracts():
    assert demo.H_CONTROL_TOKEN is contracts.H_CONTROL_TOKEN
