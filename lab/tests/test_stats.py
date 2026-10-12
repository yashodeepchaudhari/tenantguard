import pytest

from tenantguard_lab.stats import (CONN_ERROR, TIMEOUT, Recorder, Sample, bucketize, percentile, summarize,
                                   time_to_recover)


def test_percentile_matches_numpy_linear():
    xs = list(range(1, 101))            # numpy.percentile(range(1,101), q) values
    assert percentile(xs, 50) == pytest.approx(50.5)
    assert percentile(xs, 95) == pytest.approx(95.05)
    assert percentile(xs, 99) == pytest.approx(99.01)
    assert percentile(xs, 0) == 1
    assert percentile(xs, 100) == 100


def test_percentile_unsorted_small_and_empty():
    assert percentile([30, 10, 20], 50) == 20
    assert percentile([10, 20], 25) == pytest.approx(12.5)
    assert percentile([7.0], 99) == 7.0
    assert percentile([], 99) == 0.0
    with pytest.raises(ValueError):
        percentile([1, 2], 101)


def test_p99_is_dominated_by_tail():
    xs = [1.0] * 990 + [100.0] * 10      # 1% slow requests
    assert percentile(xs, 50) == 1.0
    assert percentile(xs, 99) == pytest.approx(1.99, abs=0.01)   # 99th pct sits at the boundary
    xs = [1.0] * 980 + [100.0] * 20      # 2% slow -> p99 is slow
    assert percentile(xs, 99) == 100.0


def test_summarize_counts_outcomes():
    s = summarize([Sample(0, 5, 200, "calm"), Sample(0, 1, 429, "calm"), Sample(0, 9, 503, "calm"),
                   Sample(0, 10000, TIMEOUT, "calm"), Sample(0, 1, CONN_ERROR, "calm"), Sample(0, 3, 304, "calm")])
    assert (s.count, s.ok, s.r429, s.r503, s.other_err) == (6, 2, 1, 1, 2)
    assert s.max_ms == 10000
    d = s.as_dict(window_s=2)
    assert d["ok_per_s"] == 1.0 and d["count_per_s"] == 3.0


def test_recorder_window_source_and_retention():
    r = Recorder(retention_s=10)
    for t in range(20):
        r.add("A", Sample(float(t), 1.0, 200, "calm" if t % 2 else "attack"))
    assert len(r.window("A", 0, 100)) == 11          # 9..19 kept (cutoff = 19 - 10)
    assert all(s.src == "calm" for s in r.window("A", 0, 100, src="calm"))
    assert r.totals["A"]["count"] == 20               # totals are cumulative, not pruned
    r.drop("A")
    assert r.dropped["A"] == 1


def test_bucketize():
    samples = [Sample(100.2, 1, 200, "calm"), Sample(100.9, 2, 200, "calm"), Sample(102.5, 3, 200, "calm"),
               Sample(103.0, 9, 200, "calm")]             # t1 is exclusive
    assert bucketize(samples, 100.0, 103.0, 1.0) == [[1, 2], [], [3]]


def test_time_to_recover():
    ok, bad = [10.0] * 50, [500.0] * 50
    # degraded for 4 s, then good for 6 s -> recovered at t=4
    assert time_to_recover([bad] * 4 + [ok] * 6, 10.0, 1.5, 5) == (4.0, True)
    # never degraded -> 0
    assert time_to_recover([ok] * 10, 10.0, 1.5, 5) == (0.0, False)
    # attack bites late: fine 3 s, bad 2 s, fine 5 s -> recovered at t=5, not 0
    assert time_to_recover([ok] * 3 + [bad] * 2 + [ok] * 5, 10.0, 1.5, 5) == (5.0, True)
    # streak broken by a relapse -> recovery counts from the last good run
    assert time_to_recover([bad, ok, ok, bad] + [ok] * 5, 10.0, 1.5, 5) == (4.0, True)
    # never recovered
    assert time_to_recover([bad] * 10, 10.0, 1.5, 5) == (None, True)
    # empty buckets don't break a streak
    assert time_to_recover([bad, ok, [], ok, ok, ok, ok], 10.0, 1.5, 5) == (1.0, True)
