"""DockerProver / HostProver with a fake Docker and scripted readings; the Engine API client; the switch."""
import asyncio
import json

import httpx
import pytest

from tenantguard_brain.prover import (DockerProver, EngineDocker, HostCheck, HostProver, NullProver, ProofRequest,
                                      ProverParams, Readings, make_prover)
from tenantguard_brain.verdicts import VerdictParams

SERVERS = ["w1", "w3", "w8"]


class FakeDocker:
    def __init__(self, cores=1.0, fail=False):
        self.cores, self.fail, self.calls = cores, fail, []

    async def get_cpus(self, container):
        return self.cores

    async def set_cpus(self, container, cores):
        if self.fail:
            raise RuntimeError("no docker")
        self.calls.append((container, round(cores, 3)))
        self.cores = cores


class ScriptEnv:
    """Readings follow a script: `before` until the relocation is sent, then `after` (a function of time)."""

    def __init__(self, before: Readings, after, accept=True):
        self.before, self.after, self.accept = before, after, accept
        self.t, self.sent, self.relocated_at = 0.0, [], None

    def readings(self):
        if self.relocated_at is None:
            return self.before
        return self.after(self.t - self.relocated_at)

    async def send(self, path, model):
        self.sent.append((path, model))
        if path.endswith("/relocate") and self.accept:
            self.relocated_at = self.t
        return self.accept

    async def sleep(self, s):
        self.t += s


def R(slow, spare_cpu=0.0, spare_ratio=1.0, ratio=None):
    sd = {s: slow for s in SERVERS}
    pr = {s: 1.0 + slow for s in SERVERS}
    pr["spare"] = spare_ratio
    if ratio:
        pr.update(ratio)
    return Readings(slowdown=sd, peer_ratio=pr, cpu_ms={"spare": spare_cpu})


def req(fp=800.0):
    return ProofRequest(incident_id="srv-1", tenant="K", servers=SERVERS, confidence=0.95, footprint_cpu_ms=fp)


def prover(docker=None, **kw):
    return DockerProver(docker or FakeDocker(), ProverParams(**kw), VerdictParams(), container="codeastra-spare-1")


def run(coro):
    return asyncio.run(coro)


def test_confirmed_when_servers_recover_and_harm_follows():
    d = FakeDocker(cores=1.0)
    env = ScriptEnv(R(1.5, spare_cpu=10), lambda dt: R(0.1, spare_cpu=850, spare_ratio=1.3))
    res = run(prover(d).prove(req(800), env))
    assert res.confirmed and res.harm_followed and res.recovery_R > 0.9 and res.relocated
    assert d.calls == [("codeastra-spare-1", 1.0)]               # 1.25 x 800 ms/s = 1.0 CPU
    assert res.spare_cores_before == 1.0
    path, cmd = env.sent[0]
    assert path == "/control/relocate" and cmd.tenant == "K" and cmd.target == "spare" and cmd.ttl_s > 0
    assert env.t == pytest.approx(2.0 + 6.0)                      # settle + window
    assert "followed" in res.detail


def test_not_confirmed_when_servers_stay_slow():
    env = ScriptEnv(R(1.5), lambda dt: R(1.4, spare_cpu=900))
    res = run(prover().prove(req(), env))
    assert not res.confirmed and res.recovery_R < 0.5 and res.harm_followed


def test_culprit_pausing_mid_test_is_not_confirmed():
    # it stops 1 s after the move: its servers recover, but the spare never sees the harm
    env = ScriptEnv(R(1.5), lambda dt: R(0.1, spare_cpu=900 if dt < 1.0 else 5.0))
    res = run(prover().prove(req(), env))
    assert not res.confirmed and not res.harm_followed and "paused" in res.detail


def test_spare_sizing_is_clamped():
    d = FakeDocker()
    env = ScriptEnv(R(1.0), lambda dt: R(0.0, spare_cpu=50000))
    run(prover(d, max_spare_cores=2.0).prove(req(50000), env))
    assert d.calls[0][1] == 2.0
    d2 = FakeDocker()
    run(prover(d2, min_spare_cores=0.25).prove(req(10), ScriptEnv(R(1.0), lambda dt: R(0.0, spare_cpu=10))))
    assert d2.calls[0][1] == 0.25


def test_docker_failure_still_tests_on_the_spare_as_is():
    env = ScriptEnv(R(1.5), lambda dt: R(0.1, spare_cpu=900))
    res = run(prover(FakeDocker(fail=True)).prove(req(), env))
    assert res.confirmed and "could not resize" in res.detail and res.spare_cores_before == 1.0


def test_refused_relocation_restores_the_spare():
    d = FakeDocker(cores=1.0)
    env = ScriptEnv(R(1.5), lambda dt: R(0.1), accept=False)
    res = run(prover(d).prove(req(800), env))
    assert not res.confirmed and not res.relocated and "refused" in res.detail
    assert d.calls[-1] == ("codeastra-spare-1", 1.0)               # put back


