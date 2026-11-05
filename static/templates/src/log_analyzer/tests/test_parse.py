import gzip
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.parse import open_log, parse_line, read  # noqa: E402


def test_combined_line_with_request_time():
    h = parse_line('1.2.3.4 - - [05/Nov/2026:10:00:01 +0000] "GET /a?x=1 HTTP/1.1" 200 512 "-" "ua" 0.25')
    assert (h.ip, h.method, h.path, h.status, h.size, h.seconds) == ("1.2.3.4", "GET", "/a", 200, 512, 0.25)


def test_common_line_without_extras_and_dash_size():
    h = parse_line('1.2.3.4 - bob [05/Nov/2026:10:00:01 +0000] "HEAD / HTTP/1.0" 304 -')
    assert h.status == 304 and h.size == 0 and h.seconds is None


def test_garbage_and_bad_dates_are_counted_not_guessed():
    lines = ["nope", '1.2.3.4 - - [99/Foo/2026:10:00:01 +0000] "GET / HTTP/1.1" 200 1', ""]
    assert read(lines) == ([], 2)


def test_gzip_files_open(tmp_path):
    p = tmp_path / "a.log.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write('1.2.3.4 - - [05/Nov/2026:10:00:01 +0000] "GET / HTTP/1.1" 200 1\n')
    with open_log(str(p)) as f:
        hits, bad = read(f)
    assert len(hits) == 1 and bad == 0
