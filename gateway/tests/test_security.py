"""Security review fixes (2b review, T7 step 0)."""
from __future__ import annotations

import asyncio
import httpx
import pytest
from tenantguard_common.contracts import H_CONTROL_TOKEN

from tenantguard_gateway.security import safe_api_path, valid_request_id
from test_api import client, make, run, snapshot, tenant

KEY = {"X-API-Key": "key-A"}


# ---------- (1) path traversal + header stripping ----------
@pytest.mark.parametrize("raw", [
    "/api/%2e%2e/admin/fault", "/api/..%2fadmin/fault", "/api/%2E%2E/%2E%2E/stats", "/api/a%2fb",
    "/api/a%5cb", "/api/x/../../admin/fault", "/api//light", "/api/a%00b", "/api/a%23b",
])
def test_traversal_is_rejected_and_never_forwarded(raw):
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            return await c.get(httpx.URL("http://gw" + raw), headers=KEY)
    r = run(go())
    assert r.status_code in (400, 404), r.status_code      # 404 only if the router itself refused it
    assert up.seen == []


def test_safe_paths_pass_and_raw_path_is_forwarded():
    assert safe_api_path(b"/api/read?key=a%2fb", "/api/read") == "/api/read"      # encoded '/' in query is fine
    assert safe_api_path(b"/api/caf%C3%A9", "/api/café") == "/api/caf%C3%A9"
    assert safe_api_path(b"/admin/x", "/admin/x") is None
    # dot segments as a raw client (not httpx, which normalizes them away) would send them
    assert safe_api_path(b"/api/./light", "/api/./light") is None
    assert safe_api_path(b"/api/../admin/fault", "/api/../admin/fault") is None
    assert safe_api_path(b"/api/%2e%2e/admin/fault", "/api/../admin/fault") is None
    assert safe_api_path(b"/api/a\\b", "/api/a\\b") is None
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            return await c.get("/api/read?key=a%2Fb", headers={**KEY, "X-Admin-Token": "dev",
                                                              "X-TenantGuard-Anything": "x"})
    r = run(go())
    assert r.status_code == 200
    req = up.seen[0]
    assert req.url.raw_path == b"/api/read?key=a%2Fb"
    assert "x-admin-token" not in req.headers and "x-tenantguard-anything" not in req.headers


# ---------- (2) control token ----------
def test_admin_and_control_need_the_control_token():
    gw, app, up = make([tenant("A")], mode="tenantguard")

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            out = {
                "admin_none": await c.get("/admin/tenants"),
                "admin_wrong": await c.get("/admin/tenants", headers={H_CONTROL_TOKEN: "nope"}),
                "control_none": await c.post("/control/block", json={"tenant": "A", "ttl_s": 30}),
                "mode_none": await c.post("/admin/mode", json={"mode": "off"}),
                "admin_ok": await c.get("/admin/tenants", headers={H_CONTROL_TOKEN: "dev"}),
                "api": await c.get("/api/light", headers=KEY),            # data plane: API key only
                "health": await c.get("/healthz"),
            }
            return out
    o = run(go())
    for k in ("admin_none", "admin_wrong", "control_none", "mode_none"):
        assert o[k].status_code == 401, k
    assert o["admin_ok"].status_code == 200 and o["api"].status_code == 200 and o["health"].status_code == 200
    assert gw.mode == "tenantguard" and gw.overrides.blocks == {}       # unauthenticated calls changed nothing


def test_control_token_from_settings():
    gw, app, up = make([tenant("A")], control_token="s3cret")

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            return (await c.get("/admin/mode", headers={H_CONTROL_TOKEN: "dev"}),
                    await c.get("/admin/mode", headers={H_CONTROL_TOKEN: "s3cret"}))
    a, b = run(go())
    assert a.status_code == 401 and b.status_code == 200


# ---------- (3) ttl bounds ----------
def _snapshot_with_ttl(ttl: str) -> str:
    raw = snapshot([tenant("K")]).model_dump_json()
    return raw.replace('"limits":[]', '"limits":[{"tenant":"K","factor":0.5,"weight_factor":1.0,"ttl_s":%s}]' % ttl)