def test_nothing_to_prove_when_servers_already_fine():
    d = FakeDocker()
    res = run(prover(d).prove(req(), ScriptEnv(R(0.0), lambda dt: R(0.0))))
    assert not res.confirmed and d.calls == [] and "nothing to prove" in res.detail


def test_restore_spare():
    d = FakeDocker()
    p = prover(d)
    run(p.restore_spare(0.5))
    run(p.restore_spare(None))
    assert d.calls == [("codeastra-spare-1", 0.5)]


def test_null_prover():
    p = NullProver()
    assert not p.available
    res = run(p.prove(req(), ScriptEnv(R(1.0), lambda dt: R(0.0))))
    assert not res.confirmed and "not available" in res.detail


def test_host_prover_confirms_a_slow_idle_host():
    env = ScriptEnv(R(0.0, ratio={"w4": 6.0}), lambda dt: R(0.0, ratio={"w4": 6.0}))
    env.relocated_at = 0.0
    hp = HostProver(ProverParams(), VerdictParams())
    res = run(hp.check(HostCheck(incident_id="srv-1", server="w4"), env))
    assert res.drained and res.idle_ratio == pytest.approx(6.0)
    path, cmd = env.sent[0]
    assert path == "/control/drain" and cmd.server == "w4" and cmd.drained and cmd.ttl_s > 0
    assert run(hp.undrain("w4", env)) is True and env.sent[-1][1].drained is False


def test_host_prover_drain_refused():
    env = ScriptEnv(R(0.0), lambda dt: R(0.0), accept=False)
    res = run(HostProver(ProverParams(), VerdictParams()).check(HostCheck(incident_id="i", server="w4"), env))
    assert not res.drained and "refused" in res.detail


# ------------------------------------------------------------------ Engine API client
def engine(host_config):
    seen = []

    def handler(r: httpx.Request):
        seen.append((r.method, r.url.path, json.loads(r.content) if r.content else None))
        if r.url.path.endswith("/json"):
            return httpx.Response(200, json={"HostConfig": host_config})
        return httpx.Response(200, json={"Warnings": []})

    e = EngineDocker.__new__(EngineDocker)
    e.socket_path, e.service, e.resolved = "test", "spare", {}
    e.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://docker/v1.41")
    return e, seen


def test_engine_uses_nano_cpus_for_compose_cpus():
    e, seen = engine({"NanoCpus": 1_000_000_000, "CpuQuota": 0, "CpuPeriod": 0})
    assert run(e.get_cpus("codeastra-spare-1")) == 1.0
    run(e.set_cpus("codeastra-spare-1", 1.5))
    assert seen[-1] == ("POST", "/v1.41/containers/codeastra-spare-1/update", {"NanoCpus": 1_500_000_000})


def test_engine_uses_cpu_quota_when_the_container_has_one():
    e, seen = engine({"NanoCpus": 0, "CpuQuota": 50_000, "CpuPeriod": 100_000})
    assert run(e.get_cpus("c")) == 0.5
    run(e.set_cpus("c", 0.75))
    assert seen[-1][2] == {"CpuQuota": 75_000, "CpuPeriod": 100_000}


def test_make_prover_switch(tmp_path, monkeypatch):
    p = ProverParams()
    v = VerdictParams()
    assert isinstance(run(make_prover("null", p, v)), NullProver)
    monkeypatch.setenv("TENANTGUARD_DOCKER_SOCKET", str(tmp_path / "missing.sock"))
    assert isinstance(run(make_prover("auto", p, v)), NullProver)            # no socket -> null, no error
    with pytest.raises(RuntimeError):
        run(make_prover("docker", p, v))                                     # explicitly asked: fail loudly


def test_engine_finds_the_spare_by_compose_label_when_the_name_differs():
    seen = []

    def handler(r: httpx.Request):
        seen.append((r.method, r.url.path, r.url.params.get("filters")))
        if r.url.path == "/v1.41/containers/codeastra-spare-1/json":
            return httpx.Response(404, json={"message": "No such container"})
        if r.url.path == "/v1.41/containers/json":
            return httpx.Response(200, json=[{"Id": "abc123"}])
        if r.url.path == "/v1.41/containers/abc123/json":
            return httpx.Response(200, json={"HostConfig": {"NanoCpus": 500_000_000}})
        return httpx.Response(200, json={})

    e = EngineDocker.__new__(EngineDocker)
    e.socket_path, e.service, e.resolved = "test", "spare", {}
    e.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://docker/v1.41")
    assert run(e.get_cpus("codeastra-spare-1")) == 0.5
    run(e.set_cpus("codeastra-spare-1", 1.0))
    assert seen[-1][:2] == ("POST", "/v1.41/containers/abc123/update")
    assert any("com.docker.compose.service=spare" in (f or "") for _, _, f in seen)
