"""e2e: build a whole small project from nothing -- several modules, persistence or a server, a command line -- with its own tests.

These are the tasks that answer "can it really build something end to end?". Each states its public interface exactly, as a person
would in a ticket, and is then judged by a HIDDEN acceptance check the model never sees: it imports the package, drives it the way a user
would (over HTTP, through the command line, across a restart), and asserts the behaviour. The model's own tests must also exist and pass.

Every task carries a reference solution (so the oracle proves the hidden check can be met) and a deliberately broken one (so it proves
the hidden check actually catches a mistake).
"""
FLOW = "build"
DEFAULT_N = 4

# ------------------------------------------------------------------ 1. URL shortener with a file store
SHORT_GOAL = (
    "Build a Python package `shortener` (a folder shortener/ with __init__.py) for a URL shortener, with tests in tests/. "
    "Public interface, exactly: `from shortener import Shortener`; `s = Shortener(path)` stores its data in the JSON file at `path` "
    "(created if missing); `s.shorten(url)` returns a 6-character code made of letters and digits, and the same url always gets the "
    "same code; `s.resolve(code)` returns the url, or raises KeyError for an unknown code; shorten() raises ValueError unless the url "
    "starts with http:// or https://. A second Shortener(path) on the same file must resolve codes made by the first (it persists). "
    "Standard library only. Split it into at least two modules (for example the store and the code generation)."
)
SHORT_HIDDEN = r'''
import os, tempfile
from shortener import Shortener
d = tempfile.mkdtemp(); p = os.path.join(d, "s.json")
s = Shortener(p)
c = s.shorten("https://example.com/a")
assert isinstance(c, str) and len(c) == 6 and c.isalnum(), c
assert s.shorten("https://example.com/a") == c, "same url must give the same code"
c2 = s.shorten("http://example.com/b")
assert c2 != c and s.resolve(c2) == "http://example.com/b"
for bad in ("ftp://x", "example.com", ""):
    try:
        s.shorten(bad); raise SystemExit("accepted a bad url: %r" % bad)
    except ValueError:
        pass
try:
    s.resolve("zzzzzz"); raise SystemExit("unknown code did not raise KeyError")
except KeyError:
    pass
s2 = Shortener(p)
assert s2.resolve(c) == "https://example.com/a", "codes must survive a restart"
assert s2.shorten("https://example.com/a") == c
print("ok")
'''
SHORT_SOLUTION = {
    "shortener/__init__.py": '"""A URL shortener with a JSON file store."""\nfrom .core import Shortener\n\n__all__ = ["Shortener"]\n',
    "shortener/store.py": '''"""The JSON file the codes live in."""
import json
import os


class Store:
    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def get(self, code):
        return self.data[code]

    def find(self, url):
        for code, u in self.data.items():
            if u == url:
                return code
        return None

    def put(self, code, url):
        self.data[code] = url
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)
''',
    "shortener/codes.py": '''"""Short codes derived from the url, so the same url always gets the same code."""
import hashlib
import string

ALPHABET = string.ascii_letters + string.digits


def code_for(url, salt=0):
    n = int(hashlib.sha256(f"{salt}:{url}".encode()).hexdigest(), 16)
    out = []
    for _ in range(6):
        n, r = divmod(n, len(ALPHABET))
        out.append(ALPHABET[r])
    return "".join(out)
''',
    "shortener/core.py": '''"""The shortener itself."""
from .codes import code_for
from .store import Store


class Shortener:
    def __init__(self, path):
        self.store = Store(path)

    def shorten(self, url):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("only http:// and https:// urls can be shortened")
        known = self.store.find(url)
        if known:
            return known
        salt = 0
        while True:
            code = code_for(url, salt)
            if code not in self.store.data:
                self.store.put(code, url)
                return code
            salt += 1

    def resolve(self, code):
        return self.store.get(code)
''',
    "tests/test_shortener.py": '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from shortener import Shortener


def test_round_trip_and_persistence(tmp_path):
    p = str(tmp_path / "s.json")
    c = Shortener(p).shorten("https://a.example")
    assert Shortener(p).resolve(c) == "https://a.example"


def test_rejects_bad_urls(tmp_path):
    with pytest.raises(ValueError):
        Shortener(str(tmp_path / "s.json")).shorten("ftp://x")


def test_unknown_code(tmp_path):
    with pytest.raises(KeyError):
        Shortener(str(tmp_path / "s.json")).resolve("nope00")
''',
}
SHORT_WRONG = dict(SHORT_SOLUTION)
SHORT_WRONG["shortener/store.py"] = SHORT_SOLUTION["shortener/store.py"].replace(
    "        if os.path.isfile(path):\n            with open(path, encoding=\"utf-8\") as f:\n                self.data = json.load(f)\n", "")

# ------------------------------------------------------------------ 2. a JSON API over HTTP
TODO_GOAL = (
    "Build a Python package `todo_api` (folder todo_api/ with __init__.py) that serves a to-do list as JSON over HTTP using only the "
    "standard library (http.server), with tests in tests/. Public interface, exactly: `from todo_api import make_server`; "
    "`srv = make_server(port)` returns a ready http.server server bound to 127.0.0.1 on that port (0 means pick a free one; the caller "
    "runs srv.serve_forever() in a thread). Endpoints: GET /todos -> 200 and a JSON list; POST /todos with JSON {\"title\": \"...\"} -> "
    "201 and the new item {\"id\": int, \"title\": str, \"done\": false}; a missing or empty title -> 400; PATCH /todos/<id> with JSON "
    "{\"done\": true} -> 200 and the updated item; DELETE /todos/<id> -> 204; any unknown id -> 404. Ids start at 1 and increase. "
    "Keep the routing and the storage in separate modules."
)
TODO_HIDDEN = r'''
import json, threading, urllib.request, urllib.error
from todo_api import make_server
srv = make_server(0); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read(); return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
try:
    assert call("GET", "/todos") == (200, [])
    st, a = call("POST", "/todos", {"title": "write tests"})
    assert st == 201 and a == {"id": 1, "title": "write tests", "done": False}, (st, a)
    st, b = call("POST", "/todos", {"title": "ship"})
    assert st == 201 and b["id"] == 2
    assert call("POST", "/todos", {"title": ""})[0] == 400
    assert call("POST", "/todos", {})[0] == 400
    st, u = call("PATCH", "/todos/1", {"done": True})
    assert st == 200 and u["done"] is True and u["title"] == "write tests", (st, u)
    assert call("DELETE", "/todos/2")[0] == 204
    st, items = call("GET", "/todos")
    assert st == 200 and [x["id"] for x in items] == [1], items
    assert call("DELETE", "/todos/99")[0] == 404
    assert call("PATCH", "/todos/99", {"done": True})[0] == 404
finally:
    srv.shutdown(); srv.server_close()
print("ok")
'''
TODO_SOLUTION = {
    "todo_api/__init__.py": '"""A to-do list served as JSON over HTTP."""\nfrom .server import make_server\n\n__all__ = ["make_server"]\n',
    "todo_api/store.py": '''"""In-memory storage for the to-do items."""
import threading


class Store:
    def __init__(self):
        self.items = {}
        self.next_id = 1
        self.lock = threading.Lock()

    def all(self):
        with self.lock:
            return [dict(v) for _, v in sorted(self.items.items())]

    def add(self, title):
        with self.lock:
            item = {"id": self.next_id, "title": title, "done": False}
            self.items[self.next_id] = item
            self.next_id += 1
            return dict(item)

    def update(self, item_id, done):
        with self.lock:
            item = self.items[item_id]
            item["done"] = bool(done)
            return dict(item)

    def delete(self, item_id):
        with self.lock:
            del self.items[item_id]
''',
    "todo_api/server.py": '''"""Routing: turns HTTP requests into store calls."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import Store

ITEM = re.compile(r"^/todos/(\\d+)$")


def make_server(port=0):
    store = Store()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None

        def do_GET(self):
            if self.path == "/todos":
                return self._send(200, store.all())
            self._send(404)

        def do_POST(self):
            if self.path != "/todos":
                return self._send(404)
            body = self._body()
            title = body.get("title") if isinstance(body, dict) else None
            if not isinstance(title, str) or not title.strip():
                return self._send(400, {"error": "title is required"})
            self._send(201, store.add(title))

        def do_PATCH(self):
            m = ITEM.match(self.path)
            body = self._body()
            if not m or not isinstance(body, dict):
                return self._send(404 if not m else 400)
            try:
                self._send(200, store.update(int(m.group(1)), body.get("done", False)))
            except KeyError:
                self._send(404)

        def do_DELETE(self):
            m = ITEM.match(self.path)
            if not m:
                return self._send(404)
            try:
                store.delete(int(m.group(1)))
                self._send(204)
            except KeyError:
                self._send(404)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
''',
    "tests/test_api.py": '''import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from todo_api import make_server


def test_create_and_list():
    srv = make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        req = urllib.request.Request(base + "/todos", data=json.dumps({"title": "x"}).encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            assert r.status == 201
        with urllib.request.urlopen(base + "/todos") as r:
            assert json.loads(r.read())[0]["title"] == "x"
    finally:
        srv.shutdown()
        srv.server_close()
''',
}
TODO_WRONG = dict(TODO_SOLUTION)
TODO_WRONG["todo_api/server.py"] = TODO_SOLUTION["todo_api/server.py"].replace(
    '            if not isinstance(title, str) or not title.strip():\n                return self._send(400, {"error": "title is required"})\n', "")

# ------------------------------------------------------------------ 3. a command-line tool with a data file
INV_GOAL = (
    "Build a command-line inventory tool as a Python package `inventory` (folder inventory/ with __init__.py and __main__.py) using "
    "argparse, with tests in tests/. It is run as `python -m inventory --db PATH <command>`, where PATH is a JSON file (created if "
    "missing). Commands: `add NAME QTY` adds QTY (a positive integer) to NAME; `remove NAME QTY` takes QTY away, and if that is more "
    "than there is it prints an error to stderr, changes nothing and exits with status 2; `list` prints one line per item, sorted by "
    "name, as `NAME QTY`; `list --json` prints a JSON object {name: qty}. An item that reaches 0 is removed. Successful commands exit 0. "
    "Keep the storage separate from the command-line parsing."
)
INV_HIDDEN = r'''
import json, os, subprocess, sys, tempfile
db = os.path.join(tempfile.mkdtemp(), "inv.json")
def run(*a):
    p = subprocess.run([sys.executable, "-m", "inventory", "--db", db, *a], capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr
assert run("add", "pear", "2")[0] == 0
assert run("add", "apple", "3")[0] == 0
assert run("add", "apple", "4")[0] == 0
rc, out, _ = run("list")
assert rc == 0 and out.strip().splitlines() == ["apple 7", "pear 2"], out
rc, out, err = run("remove", "pear", "5")
assert rc == 2 and err.strip(), (rc, err)
rc, out, _ = run("list", "--json")
assert rc == 0 and json.loads(out) == {"apple": 7, "pear": 2}, out
assert run("remove", "pear", "2")[0] == 0
rc, out, _ = run("list", "--json")
assert json.loads(out) == {"apple": 7}, out
print("ok")
'''
INV_SOLUTION = {
    "inventory/__init__.py": '"""A small inventory kept in a JSON file."""\n',
    "inventory/store.py": '''"""Reading and writing the inventory file."""
import json
import os


def load(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, items):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, sort_keys=True)
    os.replace(tmp, path)


def add(items, name, qty):
    items[name] = items.get(name, 0) + qty
    return items


def remove(items, name, qty):
    have = items.get(name, 0)
    if qty > have:
        raise ValueError(f"only {have} {name} in stock, cannot remove {qty}")
    if have - qty == 0:
        del items[name]
    else:
        items[name] = have - qty
    return items
''',
    "inventory/__main__.py": '''"""python -m inventory --db PATH add|remove|list"""
import argparse
import json
import sys

from . import store


def positive(s):
    n = int(s)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be a positive whole number")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(prog="inventory")
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("add", "remove"):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("qty", type=positive)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    items = store.load(a.db)
    if a.cmd == "add":
        store.save(a.db, store.add(items, a.name, a.qty))
    elif a.cmd == "remove":
        try:
            store.save(a.db, store.remove(items, a.name, a.qty))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    elif a.json:
        print(json.dumps(items, sort_keys=True))
    else:
        for k in sorted(items):
            print(f"{k} {items[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
    "tests/test_inventory.py": '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from inventory import store


def test_add_accumulates():
    assert store.add({"a": 1}, "a", 2) == {"a": 3}


def test_remove_too_many_raises():
    with pytest.raises(ValueError):
        store.remove({"a": 1}, "a", 2)


def test_remove_to_zero_drops_item():
    assert store.remove({"a": 2}, "a", 2) == {}
''',
}
INV_WRONG = dict(INV_SOLUTION)
INV_WRONG["inventory/store.py"] = INV_SOLUTION["inventory/store.py"].replace(
    "    if have - qty == 0:\n        del items[name]\n    else:\n        items[name] = have - qty\n", "    items[name] = have - qty\n")

# ------------------------------------------------------------------ 4. a data pipeline with validation and a report
REPORT_GOAL = (
    "Build a Python package `salesreport` (folder salesreport/ with __init__.py and __main__.py), with tests in tests/, that turns a "
    "sales CSV into a Markdown report. The CSV has a header row `date,region,amount` (date as YYYY-MM-DD, amount a number). Public "
    "interface, exactly: `from salesreport import build_report`; `build_report(csv_text)` returns a dict {\"totals\": {region: total}, "
    "\"months\": {\"YYYY-MM\": total}, \"rejected\": [(line_number, reason), ...], \"markdown\": str}. A row with a bad date, a "
    "non-numeric amount or a missing field is rejected with its 1-based line number in the file (the header is line 1) and is NOT "
    "counted. Totals are rounded to 2 decimals. The markdown contains a table with a `| Region | Total |` header and one row per "
    "region sorted by name. `python -m salesreport IN.csv OUT.md` writes the markdown and exits 0. Keep parsing, aggregation and "
    "rendering in separate modules."
)
REPORT_HIDDEN = r'''
import os, subprocess, sys, tempfile
from salesreport import build_report
csv_text = "date,region,amount\n2024-01-05,north,10.50\n2024-01-20,south,4\n2024-02-01,north,2.25\nbad-date,north,5\n2024-02-03,east,abc\n2024-02-04,,3\n2024-02-05,south,1.01\n"
r = build_report(csv_text)
assert r["totals"] == {"north": 12.75, "south": 5.01}, r["totals"]
assert r["months"] == {"2024-01": 14.5, "2024-02": 3.26}, r["months"]
assert [ln for ln, _ in r["rejected"]] == [5, 6, 7], r["rejected"]
md = r["markdown"]
assert "| Region | Total |" in md and md.index("north") < md.index("south"), md
d = tempfile.mkdtemp(); i = os.path.join(d, "in.csv"); o = os.path.join(d, "out.md")
open(i, "w").write(csv_text)
p = subprocess.run([sys.executable, "-m", "salesreport", i, o], capture_output=True, text=True, timeout=60)
assert p.returncode == 0 and "| Region | Total |" in open(o).read(), p.stderr
print("ok")
'''
REPORT_SOLUTION = {
    "salesreport/__init__.py": '"""Sales CSV to a Markdown report."""\nfrom .report import build_report\n\n__all__ = ["build_report"]\n',
    "salesreport/parse.py": '''"""Reading and validating the rows."""
import csv
import datetime
import io


def rows(csv_text):
    """-> (good rows as (date, region, amount), rejected as (line, reason))."""
    good, bad = [], []
    reader = csv.DictReader(io.StringIO(csv_text))
    for i, row in enumerate(reader, start=2):
        date, region, amount = (row.get("date") or "").strip(), (row.get("region") or "").strip(), (row.get("amount") or "").strip()
        if not date or not region or not amount:
            bad.append((i, "missing field"))
            continue
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            bad.append((i, f"bad date {date!r}"))
            continue
        try:
            value = float(amount)
        except ValueError:
            bad.append((i, f"amount is not a number: {amount!r}"))
            continue
        good.append((date, region, value))
    return good, bad
''',
    "salesreport/aggregate.py": '''"""Totals per region and per month."""


def totals(rows):
    by_region, by_month = {}, {}
    for date, region, amount in rows:
        by_region[region] = by_region.get(region, 0.0) + amount
        by_month[date[:7]] = by_month.get(date[:7], 0.0) + amount
    return ({k: round(v, 2) for k, v in by_region.items()}, {k: round(v, 2) for k, v in by_month.items()})
''',
    "salesreport/render.py": '''"""The Markdown report."""


def markdown(by_region, by_month, rejected):
    out = ["# Sales report", "", "| Region | Total |", "|---|---:|"]
    out += [f"| {r} | {by_region[r]:.2f} |" for r in sorted(by_region)]
    out += ["", "| Month | Total |", "|---|---:|"]
    out += [f"| {m} | {by_month[m]:.2f} |" for m in sorted(by_month)]
    if rejected:
        out += ["", f"{len(rejected)} row(s) rejected:"] + [f"- line {ln}: {why}" for ln, why in rejected]
    return "\\n".join(out) + "\\n"
''',
    "salesreport/report.py": '''"""Putting it together."""
from .aggregate import totals
from .parse import rows
from .render import markdown


def build_report(csv_text):
    good, bad = rows(csv_text)
    by_region, by_month = totals(good)
    return {"totals": by_region, "months": by_month, "rejected": bad, "markdown": markdown(by_region, by_month, bad)}
''',
    "salesreport/__main__.py": '''import sys

from .report import build_report

if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as f:
        r = build_report(f.read())
    with open(dst, "w", encoding="utf-8") as f:
        f.write(r["markdown"])
''',
    "tests/test_report.py": '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from salesreport import build_report


def test_totals_and_rejects():
    r = build_report("date,region,amount\\n2024-01-01,a,1\\nnope,a,2\\n")
    assert r["totals"] == {"a": 1.0}
    assert [ln for ln, _ in r["rejected"]] == [3]
''',
}
REPORT_WRONG = dict(REPORT_SOLUTION)
REPORT_WRONG["salesreport/parse.py"] = REPORT_SOLUTION["salesreport/parse.py"].replace("start=2", "start=1")

PROJECTS = [
    ("shortener", SHORT_GOAL, SHORT_HIDDEN, SHORT_SOLUTION, SHORT_WRONG, ["shortener/__init__.py"]),
    ("todo-api", TODO_GOAL, TODO_HIDDEN, TODO_SOLUTION, TODO_WRONG, ["todo_api/__init__.py"]),
    ("inventory-cli", INV_GOAL, INV_HIDDEN, INV_SOLUTION, INV_WRONG, ["inventory/__main__.py"]),
    ("sales-report", REPORT_GOAL, REPORT_HIDDEN, REPORT_SOLUTION, REPORT_WRONG, ["salesreport/__main__.py"]),
]


def generate(n, seed=0):
    out = []
    for i, (name, goal, hidden, sol, wrong, must) in enumerate(PROJECTS[:max(1, n)]):
        out.append({
            "id": f"e2e-{name}-{i:02d}", "tags": ["e2e", "multi-file", "from-nothing"], "goal": goal,
            "params": {"domains": ["api"]}, "fixture": {"files": {}},
            "budget": {"calls": 40, "tokens": 160000, "seconds": 2400},
            "judge": [{"type": "file_exists", "paths": must + ["tests/test_*.py"]},
                      {"type": "python_assert", "code": hidden, "timeout": 120},
                      {"type": "tests_pass"}],
            "solution": {"files": sol},
            "wrong": {"files": wrong},
        })
    return out
