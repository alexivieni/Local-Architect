import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.__main__ import main  # noqa: E402
from __PKG__.parse import Hit  # noqa: E402
from __PKG__.stats import percentile, spikes, summarize  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = datetime(2026, 11, 5, 10, tzinfo=timezone.utc)


def test_nearest_rank_percentiles():
    assert percentile([5, 1, 3, 2, 4], 50) == 3 and percentile([1, 2, 3, 4], 95) == 4 and percentile([], 50) is None


def test_spike_minute_is_found():
    hits = [Hit("i", T0 + timedelta(minutes=m), "GET", "/", 200, 1) for m in range(5) for _ in range(2)]
    hits += [Hit("i", T0 + timedelta(minutes=2), "GET", "/", 200, 1)] * 10
    assert spikes(hits) == ["2026-11-05 10:02"]


def test_summary_of_the_sample(capsys):
    assert main([os.path.join(ROOT, "samples", "access.log"), "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["requests"] == 6 and r["malformed_lines"] == 1
    assert r["status"] == {"2xx": 4, "4xx": 1, "5xx": 1} and r["top_paths"][0] == ["/api/items", 4]
    assert r["latency_s"]["p99"] == 1.2


def test_empty_input_is_not_a_crash():
    assert summarize([])["error_rate"] == 0.0


def test_missing_file_exits_1():
    assert main(["no/such/file.log"]) == 1
