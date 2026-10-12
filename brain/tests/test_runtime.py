"""Runtime pieces without a network: send ordering, start-up adoption, control token, default mode."""
import asyncio

import httpx

from tenantguard_common.contracts import H_CONTROL_TOKEN, LimitsCmd, OverridesView

from tenantguard_brain.core import Brain
from tenantguard_brain.runtime import Runtime, Sender

from .conftest import T0


async def test_sender_one_in_flight_per_key_and_latest_wins():
    sent = []
    gate = asyncio.Event()

    async def post(path, model):
        sent.append(model.factor)
        if model.factor == 0.5:
            await gate.wait()                  # the first send is slow
        return True

    done = []
    s = Sender(post)
    mk = lambda f: LimitsCmd(tenant="K", factor=f, weight_factor=1.0)    # noqa: E731
    s.submit("limits:K", "/control/limits", mk(0.5), lambda m, ok: done.append(m.factor))
    await asyncio.sleep(0)
    for f in (0.25, 0.35, 0.45):                # queued while 0.5 is in flight: only the latest survives
        s.submit("limits:K", "/control/limits", mk(f), lambda m, ok: done.append(m.factor))
    assert len(s.running) == 1
    gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert sent == [0.5, 0.45] and done == [0.5, 0.45]
    assert s.running == {}                      # task references released when done


async def test_sender_keys_are_independent():
    order = []

    async def post(path, model):
        order.append(model.tenant)
        await asyncio.sleep(0)
        return True

    s = Sender(post)
    for t in "ABC":
        s.submit(f"limits:{t}", "/control/limits", LimitsCmd(tenant=t), lambda m, ok: None)
    assert len(s.running) == 3
    await asyncio.sleep(0.01)
    assert sorted(order) == ["A", "B", "C"] and s.running == {}


async def test_control_token_header_is_sent(cfg):
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.path, req.headers.get(H_CONTROL_TOKEN)))
        return httpx.Response(200, json={"ok": True})

    rt = Runtime(cfg)
    rt.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await rt._post("/control/limits", LimitsCmd(tenant="K", factor=0.5))
    await rt.client.aclose()
    assert seen == [("/control/limits", cfg.control_token)]


async def test_refused_send_reports_false(cfg):
    rt = Runtime(cfg)
    rt.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert not await rt._post("/control/limits", LimitsCmd(tenant="K", factor=0.5))
    await rt.client.aclose()


def test_adopt_leftover_overrides(cfg):
    brain = Brain(cfg)
    ov = OverridesView(limits={
        "K": LimitsCmd(tenant="K", factor=0.25, weight_factor=0.25, incident_id="inc-old"),
        "ZZ": LimitsCmd(tenant="ZZ", factor=0.5),                     # not a tenant we know
    })
    events, clears = brain.adopt_overrides(ov, T0)
    k = brain.ladders["K"]
    assert k.level == "deprioritize" and k.factor == 0.25 and k.weight_factor == 0.25
    assert events and events[0].tenant == "K" and "Adopted" in events[0].detail
    assert [c.tenant for c in clears] == ["ZZ"] and clears[0].factor == 1.0 and clears[0].weight_factor == 1.0
    assert brain.sent["K"].ok                                     # renewed from here on, not re-sent at once
    assert brain._commands(T0) == []
    # a tenant this run already manages is not overwritten
    brain.ladders["A"].factor = 0.5
    brain.ladders["A"].level = "throttle"
    brain.adopt_overrides(OverridesView(limits={"A": LimitsCmd(tenant="A", factor=0.1, weight_factor=0.1)}), T0)
    assert brain.ladders["A"].factor == 0.5


def test_adopted_override_recovers_when_calm(cfg):
    from tenantguard_brain.sim import Phase, Simulator
    from .conftest import drive
    brain = Brain(cfg)
    brain.adopt_overrides(OverridesView(limits={"K": LimitsCmd(tenant="K", factor=0.5)}), T0)
    sim = Simulator(cfg.tenants, [Phase("calm", 60)], start=T0)
    events, commands = drive(brain, sim, T0 + 60)
    assert brain.ladders["K"].level == "normal" and brain.ladders["K"].factor == 1.0
    assert commands[-1][1].factor == 1.0


def test_mode_before_first_frame_is_the_yaml_default(cfg):
    brain = Brain(cfg)
    assert brain.snapshot(T0).mode == cfg.default_mode == "tenantguard"
    assert brain.snapshot(T0).noisy_thresholds == {"warn": cfg.ladder.warn, "throttle": cfg.ladder.throttle,
                                                   "quarantine": cfg.ladder.quarantine,
                                                   "recover_below": cfg.ladder.recover_below}
