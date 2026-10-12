from __future__ import annotations

import asyncio
import json

import httpx
from conftest import SRV, T0, ev, scenario, stats
from fastapi.testclient import TestClient

from tenantguard_billing.main import Billing, create_app, poll_loop
from tenantguard_common.contracts import H_CONTROL_TOKEN, BillView, WhyView


def client_with(billing) -> TestClient:
    return TestClient(create_app(billing.cfg, ingest=False, billing=billing))


def test_api_bills_and_why(billing):
    scenario(billing)
    with client_with(billing) as c:
        allb = c.get("/bills", params={"from": T0, "to": T0 + 180}).json()
        assert [b["tenant"] for b in allb] == sorted(billing.cfg.tenants)
        a = BillView(**c.get("/bills/A", params={"from": T0, "to": T0 + 180}).json())
        assert a.sla_credit_pct == 4.0 and a.contention_waived_cpu_ms == 200.0
        w = WhyView(**c.get("/why/A", params={"from": T0, "to": T0 + 180}).json())
        assert w.credit_pct == 4.0 and "another tenant's workload" in w.sentence
        assert c.get("/bills/a").status_code == 404           # exact id only
        assert c.get("/why/ZZ").status_code == 404
        assert c.get("/bills/A", params={"from": T0 + 10, "to": T0}).status_code == 400
        h = c.get("/healthz").json()
        assert h["ok"] and h["episodes"] == 1 and h["ticks"] > 0
        r = c.options("/bills", headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET"})
        assert r.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_api_is_read_only(billing):
    with client_with(billing) as c:
        assert c.post("/bills/A").status_code == 405
        assert c.delete("/bills/A").status_code == 405


def test_poll_loop_reads_workers_brain_gateway(cfg):
    cfg.worker_urls = {"w1": "http://w1:8100", "w2": "http://w2:8100"}
    cfg.brain_url, cfg.gateway_url, cfg.poll_s = "http://brain:8200", "http://gw:8000", 0.05
    b = Billing(cfg, started_at=T0 - 1)
    seen = {"token": None, "calls": 0}
    w1_samples = [stats("w1", T0 + 1, T0 - 10, {"A": {"requests": 1, "cpu": 1.0}}),
                  stats("w1", T0 + 2, T0 - 10, {"A": {"requests": 5, "cpu": 9.0}})]

    def handler(req: httpx.Request) -> httpx.Response:
        seen["calls"] += 1
        host, path = req.url.host, req.url.path
        if host == "w1" and path == "/stats":
            s = w1_samples[0] if len(w1_samples) == 1 else w1_samples.pop(0)
            return httpx.Response(200, json=json.loads(s.model_dump_json()))
        if host == "w2":
            return httpx.Response(503)                         # a down worker must not stop billing
        if host == "brain" and path == "/api/incidents":
            return httpx.Response(200, json=[json.loads(ev(SRV, "detected", T0 + 1).model_dump_json())])
        if host == "gw" and path == "/admin/tenants":
            seen["token"] = req.headers.get(H_CONTROL_TOKEN)
            t = cfg.tenants["A"]
            view = {"id": "A", "name": "Aarogya Renamed", "tier": "enterprise", "servers": t.servers, "weight": 4,
                    "quota": t.quota.model_dump(), "effective_quota": t.quota.model_dump()}
            return httpx.Response(200, json=[view])
        return httpx.Response(404)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task = asyncio.ensure_future(poll_loop(b, client))
        await asyncio.sleep(0.4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()

    asyncio.run(run())
    assert sum(u.requests for _, u in b.meter.tenant_buckets("A", 0, 1e12)) == 4
    assert SRV in b.timeline.episodes
    assert b.sources["worker:w1"]["ok"] and not b.sources["worker:w2"]["ok"]
    assert b.sources["brain:incidents"]["ok"] and b.sources["gateway:tenants"]["ok"]
    assert seen["token"] == cfg.control_token
    assert cfg.tenants["A"].name == "Aarogya Renamed" and cfg.tenants["A"].tier == "enterprise"
    assert cfg.tenants["A"].servers == ["w0", "w1", "w2"]      # barcodes stay from the layout
