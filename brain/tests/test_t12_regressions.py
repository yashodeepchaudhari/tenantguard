"""T12 regressions: failed requests are not slot usage (T9's L false flag)."""
from tenantguard_common.contracts import Cell, QueueStat, TelemetryFrame, TenantUsage, WorkerStats

from tenantguard_brain.core import Brain

from .conftest import T0


def frame(brain, now, *, l_down: bool, hurt: float):
    cells, queues = [], []
    for t, cfg in brain.tenants.items():
        n_total = 0
        for s in cfg.servers:
            if t == "L" and l_down:
                # every L server unreachable: fast 502s
                cells.append(Cell(tenant=t, server=s, count=3, p50_ms=0.8, p95_ms=1.0, errors=3))
                n_total += 3
                continue
            p95 = 10.0 * (hurt if t in brain.neighbours["L"] else 1.0)
            cells.append(Cell(tenant=t, server=s, count=2, p50_ms=p95 / 2, p95_ms=p95))
            n_total += 2
        busy = t == "L" and l_down
        queues.append(QueueStat(tenant=t, depth=3 if busy else 0, wait_p95_ms=1.0,
                                p95_ms=1.0 if busy else (10.0 * (hurt if t in brain.neighbours["L"] else 1.0)),
                                completed=n_total, admitted=n_total, rejected_429=0, expired_503=0,
                                inflight=6 if busy else 0))
    return TelemetryFrame(ts=now, mode="tenantguard", cells=cells, queues=queues)


def stats(brain, now, k):
    out = []
    for s in brain.cfg.fleet_servers:
        ts = [TenantUsage(tenant=t, requests=k, handler_cpu_ms=(0.0 if t == "L" else 20.0 * k), bg_cpu_ms=0.0,
                          db_ms=0.0) for t, c in brain.tenants.items() if s in c.servers]
        out.append(WorkerStats(server=s, ts=now, started_at=T0 - 10, cpu_total_ms=60.0 * k, mem_mb=1,
                               tenants=ts))
    return out


def run(brain, start, secs, **kw):
    now = start
    for i in range(int(secs * 4)):
        now += 0.25
        brain.ingest_frame(frame(brain, now, **kw), now)
        if i % 4 == 0:
            for st in stats(brain, now, int(now - T0)):
                brain.ingest_stats(st, now)
        brain.tick(now)
    return now


def test_failed_requests_are_not_slot_usage(cfg):
    brain = Brain(cfg)
    now = run(brain, T0, 30, l_down=False, hurt=1.0)          # calm baseline
    now = run(brain, now, 30, l_down=True, hurt=2.0)          # L's servers down; L's neighbours slow anyway
    l = brain.scores["L"]
    assert l.queue_share == 0.0 and l.cpu_share == 0.0
    assert not l.flagged and l.score < 1.0
    assert brain.ladders["L"].level == "normal"
    assert not [e for e in brain.book.events(tenant="L") if e.incident_id.startswith("inc-")]


# ------------------------------------------------------------------ bg CPU charge
def bg_stats(server, now, k, bg_per_s):
    return WorkerStats(server=server, ts=now, started_at=T0 - 10, cpu_total_ms=100.0 * k, mem_mb=1,
                       tenants=[TenantUsage(tenant="K", requests=k, handler_cpu_ms=10.0 * k,
                                            bg_cpu_ms=bg_per_s * k, db_ms=0.0)])


def test_bg_cpu_is_charged_once_a_second_in_tenantguard_mode(cfg):
    brain = Brain(cfg)
    now = T0
    charges = []
    for i in range(1, 25):                      # 6 s at 4 Hz
        now += 0.25
        brain.ingest_frame(TelemetryFrame(ts=now, mode="tenantguard"), now)
        if i % 4 == 0:
            for s in ("w1", "w3"):
                brain.ingest_stats(bg_stats(s, now, i // 4, 30.0), now)
        charges += [c for _, path, c in brain.tick(now).control if path == "/control/charge"]
    assert charges and all(c.tenant == "K" and c.cpu_ms > 0 for c in charges)
    assert len({c.charge_id for c in charges}) == len(charges)              # unique ids
    assert all(c.charge_id.startswith("K-") for c in charges)
    # every bg ms is charged exactly once (the last second's is still pending when the loop ends)
    assert abs(sum(c.cpu_ms for c in charges) + brain.bg_pending["K"] - 2 * 30.0 * 5) < 1e-6
    brain.bg_pending.clear()
    # a failed delivery is folded into the next charge, not lost
    brain.charge_failed(charges[-1])
    assert brain.bg_pending["K"] == charges[-1].cpu_ms


def test_no_charge_outside_tenantguard_mode(cfg):
    brain = Brain(cfg)
    now = T0
    for i in range(1, 13):
        now += 0.25
        brain.ingest_frame(TelemetryFrame(ts=now, mode="industry"), now)
        if i % 4 == 0:
            brain.ingest_stats(bg_stats("w1", now, i // 4, 30.0), now)
        assert not [c for _, p, c in brain.tick(now).control if p == "/control/charge"]