@pytest.mark.parametrize("ttl", ["Infinity", "-Infinity", "NaN", "0", "-1", "3600.5", "1e308"])
def test_ttl_must_be_finite_and_bounded(ttl):
    gw, app, up = make([tenant("K")], mode="tenantguard")
    block = '{"tenant": "K", "ttl_s": %s}' % ttl

    async def go():
        async with client(app) as c:
            r = await c.post("/control/snapshot", content=_snapshot_with_ttl(ttl),
                             headers={"content-type": "application/json"})
            rb = await c.post("/control/block", content=block, headers={"content-type": "application/json"})
            ov = await c.get("/admin/overrides")
            return r, rb, ov
    r, rb, ov = run(go())
    assert r.status_code == 400 and rb.status_code == 400
    assert ov.status_code == 200 and ov.json()["expires_at"] == {}


def test_ttl_at_max_is_accepted():
    gw, app, up = make([tenant("K")], mode="tenantguard")

    async def go():
        async with client(app) as c:
            return await c.post("/control/snapshot", content=_snapshot_with_ttl("3600"),
                                headers={"content-type": "application/json"})
    assert run(go()).status_code == 200


# ---------- (5) key + Content-Length before the body; capped streaming read ----------
def test_no_key_is_401_before_reading_a_huge_body():
    gw, app, up = make([tenant("A")])
    reads = {"n": 0}

    async def gen():
        for _ in range(64):
            reads["n"] += 1
            yield b"x" * 65536

    async def go():
        async with client(app) as c:
            return await c.post("/api/write", content=gen())
    r = run(go())
    assert r.status_code == 401
    assert up.seen == []
    assert reads["n"] <= 1          # the 4 MB body was never read (at most the first chunk was pulled)


def test_content_length_over_limit_is_413_without_forwarding():
    gw, app, up = make([tenant("A")], body_limit_bytes=1000)

    async def go():
        async with client(app) as c:
            ok = await c.post("/api/write", content=b"x" * 1000, headers=KEY)
            big = await c.post("/api/write", content=b"x" * 1001, headers=KEY)
            return ok, big
    ok, big = run(go())
    assert ok.status_code == 200 and big.status_code == 413
    assert len(up.seen) == 1


def test_chunked_body_over_limit_is_413():
    gw, app, up = make([tenant("A")], body_limit_bytes=1000)

    async def gen():
        for _ in range(10):
            yield b"y" * 200                 # no Content-Length: 2000 bytes streamed

    async def go():
        async with client(app) as c:
            return await c.post("/api/write", content=gen(), headers=KEY)
    r = run(go())
    assert r.status_code == 413 and up.seen == []


# ---------- (6) X-Request-Id ----------
def test_request_id_validated_and_forwarded_once():
    assert valid_request_id("abc-123") and not valid_request_id("a b") and not valid_request_id("x" * 65)
    gw, app, up = make([tenant("A")])

    async def go():
        async with client(app) as c:
            good = await c.get("/api/light", headers={**KEY, "X-Request-Id": "trace-42"})
            bad = await c.get("/api/light", headers={**KEY, "X-Request-Id": "evil\" id; drop"})
            return good, bad
    good, bad = run(go())
    assert good.headers["x-request-id"] == "trace-42"
    assert up.seen[0].headers.get_list("x-request-id") == ["trace-42"]
    assert bad.headers["x-request-id"] != "evil\" id; drop" and valid_request_id(bad.headers["x-request-id"])
    assert len(up.seen[1].headers.get_list("x-request-id")) == 1
    assert len(good.headers.get_list("x-request-id")) == 1


# ---------- (7) a client that disconnects mid-flight releases its slot and counters ----------
def test_client_disconnect_releases_slot_and_counters():
    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, headers={"X-TenantGuard-Cpu-Ms": "1"})

    from tenantguard_gateway.core import Gateway
    from tenantguard_gateway.settings import GatewaySettings
    gw = Gateway(GatewaySettings(default_mode="industry"), client=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    gw.on_snapshot(snapshot([tenant("A")]))

    async def body(limit):
        return b""

    async def go():
        t = asyncio.create_task(gw.handle("GET", "/api/heavy", "", {}, {"x-api-key": "key-A"}, body))
        await asyncio.sleep(0.05)
        assert gw.registry.get("A").inflight == 1
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    run(go())
    st = gw.registry.get("A")
    assert st.inflight == 0 and st.pending == 0 and set(gw.slots.values()) == {0}
