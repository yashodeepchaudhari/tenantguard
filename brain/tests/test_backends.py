"""DockerBackend against a fake Engine API (no Docker): request bodies, labels, scoping, never raising."""
import json

import httpx
import pytest

from tenantguard_brain.backends import DockerBackend, DockerCfg
from tenantguard_brain.scaler import Action


class FakeEngine:
    def __init__(self, fail_on: str = ""):
        self.calls = []
        self.fail_on = fail_on
        self.containers = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1.41")
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path, dict(req.url.params), body))
        if self.fail_on and self.fail_on in path:
            return httpx.Response(500, json={"message": "boom"})
        if req.method == "POST" and path == "/containers/create":
            self.containers[req.url.params["name"]] = body
            return httpx.Response(201, json={"Id": "abc"})
        if path == "/containers/json":
            return httpx.Response(200, json=[
                {"Id": "c1", "State": "running", "Labels": {
                    "tg.stack": "tg", "tg.replica": "tg-media-1", "tg.project": "media", "tg.tenants": "H,L",
                    "tg.mem_mb": "160", "tg.borrowed": "0", "tg.started_at": "12.5"}},
                {"Id": "c2", "State": "running", "Labels": {"tg.stack": "other", "tg.replica": "x", "tg.project": "p"}}])
        if path.endswith("/json"):
            return httpx.Response(200, json={"HostConfig": {"NanoCpus": 750_000_000, "CpuShares": 7680},
                                             "NetworkSettings": {"IPAddress": "172.18.0.9"}})
        if path.endswith("/stats"):
            return httpx.Response(200, json={"cpu_stats": {"cpu_usage": {"total_usage": 2_500_000_000}}})
        return httpx.Response(204)


def backend(engine, network="tg_default"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(engine), base_url="http://docker/v1.41")
    return DockerBackend(DockerCfg(image="tenantguard-worker:latest", stack="tg", network=network,
                                   cgroup_parent="tg-pool.slice"), client)


START = Action("start", "media", "tg-media-1", 750_000_000, 7680, 160, False, ["H", "L"])


async def test_start_creates_a_limited_labelled_container():
    eng = FakeEngine()
    res = await backend(eng).apply([START])
    assert res[0].ok and res[0].replica.url == "http://tg-media-1:8100" and res[0].replica.state == "starting"
    body = eng.containers["tg-tg-media-1"]
    hc = body["HostConfig"]
    assert (hc["NanoCpus"], hc["CpuShares"], hc["Memory"]) == (750_000_000, 7680, 160 * 1024 * 1024)
    assert hc["CgroupParent"] == "tg-pool.slice" and hc["NetworkMode"] == "tg_default"
    assert body["Labels"]["tg.stack"] == "tg" and body["Labels"]["tg.tenants"] == "H,L"
    assert "TENANTGUARD_TENANTS=H,L" in body["Env"]
    assert [c[1] for c in eng.calls] == ["/containers/create", "/containers/tg-tg-media-1/start"]


async def test_update_stop_and_drain():
    eng = FakeEngine()
    b = backend(eng)
    res = await b.apply([Action("update", "media", "tg-media-1", 500_000_000, 5120),
                         Action("drain", "media", "tg-media-1"),
                         Action("stop", "media", "tg-media-1")])
    assert all(r.ok for r in res)
    assert eng.calls[0][1:] == ("/containers/tg-tg-media-1/update", {}, {"NanoCpus": 500_000_000, "CpuShares": 5120})
    assert eng.calls[1][0] == "DELETE" and eng.calls[1][2] == {"force": "true"}
    assert len(eng.calls) == 2                                  # drain is routing only


async def test_apply_never_raises():
    res = await backend(FakeEngine(fail_on="create")).apply([START])
    assert not res[0].ok and "HTTPStatusError" in res[0].error


async def test_list_only_our_stack_and_cpu_ms():
    b = backend(FakeEngine())
    reps = await b.list_replicas()
    assert [(r.id, r.tenants, r.mem_mb, r.nano_cpus, r.started_at) for r in reps] == [
        ("tg-media-1", ["H", "L"], 160, 750_000_000, 12.5)]
    assert await b.cpu_ms(["tg-media-1"]) == {"tg-media-1": pytest.approx(2500.0)}


async def test_default_bridge_uses_container_ip():
    res = await backend(FakeEngine(), network="").apply([START])
    assert res[0].replica.url == "http://172.18.0.9:8100"
