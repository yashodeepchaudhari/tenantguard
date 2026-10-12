from __future__ import annotations

from conftest import T0, stats

from tenantguard_billing.meter import Meter


def usage(m: Meter, tenant: str):
    out = {"requests": 0, "cpu": 0.0, "bg": 0.0, "db": 0.0}
    for _, u in m.tenant_buckets(tenant, 0, 1e12):
        out["requests"] += u.requests
        out["cpu"] += u.handler_cpu_ms
        out["bg"] += u.bg_cpu_ms
        out["db"] += u.db_ms
    return out


def test_diffs_cumulative_counters_into_buckets():
    m = Meter(60, started_at=T0)
    m.ingest(stats("w0", T0 + 1, T0 - 100, {"A": {"requests": 50, "cpu": 40.0}}))   # baseline (history before billing)
    m.ingest(stats("w0", T0 + 30, T0 - 100, {"A": {"requests": 70, "cpu": 60.0, "bg": 3, "db": 2}}))
    m.ingest(stats("w0", T0 + 90, T0 - 100, {"A": {"requests": 100, "cpu": 100.0, "bg": 5, "db": 4}}))
    assert usage(m, "A") == {"requests": 50, "cpu": 60.0, "bg": 5.0, "db": 4.0}
    assert [b for b, _ in m.tenant_buckets("A", 0, 1e12)] == [T0, T0 + 60]


def test_worker_restart_counts_new_counters_from_zero():
    m = Meter(60, started_at=T0)
    m.ingest(stats("w0", T0 + 1, T0 - 100, {"A": {"requests": 500, "cpu": 400.0}}))
    m.ingest(stats("w0", T0 + 10, T0 - 100, {"A": {"requests": 510, "cpu": 410.0}}))
    # restart: started_at changes and counters fall back
    m.ingest(stats("w0", T0 + 20, T0 + 15, {"A": {"requests": 7, "cpu": 6.0}}))
    m.ingest(stats("w0", T0 + 30, T0 + 15, {"A": {"requests": 9, "cpu": 8.0}}))
    assert usage(m, "A")["requests"] == 10 + 7 + 2
    assert usage(m, "A")["cpu"] == 10.0 + 6.0 + 2.0
    assert m.restarts["w0"] == 1


def test_worker_started_after_billing_counts_from_its_start():
    m = Meter(60, started_at=T0)
    m.ingest(stats("w5", T0 + 40, T0 + 30, {"B": {"requests": 4, "cpu": 3.0}}))
    assert usage(m, "B") == {"requests": 4, "cpu": 3.0, "bg": 0.0, "db": 0.0}


def test_counter_drop_without_restart_is_a_reset_and_duplicates_ignored():
    m = Meter(60, started_at=T0)
    m.ingest(stats("w0", T0 + 1, T0 - 5, {"A": {"requests": 10, "cpu": 10.0}}))   # baseline (pre-billing history)
    m.ingest(stats("w0", T0 + 1, T0 - 5, {"A": {"requests": 99, "cpu": 99.0}}))   # same ts: ignored
    m.ingest(stats("w0", T0 + 2, T0 - 5, {"A": {"requests": 3, "cpu": 2.0}}))     # went down: reset
    m.ingest(stats("w0", T0 + 3, T0 - 5, {"A": {"requests": 5, "cpu": 4.0}}))
    assert usage(m, "A") == {"requests": 5, "cpu": 4.0, "bg": 0.0, "db": 0.0}
    assert all(u.requests >= 0 and u.handler_cpu_ms >= 0 for _, u in m.tenant_buckets("A", 0, 1e12))


def test_servers_sum_and_classes_diffed():
    m = Meter(60, started_at=T0)
    for srv in ("w0", "w1"):
        m.ingest(stats(srv, T0 + 1, T0 - 5, {"A": {}}, {("A", "c1"): (0, 0.0)}))
        m.ingest(stats(srv, T0 + 5, T0 - 5, {"A": {"requests": 3, "cpu": 6.0}}, {("A", "c1"): (3, 6.0)}))
    [(_, u)] = m.tenant_buckets("A", 0, 1e12)
    assert u.requests == 6 and u.classes == {"c1": [6, 12.0]}


def test_blackbox_prorated_over_window():
    m = Meter(60, started_at=T0)
    m.ingest(stats("w1", T0, T0 - 5, {}, blackbox=0))
    m.ingest(stats("w1", T0 + 100, T0 - 5, {}, blackbox=1000.0))
    cpu, secs = m.blackbox_ms(["w1"], T0 + 50, T0 + 75)
    assert cpu == 250.0 and secs == 25.0
    assert m.blackbox_ms(["w2"], T0, T0 + 100) == (0.0, 0.0)
