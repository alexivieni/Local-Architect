"""Eight more project starters for local AI and distributed work, registered into arch_templates.TEMPLATES on import
(`import arch_templates_extra`, at the end of arch_templates.py next to arch_templates_more).

Same rules as arch_templates.py: each starter is a small, working, tested project as plain data (README with how to
run it, pyproject.toml, a package, tests/), standard library only, nothing leaves the machine (every server binds
127.0.0.1; the model clients talk to a local Ollama-style /api/chat and are tested against a loopback stand-in).
Placeholders: __NAME__ (display name) and __PKG__ (python identifier, also used in paths). Every test file puts the
project root on sys.path.

    tool_server  agent_fleet  lease_queue  api_gateway  event_store  model_cli  telemetry_collector  fts_rag

File texts are raw strings (r'''...'''): what you read here is byte for byte what lands in the project.
"""
import re

import arch_templates as T
from arch_templates import _t, COMMON, KEYWORDS

EXTRA_IDS = ("tool_server", "agent_fleet", "lease_queue", "api_gateway", "event_store", "model_cli",
             "telemetry_collector", "fts_rag")

PYPROJECT = '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
TEST_HEAD = 'import os\nimport sys\n\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n'


# ================================================================================================ tool_server
TS_TOOLS = r'''"""The tools this server offers. A tool is a plain function with type hints and a docstring, registered with
@tool. The hints become the argument schema; the docstring is what a model reads when it picks a tool."""
import inspect
import os

REGISTRY = {}
_JSON = {int: "integer", float: "number", str: "string", bool: "boolean", list: "array", dict: "object"}


def tool(fn):
    REGISTRY[fn.__name__] = fn
    return fn


def schema(fn):
    props, required = {}, []
    for name, p in inspect.signature(fn).parameters.items():
        props[name] = {"type": _JSON.get(p.annotation, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
        else:
            props[name]["default"] = p.default
    return {"type": "object", "properties": props, "required": required}


def listing():
    return [{"name": n, "description": inspect.getdoc(fn) or "", "input_schema": schema(fn)}
            for n, fn in sorted(REGISTRY.items())]


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def word_count(text: str) -> dict:
    """Count the words and lines in a piece of text."""
    return {"words": len(text.split()), "lines": len(text.splitlines())}


@tool
def read_text(path: str, limit: int = 2000) -> str:
    """Read the start of a text file under the tool root (TOOL_ROOT, default: the current folder)."""
    root = os.path.realpath(os.environ.get("TOOL_ROOT", "."))
    full = os.path.realpath(os.path.join(root, path))
    try:
        inside = os.path.commonpath([root, full]) == root
    except ValueError:              # another drive on Windows
        inside = False
    if not inside:
        raise PermissionError(f"{path!r} is outside the tool root")
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        return f.read(max(0, min(limit, 20000)))
'''

TS_DISPATCH = r'''"""One call, checked in code before and after it runs: a known tool, arguments that match its schema, a time limit
and an output cap. Every outcome is a dict, never an exception, so the caller (often a model) gets a reason it can
act on instead of a stack trace."""
import concurrent.futures
import json
import time

from . import tools

TIMEOUT_S = 10.0
OUTPUT_CAP = 8000
_TYPES = {"integer": int, "number": (int, float), "string": str, "boolean": bool, "array": list, "object": dict}
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")


def check_args(name, args):
    """-> list of problems in plain words (empty = the call may run)."""
    fn = tools.REGISTRY.get(name)
    if fn is None:
        return [f"unknown tool {name!r} (known: {', '.join(sorted(tools.REGISTRY))})"]
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]
    sch = tools.schema(fn)
    problems = [f"missing argument {k!r}" for k in sch["required"] if k not in args]
    for k, v in args.items():
        spec = sch["properties"].get(k)
        if spec is None:
            problems.append(f"unexpected argument {k!r}")
            continue
        is_bool_as_number = isinstance(v, bool) and spec["type"] in ("integer", "number")
        if is_bool_as_number or not isinstance(v, _TYPES[spec["type"]]):
            problems.append(f"argument {k!r} should be {spec['type']}, got {type(v).__name__}")
    return problems


def call(name, args, timeout=None, log=None):
    """-> {"ok": bool, "result" or "error": str, "ms": int}. A tool that runs past the limit is reported, not awaited."""
    limit = TIMEOUT_S if timeout is None else timeout
    started = time.monotonic()
    problems = check_args(name, args)
    if problems:
        out = {"ok": False, "error": "; ".join(problems)}
    else:
        future = _POOL.submit(tools.REGISTRY[name], **args)
        try:
            value = future.result(timeout=limit)
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            out = {"ok": True, "result": text[:OUTPUT_CAP], "truncated": len(text) > OUTPUT_CAP}
        except concurrent.futures.TimeoutError:
            out = {"ok": False, "error": f"timed out after {limit:g}s"}
        except Exception as e:      # a tool's own failure becomes a reason for the caller
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["ms"] = int((time.monotonic() - started) * 1000)
    if log is not None:
        log.append({"tool": name, "ok": out["ok"], "ms": out["ms"]})
    return out
'''

TS_SERVER = r'''"""The tools over HTTP on loopback: GET /tools lists them, POST /call runs one, GET /health counts the calls.
JSON in, JSON out. A refused or failed call answers 422 with the reason."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import dispatch, tools

MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "__PKG__/0.1"

    def log_message(self, fmt, *args):
        pass                        # quiet: the server's call log keeps what matters

    def _send(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/tools":
            return self._send(200, {"tools": tools.listing()})
        if self.path == "/health":
            return self._send(200, {"ok": True, "calls": len(self.server.call_log)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/call":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return self._send(413, {"error": f"body larger than {MAX_BODY} bytes"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            return self._send(400, {"error": 'expected {"name": ..., "arguments": {...}}'})
        out = dispatch.call(body["name"], body.get("arguments", {}), log=self.server.call_log)
        return self._send(200 if out["ok"] else 422, out)


def make_server(host="127.0.0.1", port=0):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.call_log = []
    return srv
'''

TS_MAIN = r'''import os
import sys

from .server import make_server


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else int(os.environ.get("TOOL_PORT", "8765"))
    srv = make_server(port=port)
    print(f"tools on http://127.0.0.1:{srv.server_address[1]}  (GET /tools, POST /call, GET /health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        srv.server_close()
    return 0


sys.exit(main())
'''

TS_TEST_DISPATCH = TEST_HEAD + r'''
import time  # noqa: E402

from __PKG__ import dispatch, tools  # noqa: E402


def test_listing_has_schemas_from_type_hints():
    names = {t["name"]: t for t in tools.listing()}
    assert {"add", "word_count", "read_text"} <= set(names)
    s = names["read_text"]["input_schema"]
    assert s["required"] == ["path"] and s["properties"]["limit"] == {"type": "integer", "default": 2000}
    assert names["add"]["description"] == "Add two integers."


def test_good_call_returns_text_and_is_logged():
    log = []
    out = dispatch.call("add", {"a": 2, "b": 3}, log=log)
    assert out["ok"] and out["result"] == "5" and log == [{"tool": "add", "ok": True, "ms": out["ms"]}]


def test_bad_arguments_are_refused_before_running():
    assert dispatch.check_args("add", {"a": 1}) == ["missing argument 'b'"]
    assert "should be integer, got str" in dispatch.check_args("add", {"a": "1", "b": 2})[0]
    assert "should be integer, got bool" in dispatch.check_args("add", {"a": True, "b": 2})[0]
    assert dispatch.check_args("add", {"a": 1, "b": 2, "c": 3}) == ["unexpected argument 'c'"]
    assert dispatch.check_args("add", [1, 2]) == ["arguments must be a JSON object"]
    out = dispatch.call("nope", {})
    assert not out["ok"] and out["error"].startswith("unknown tool 'nope'")


def test_a_failing_tool_is_a_reason_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_ROOT", str(tmp_path))
    out = dispatch.call("read_text", {"path": "../outside.txt"})
    assert not out["ok"] and out["error"].startswith("PermissionError")
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    assert dispatch.call("read_text", {"path": "a.txt", "limit": 5})["result"] == "hello"


def test_a_slow_tool_times_out_instead_of_hanging_the_caller():
    def slow(seconds: float) -> str:
        time.sleep(seconds)
        return "late"
    tools.REGISTRY["slow"] = slow
    try:
        started = time.monotonic()
        out = dispatch.call("slow", {"seconds": 1.0}, timeout=0.05)
        took = time.monotonic() - started
    finally:
        tools.REGISTRY.pop("slow")
    assert not out["ok"] and "timed out" in out["error"] and took < 0.9


def test_output_is_capped(monkeypatch):
    monkeypatch.setattr(dispatch, "OUTPUT_CAP", 10)
    out = dispatch.call("word_count", {"text": "a b c"})
    assert out["ok"] and len(out["result"]) == 10 and out["truncated"]
'''

TS_TEST_SERVER = TEST_HEAD + r'''
import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402

from __PKG__ import server  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # loopback only, never a proxy


@pytest.fixture
def base():
    srv = server.make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", srv
    srv.shutdown()
    srv.server_close()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_list_and_call_over_real_http(base):
    url, srv = base
    with OPENER.open(url + "/tools", timeout=5) as r:
        names = [t["name"] for t in json.loads(r.read())["tools"]]
    assert "add" in names and "read_text" in names
    code, out = post(url + "/call", {"name": "add", "arguments": {"a": 20, "b": 22}})
    assert code == 200 and out["result"] == "42"
    assert srv.call_log[-1]["tool"] == "add"


def test_refusals_over_real_http(base):
    url, _ = base
    code, out = post(url + "/call", {"name": "add", "arguments": {"a": 1}})
    assert code == 422 and out["error"] == "missing argument 'b'"
    assert post(url + "/call", {"arguments": {}})[0] == 400
    assert post(url + "/elsewhere", {})[0] == 404
'''

_t('tool_server', 'Local tool server (HTTP, typed tools)',
   'Tools a local model can call, served over loopback HTTP: the schema comes from type hints, every call is checked in code (known tool, argument types, a time limit, an output cap, a path fence) and answers with a reason instead of a crash. Tests talk to a real server on a free port.',
   'python / http.server', ['api'], {
       **COMMON,
       'README.md': '# __NAME__\n\nTools a local model can call, over plain HTTP on 127.0.0.1. A tool is one function in `__PKG__/tools.py`\nwith type hints and a docstring; the argument schema is derived from the hints.\n\n    python -m __PKG__            # serves on 127.0.0.1:8765 (or TOOL_PORT, or the first argument)\n    python -m pytest -q\n\n| request | answer |\n|---------|--------|\n| `GET /tools` | every tool: name, description, `input_schema` |\n| `POST /call` `{"name": "add", "arguments": {"a": 1, "b": 2}}` | `{"ok": true, "result": "3", "ms": 0}` |\n| `GET /health` | `{"ok": true, "calls": N}` |\n\nEvery call is checked in code before it runs: the tool must exist, required arguments must be present, types\nmust match (a JSON `true` is not an integer), and unknown arguments are refused. A call that runs past\n`TIMEOUT_S` is answered "timed out" instead of hanging the caller; output is capped at `OUTPUT_CAP` characters;\n`read_text` refuses paths outside `TOOL_ROOT`. A refused or failed call answers HTTP 422 with the reason, so a\nmodel can correct itself.\n',
       'pyproject.toml': PYPROJECT,
       '__PKG__/__init__.py': '"""__NAME__: typed tools over loopback HTTP."""\n',
       '__PKG__/__main__.py': TS_MAIN,
       '__PKG__/tools.py': TS_TOOLS,
       '__PKG__/dispatch.py': TS_DISPATCH,
       '__PKG__/server.py': TS_SERVER,
       'tests/test_dispatch.py': TS_TEST_DISPATCH,
       'tests/test_server.py': TS_TEST_SERVER,
   }, 'python -m __PKG__')


# ================================================================================================ agent_fleet
AF_PLAN = r'''"""The plan check: refuse a fleet before anything runs. Every brief names the files it owns; no two briefs may own
the same file, or a folder and a file inside it; every brief has a step budget and an agent."""
import posixpath


def norm(path):
    """A workspace-relative path with forward slashes, no leading slash, '..' kept so it can be refused."""
    return posixpath.normpath(str(path).replace("\\", "/")).lstrip("/")


def overlaps(a, b):
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def check_plan(briefs):
    """-> list of problems in plain words (empty = the plan may run)."""
    problems, names, owned = [], set(), []
    for i, b in enumerate(briefs):
        name = b.get("name")
        if not name:
            problems.append(f"brief {i} has no name")
            name = f"brief {i}"
        elif name in names:
            problems.append(f"two briefs are called {name!r}")
        names.add(name)
        files = b.get("owns") or []
        if not files:
            problems.append(f"{name}: owns no files")
        steps = b.get("max_steps")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            problems.append(f"{name}: needs a max_steps budget of at least 1")
        for f in files:
            p = norm(f)
            if p in ("", ".") or p == ".." or p.startswith("../"):
                problems.append(f"{name}: {f!r} is outside the workspace")
                continue
            for other, q in owned:
                if other != name and overlaps(p, q):
                    problems.append(f"{name} and {other} both own {min(p, q, key=len)!r}")
            owned.append((name, p))
    return problems
'''

AF_WORKSPACE = r'''"""The only door an agent has to the disk: it may write the files its brief owns and nothing else. Reads are free
(an agent may look at the whole project); every write is recorded."""
import os

from .plan import norm


class OutsideBrief(PermissionError):
    """An agent tried to write a file its brief does not own."""


class Workspace:
    def __init__(self, root, owns):
        self.root = os.path.abspath(root)
        self.owns = [norm(f) for f in owns]
        self.written = []

    def owned(self, rel):
        p = norm(rel)
        if p == ".." or p.startswith("../"):
            return False
        return any(p == o or p.startswith(o.rstrip("/") + "/") for o in self.owns)

    def read(self, rel):
        with open(os.path.join(self.root, norm(rel)), "r", encoding="utf-8") as f:
            return f.read()

    def write(self, rel, text):
        if not self.owned(rel):
            raise OutsideBrief(f"{rel!r} is not in this brief (owns: {', '.join(self.owns)})")
        full = os.path.join(self.root, norm(rel))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        if norm(rel) not in self.written:
            self.written.append(norm(rel))
        return full
'''

AF_FLEET = r'''"""Run several agents at once. An agent is a function agent(brief, workspace, step) -> {"done": bool, "ok": True |
False | None, "note": str}, called once per step until it says done or its max_steps run out.

Three outcomes, never two:
    pass      the agent finished and its own check passed (ok True)
    fail      the agent finished with ok False, raised, or tried to write outside its brief
    not_sure  the agent ran out of steps or time, or finished without being able to check (ok None)
"""
import concurrent.futures
import time

from .plan import check_plan
from .workspace import OutsideBrief, Workspace

OUTCOMES = ("pass", "fail", "not_sure")


class PlanRefused(ValueError):
    def __init__(self, problems):
        super().__init__("plan refused: " + "; ".join(problems))
        self.problems = problems


def _result(brief, outcome, why, steps, ws, started, notes):
    return {"name": brief["name"], "outcome": outcome, "why": why, "steps": steps, "written": list(ws.written),
            "notes": notes, "seconds": round(time.monotonic() - started, 3)}


def run_agent(agent, brief, root):
    ws = Workspace(root, brief["owns"])
    started, notes = time.monotonic(), []
    for step in range(brief["max_steps"]):
        try:
            reply = agent(brief, ws, step) or {}
        except OutsideBrief as e:
            return _result(brief, "fail", f"wrote outside its brief: {e}", step + 1, ws, started, notes)
        except Exception as e:          # an agent's crash is its outcome, not the fleet's
            return _result(brief, "fail", f"{type(e).__name__}: {e}", step + 1, ws, started, notes)
        if reply.get("note"):
            notes.append(reply["note"])
        if reply.get("done"):
            ok = reply.get("ok")
            outcome = "pass" if ok is True else "fail" if ok is False else "not_sure"
            return _result(brief, outcome, reply.get("note", ""), step + 1, ws, started, notes)
    return _result(brief, "not_sure", f"stopped at its budget of {brief['max_steps']} steps", brief["max_steps"], ws,
                   started, notes)


def run_fleet(briefs, agents, root, max_parallel=4, timeout_s=60.0):
    """-> {"results": [...], "summary": {"pass": n, "fail": n, "not_sure": n}}. Refuses (PlanRefused) before any
    agent runs when the plan check finds a problem or a brief names an agent that does not exist."""
    problems = check_plan(briefs)
    problems += [f"{b.get('name')}: no agent called {b.get('agent')!r}" for b in briefs if b.get("agent") not in agents]
    if problems:
        raise PlanRefused(problems)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_parallel), thread_name_prefix="agent")
    futures = [(pool.submit(run_agent, agents[b["agent"]], b, root), b) for b in briefs]
    deadline = time.monotonic() + timeout_s
    results = []
    for fut, b in futures:
        try:
            results.append(fut.result(timeout=max(0.0, deadline - time.monotonic())))
        except concurrent.futures.TimeoutError:
            results.append({"name": b["name"], "outcome": "not_sure", "why": f"still running after {timeout_s:g}s",
                            "steps": None, "written": [], "notes": [], "seconds": timeout_s})
    pool.shutdown(wait=False, cancel_futures=True)
    summary = {k: sum(r["outcome"] == k for r in results) for k in OUTCOMES}
    return {"results": results, "summary": summary}
'''

AF_AGENTS = r'''"""Two example agents. Real ones would ask a model for each step; these are plain code so the fleet can be tried
and tested offline. Swap them for your own: same signature, same reply shape."""


def scribe(brief, ws, step):
    """Writes each owned file (one per step) with a heading from the brief's task, then reads them back as its check."""
    todo = [f for f in ws.owns if f not in ws.written]
    if todo:
        ws.write(todo[0], f"# {brief.get('task', 'notes')}\n")
        return {"done": False, "note": f"wrote {todo[0]}"}
    ok = all(ws.read(f).startswith("# ") for f in ws.owns)
    return {"done": True, "ok": ok, "note": f"{len(ws.owns)} file(s) written and read back"}


def counter(brief, ws, step):
    """Counts the words in brief['inputs'] (read-only) and writes the totals to its one owned file. A missing input
    means it could not check anything: ok None, not a failure."""
    totals = {}
    for rel in brief.get("inputs", []):
        try:
            totals[rel] = len(ws.read(rel).split())
        except FileNotFoundError:
            return {"done": True, "ok": None, "note": f"{rel} is missing, so nothing could be counted"}
    ws.write(ws.owns[0], "".join(f"{k}: {v}\n" for k, v in sorted(totals.items())))
    return {"done": True, "ok": bool(totals), "note": f"counted {len(totals)} file(s)"}


AGENTS = {"scribe": scribe, "counter": counter}
'''

AF_MAIN = r'''import json
import os
import sys
import tempfile

from .agents import AGENTS
from .fleet import PlanRefused, run_fleet

BRIEFS = [
    {"name": "docs", "agent": "scribe", "task": "Release notes", "owns": ["docs/notes.md", "docs/changes.md"], "max_steps": 5},
    {"name": "stats", "agent": "counter", "inputs": ["input.txt"], "owns": ["out/stats.txt"], "max_steps": 1},
]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0] if argv else tempfile.mkdtemp(prefix="fleet_")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "input.txt"), "w", encoding="utf-8") as f:
        f.write("three small words\n")
    try:
        report = run_fleet(BRIEFS, AGENTS, root)
    except PlanRefused as e:
        print(e, file=sys.stderr)
        return 2
    for r in report["results"]:
        print(f"{r['outcome']:>9}  {r['name']}: {r['why']}  (wrote {', '.join(r['written']) or 'nothing'})")
    print("workspace:", root)
    print(json.dumps(report["summary"]))
    return 0 if report["summary"]["fail"] == 0 else 1


sys.exit(main())
'''

AF_TEST_PLAN = TEST_HEAD + r'''
from __PKG__.plan import check_plan  # noqa: E402


def brief(name, owns, steps=3):
    return {"name": name, "agent": "scribe", "owns": owns, "max_steps": steps}


def test_a_clean_plan_has_no_problems():
    assert check_plan([brief("a", ["src/a.py"]), brief("b", ["src/b.py", "docs/"])]) == []


def test_two_briefs_may_not_share_a_file():
    problems = check_plan([brief("a", ["src/a.py"]), brief("b", ["./src/a.py"])])
    assert problems == ["b and a both own 'src/a.py'"]


def test_a_folder_and_a_file_inside_it_overlap():
    problems = check_plan([brief("a", ["src"]), brief("b", ["src\\deep\\x.py"])])
    assert problems == ["b and a both own 'src'"]


def test_budget_name_and_paths_are_checked():
    problems = check_plan([brief("a", ["x.py"], steps=0), brief("a", ["y.py"]), {"owns": ["../up.py"], "max_steps": 1},
                           brief("c", [])])
    assert "a: needs a max_steps budget of at least 1" in problems
    assert "two briefs are called 'a'" in problems
    assert "brief 2 has no name" in problems and "brief 2: '../up.py' is outside the workspace" in problems
    assert "c: owns no files" in problems
'''

AF_TEST_FLEET = TEST_HEAD + r'''
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from __PKG__.agents import AGENTS  # noqa: E402
from __PKG__.fleet import PlanRefused, run_fleet  # noqa: E402


def by_name(report):
    return {r["name"]: r for r in report["results"]}


def test_example_agents_pass_and_write_only_their_files(tmp_path):
    (tmp_path / "in.txt").write_text("one two three", encoding="utf-8")
    briefs = [{"name": "docs", "agent": "scribe", "task": "Notes", "owns": ["docs/a.md", "docs/b.md"], "max_steps": 5},
              {"name": "stats", "agent": "counter", "inputs": ["in.txt"], "owns": ["out/stats.txt"], "max_steps": 1}]
    report = run_fleet(briefs, AGENTS, str(tmp_path))
    r = by_name(report)
    assert report["summary"] == {"pass": 2, "fail": 0, "not_sure": 0}
    assert r["docs"]["written"] == ["docs/a.md", "docs/b.md"] and r["docs"]["steps"] == 3
    assert (tmp_path / "out" / "stats.txt").read_text(encoding="utf-8") == "in.txt: 3\n"


def test_three_outcomes_never_two(tmp_path):
    def fails(brief, ws, step):
        return {"done": True, "ok": False, "note": "my check failed"}

    def unsure(brief, ws, step):
        return {"done": True, "ok": None, "note": "could not run my check"}

    def never_done(brief, ws, step):
        return {"done": False}

    agents = {"fails": fails, "unsure": unsure, "never_done": never_done}
    briefs = [{"name": n, "agent": n, "owns": [n + ".txt"], "max_steps": 2} for n in agents]
    r = by_name(run_fleet(briefs, agents, str(tmp_path)))
    assert r["fails"]["outcome"] == "fail" and r["unsure"]["outcome"] == "not_sure"
    assert r["never_done"]["outcome"] == "not_sure" and "budget of 2 steps" in r["never_done"]["why"]


def test_writing_outside_the_brief_fails_and_leaves_no_file(tmp_path):
    def trespass(brief, ws, step):
        ws.write("someone_elses.py", "x = 1\n")
        return {"done": True, "ok": True}

    def crash(brief, ws, step):
        raise RuntimeError("boom")

    briefs = [{"name": "t", "agent": "trespass", "owns": ["mine.py"], "max_steps": 1},
              {"name": "c", "agent": "crash", "owns": ["c.py"], "max_steps": 1}]
    r = by_name(run_fleet(briefs, {"trespass": trespass, "crash": crash}, str(tmp_path)))
    assert r["t"]["outcome"] == "fail" and "outside its brief" in r["t"]["why"]
    assert not (tmp_path / "someone_elses.py").exists()
    assert r["c"]["outcome"] == "fail" and r["c"]["why"] == "RuntimeError: boom"


def test_a_bad_plan_is_refused_before_any_agent_runs(tmp_path):
    ran = []

    def records(brief, ws, step):
        ran.append(brief["name"])
        return {"done": True, "ok": True}

    briefs = [{"name": "a", "agent": "records", "owns": ["x.py"], "max_steps": 1},
              {"name": "b", "agent": "records", "owns": ["x.py"], "max_steps": 1},
              {"name": "c", "agent": "missing", "owns": ["y.py"], "max_steps": 1}]
    with pytest.raises(PlanRefused) as e:
        run_fleet(briefs, {"records": records}, str(tmp_path))
    assert ran == [] and "b and a both own 'x.py'" in e.value.problems and "c: no agent called 'missing'" in e.value.problems


def test_agents_really_run_at_the_same_time(tmp_path):
    barrier = threading.Barrier(2, timeout=5)

    def meets(brief, ws, step):
        barrier.wait()          # only returns when BOTH agents are inside at once
        return {"done": True, "ok": True}

    briefs = [{"name": n, "agent": "meets", "owns": [n], "max_steps": 1} for n in ("a", "b")]
    assert run_fleet(briefs, {"meets": meets}, str(tmp_path), max_parallel=2)["summary"]["pass"] == 2


def test_an_agent_past_the_fleet_deadline_is_not_sure(tmp_path):
    def sleeps(brief, ws, step):
        time.sleep(1.0)
        return {"done": True, "ok": True}

    report = run_fleet([{"name": "s", "agent": "sleeps", "owns": ["s"], "max_steps": 1}], {"sleeps": sleeps},
                       str(tmp_path), timeout_s=0.1)
    assert report["summary"] == {"pass": 0, "fail": 0, "not_sure": 1} and "still running" in report["results"][0]["why"]
'''

_t('agent_fleet', 'Agent fleet runner (file owners, three outcomes)',
   'Run several agents at once, each owning its own files: a plan check refuses overlapping owners or missing budgets before anything runs, a workspace refuses writes outside the brief, and every agent ends pass, fail or not_sure (never collapsed to two). Example agents are plain code so it runs offline.',
   'python / concurrent.futures', ['api'], {
       **COMMON,
       'README.md': '# __NAME__\n\nSeveral agents at once, one owner per file.\n\n    python -m __PKG__ [WORKSPACE]   # runs the two example briefs; prints each outcome\n    python -m pytest -q\n\nA **brief** is `{"name", "agent", "owns": [paths or folders], "max_steps": N, ...anything the agent reads}`.\nAn **agent** is `agent(brief, workspace, step) -> {"done": bool, "ok": True | False | None, "note": str}`;\nput a model call inside it, or plain code like the examples in `__PKG__/agents.py`.\n\nWhat code guarantees:\n\n- **The plan check** (`plan.check_plan`) refuses the whole fleet before anything runs when two briefs own the\n  same file, or a folder and a file inside it, when a brief has no step budget, or names a path outside the\n  workspace.\n- **The workspace** lets an agent write only what its brief owns (`OutsideBrief` otherwise). Reads are free.\n- **Three outcomes**: `pass` (finished, its own check passed), `fail` (check failed, crashed, or wrote\n  outside its brief), `not_sure` (out of steps or time, or finished without being able to check). A\n  `not_sure` is never counted as a pass or a fail.\n',
       'pyproject.toml': PYPROJECT,
       '__PKG__/__init__.py': '"""__NAME__: several agents at once, one owner per file."""\n',
       '__PKG__/__main__.py': AF_MAIN,
       '__PKG__/plan.py': AF_PLAN,
       '__PKG__/workspace.py': AF_WORKSPACE,
       '__PKG__/fleet.py': AF_FLEET,
       '__PKG__/agents.py': AF_AGENTS,
       'tests/test_plan.py': AF_TEST_PLAN,
       'tests/test_fleet.py': AF_TEST_FLEET,
   }, 'python -m __PKG__')

# ================================================================================================ lease_queue
LQ_JOBS = r'''"""A job queue on SQLite where a claim is a LEASE: the worker gets the job for lease_s seconds and a token. Only
the holder of a live lease can finish or fail it; a worker that dies simply lets the lease run out and the job goes
back to the queue. A failure is retried after an exponential backoff; after max_attempts the job is dead and kept,
with every error, for a person to look at.

The clock is a parameter (now=...) so every timing rule is testable without sleeping.
"""
import json
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    state TEXT NOT NULL DEFAULT 'ready' CHECK (state IN ('ready', 'leased', 'done', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    available_at REAL NOT NULL,
    lease_token TEXT,
    lease_until REAL,
    result TEXT,
    errors TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (state, available_at);
"""

STATES = ("ready", "leased", "done", "dead")
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 300.0


class LeaseLost(RuntimeError):
    """The lease ran out or belongs to another worker: stop working on this job."""


def connect(path=":memory:"):
    con = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _now(now):
    return time.time() if now is None else now


def backoff(attempt):
    """Seconds to wait before attempt+1: 2, 4, 8, ... capped at BACKOFF_MAX_S."""
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** max(0, attempt - 1))


def enqueue(con, kind, payload, max_attempts=3, dedupe_key=None, delay_s=0.0, now=None):
    """-> job id. With a dedupe_key, enqueueing the same work twice returns the first job's id."""
    try:
        cur = con.execute("INSERT INTO jobs (kind, payload, dedupe_key, max_attempts, available_at) VALUES (?, ?, ?, ?, ?)",
                          (kind, json.dumps(payload), dedupe_key, max_attempts, _now(now) + delay_s))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        row = con.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        if row is None:
            raise
        return row["id"]


def _append_error(row, error):
    return json.dumps(json.loads(row["errors"]) + [error])


def _bury_expired(con, now):
    """A lease that ran out on the job's LAST attempt has nobody left to retry it: the job is dead."""
    rows = con.execute("SELECT * FROM jobs WHERE state = 'leased' AND lease_until <= ? AND attempts >= max_attempts",
                       (now,)).fetchall()
    for row in rows:
        con.execute("UPDATE jobs SET state = 'dead', lease_token = NULL, lease_until = NULL, errors = ? WHERE id = ?",
                    (_append_error(row, f"attempt {row['attempts']}: lease expired"), row["id"]))


def claim(con, lease_s=30.0, kinds=None, now=None):
    """-> {"id", "kind", "payload", "token", "attempt", "lease_until"} or None. A job whose lease ran out counts as
    ready again (its worker is presumed dead); the claim and the attempt count change in one transaction."""
    now = _now(now)
    con.execute("BEGIN IMMEDIATE")
    try:
        _bury_expired(con, now)
        sql = "SELECT * FROM jobs WHERE ((state = 'ready' AND available_at <= ?) OR (state = 'leased' AND lease_until <= ?))"
        args = [now, now]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += list(kinds)
        row = con.execute(sql + " ORDER BY available_at, id LIMIT 1", args).fetchone()
        token = None
        if row is not None:
            token = secrets.token_hex(8)
            con.execute("UPDATE jobs SET state = 'leased', lease_token = ?, lease_until = ?, attempts = attempts + 1 WHERE id = ?",
                        (token, now + lease_s, row["id"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    if row is None:
        return None
    return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]), "token": token,
            "attempt": row["attempts"] + 1, "lease_until": now + lease_s}


def _live(con, job_id, token, now):
    row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise LeaseLost(f"job {job_id} does not exist")
    if row["state"] != "leased" or row["lease_token"] != token:
        raise LeaseLost(f"job {job_id} is not leased to this worker (state {row['state']})")
    if row["lease_until"] <= now:
        raise LeaseLost(f"the lease on job {job_id} ran out")
    return row


def heartbeat(con, job_id, token, lease_s=30.0, now=None):
    """Extend a live lease. -> the new lease_until. Raises LeaseLost when the lease is gone."""
    now = _now(now)
    con.execute("BEGIN IMMEDIATE")
    try:
        _live(con, job_id, token, now)
        con.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (now + lease_s, job_id))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return now + lease_s


def complete(con, job_id, token, result=None, now=None):
    now = _now(now)
    con.execute("BEGIN IMMEDIATE")
    try:
        _live(con, job_id, token, now)
        con.execute("UPDATE jobs SET state = 'done', result = ?, lease_token = NULL, lease_until = NULL WHERE id = ?",
                    (json.dumps(result), job_id))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def fail(con, job_id, token, error, now=None):
    """-> "retry" (back in the queue after a backoff) or "dead" (out of attempts). Raises LeaseLost."""
    now = _now(now)
    con.execute("BEGIN IMMEDIATE")
    try:
        row = _live(con, job_id, token, now)
        errors = _append_error(row, f"attempt {row['attempts']}: {error}")
        if row["attempts"] >= row["max_attempts"]:
            con.execute("UPDATE jobs SET state = 'dead', errors = ?, lease_token = NULL, lease_until = NULL WHERE id = ?",
                        (errors, job_id))
            outcome = "dead"
        else:
            con.execute("UPDATE jobs SET state = 'ready', errors = ?, available_at = ?, lease_token = NULL, lease_until = NULL "
                        "WHERE id = ?", (errors, now + backoff(row["attempts"]), job_id))
            outcome = "retry"
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return outcome


def get(con, job_id):
    row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["payload"], job["errors"] = json.loads(job["payload"]), json.loads(job["errors"])
    job["result"] = json.loads(job["result"]) if job["result"] is not None else None
    return job


def counts(con):
    out = dict.fromkeys(STATES, 0)
    for row in con.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
        out[row["state"]] = row["n"]
    return out
'''

LQ_WORKER = r'''"""Runs jobs off the queue. A handler is handler(payload) -> result (JSON-able), or raises to fail the attempt."""
import time
from collections import Counter

from . import jobs


def work_one(con, handlers, lease_s=30.0, clock=time.time):
    """Claim one job and run its handler. -> "idle" | "done" | "retry" | "dead" | "lost"."""
    job = jobs.claim(con, lease_s=lease_s, now=clock())
    if job is None:
        return "idle"
    handler = handlers.get(job["kind"])
    try:
        if handler is None:
            raise LookupError(f"no handler for kind {job['kind']!r}")
        result = handler(job["payload"])
    except Exception as e:          # the job's failure is recorded on the job, never raised into the worker
        try:
            return jobs.fail(con, job["id"], job["token"], f"{type(e).__name__}: {e}", now=clock())
        except jobs.LeaseLost:
            return "lost"
    try:
        jobs.complete(con, job["id"], job["token"], result, now=clock())
    except jobs.LeaseLost:
        return "lost"           # took longer than the lease: another worker may already have it
    return "done"


def drain(con, handlers, lease_s=30.0, clock=time.time, max_jobs=1000):
    """Work until the queue has nothing ready (or max_jobs attempts). -> Counter of outcomes."""
    seen = Counter()
    for _ in range(max_jobs):
        outcome = work_one(con, handlers, lease_s, clock)
        if outcome == "idle":
            break
        seen[outcome] += 1
    return seen
'''

LQ_HANDLERS = r'''"""Example handlers. Each is handler(payload) -> result; raise to fail the attempt (the job retries)."""


def echo(payload):
    return payload


def shout(payload):
    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("payload needs a 'text' string")
    return text.upper()


HANDLERS = {"echo": echo, "shout": shout}
'''

LQ_MAIN = r'''import argparse
import json
import os
import sys

from . import jobs
from .handlers import HANDLERS
from .worker import drain


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: a job queue with leases and retries.")
    ap.add_argument("--db", default=os.environ.get("JOBS_DB", "jobs.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("enqueue", help="add a job")
    p.add_argument("kind", choices=sorted(HANDLERS))
    p.add_argument("payload", help="JSON")
    p.add_argument("--key", help="dedupe key: the same key is enqueued once")
    p.add_argument("--attempts", type=int, default=3)
    sub.add_parser("work", help="run every ready job, then stop")
    sub.add_parser("status", help="jobs per state")
    args = ap.parse_args(argv)
    con = jobs.connect(args.db)
    try:
        if args.cmd == "enqueue":
            try:
                payload = json.loads(args.payload)
            except ValueError:
                print("error: payload is not JSON", file=sys.stderr)
                return 2
            print(jobs.enqueue(con, args.kind, payload, max_attempts=args.attempts, dedupe_key=args.key))
        elif args.cmd == "work":
            print(json.dumps(dict(drain(con, HANDLERS))))
        else:
            print(json.dumps(jobs.counts(con)))
    finally:
        con.close()
    return 0


sys.exit(main())
'''

LQ_TEST_JOBS = TEST_HEAD + r'''
import pytest  # noqa: E402

from __PKG__ import jobs  # noqa: E402


@pytest.fixture
def con():
    c = jobs.connect()
    yield c
    c.close()


def test_claim_gives_a_lease_and_a_second_claim_gets_nothing(con):
    jid = jobs.enqueue(con, "echo", {"x": 1}, now=0)
    job = jobs.claim(con, lease_s=30, now=1)
    assert job["id"] == jid and job["payload"] == {"x": 1} and job["attempt"] == 1 and job["lease_until"] == 31
    assert jobs.claim(con, now=2) is None
    jobs.complete(con, jid, job["token"], {"ok": True}, now=3)
    assert jobs.get(con, jid)["state"] == "done" and jobs.get(con, jid)["result"] == {"ok": True}


def test_an_expired_lease_goes_back_and_the_old_holder_cannot_finish(con):
    jid = jobs.enqueue(con, "echo", {}, now=0)
    first = jobs.claim(con, lease_s=10, now=0)
    second = jobs.claim(con, lease_s=10, now=11)          # the first worker went quiet
    assert second["id"] == jid and second["attempt"] == 2 and second["token"] != first["token"]
    with pytest.raises(jobs.LeaseLost):
        jobs.complete(con, jid, first["token"], now=12)
    jobs.complete(con, jid, second["token"], now=12)
    assert jobs.counts(con) == {"ready": 0, "leased": 0, "done": 1, "dead": 0}


def test_heartbeat_keeps_the_lease_alive(con):
    jid = jobs.enqueue(con, "echo", {}, now=0)
    job = jobs.claim(con, lease_s=10, now=0)
    assert jobs.heartbeat(con, jid, job["token"], lease_s=10, now=8) == 18
    assert jobs.claim(con, now=15) is None
    jobs.complete(con, jid, job["token"], now=17)
    with pytest.raises(jobs.LeaseLost):
        jobs.heartbeat(con, jid, job["token"], now=17)


def test_failures_back_off_exponentially_then_die_with_every_error(con):
    jid = jobs.enqueue(con, "echo", {}, max_attempts=3, now=0)
    job = jobs.claim(con, now=0)
    assert jobs.fail(con, jid, job["token"], "first", now=0) == "retry"
    assert jobs.claim(con, now=1.9) is None                 # backoff after attempt 1 is 2 s
    job = jobs.claim(con, now=2)
    assert jobs.fail(con, jid, job["token"], "second", now=2) == "retry"
    assert jobs.claim(con, now=5.9) is None                 # after attempt 2: 4 s
    job = jobs.claim(con, now=6)
    assert jobs.fail(con, jid, job["token"], "third", now=6) == "dead"
    dead = jobs.get(con, jid)
    assert dead["state"] == "dead" and dead["errors"] == ["attempt 1: first", "attempt 2: second", "attempt 3: third"]
    assert jobs.claim(con, now=1000) is None


def test_a_lease_that_expires_on_the_last_attempt_buries_the_job(con):
    jid = jobs.enqueue(con, "echo", {}, max_attempts=1, now=0)
    jobs.claim(con, lease_s=5, now=0)
    assert jobs.claim(con, now=6) is None
    assert jobs.get(con, jid)["state"] == "dead" and jobs.get(con, jid)["errors"] == ["attempt 1: lease expired"]


def test_dedupe_key_delay_and_kind_filter(con):
    a = jobs.enqueue(con, "echo", {"n": 1}, dedupe_key="order-7", now=0)
    assert jobs.enqueue(con, "echo", {"n": 2}, dedupe_key="order-7", now=0) == a
    jobs.enqueue(con, "shout", {"text": "later"}, delay_s=60, now=0)
    assert jobs.claim(con, kinds=["shout"], now=1) is None
    assert jobs.claim(con, kinds=["shout"], now=60)["kind"] == "shout"
    assert jobs.backoff(1) == 2 and jobs.backoff(3) == 8 and jobs.backoff(50) == jobs.BACKOFF_MAX_S
'''

LQ_TEST_WORKER = TEST_HEAD + r'''
from __PKG__ import jobs  # noqa: E402
from __PKG__.handlers import HANDLERS  # noqa: E402
from __PKG__.worker import drain, work_one  # noqa: E402


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_drain_runs_everything_ready(tmp_path):
    con = jobs.connect(str(tmp_path / "q.db"))
    a = jobs.enqueue(con, "shout", {"text": "hi"}, now=0)
    jobs.enqueue(con, "echo", {"n": 1}, now=0)
    seen = drain(con, HANDLERS, clock=Clock(1))
    assert seen == {"done": 2} and jobs.get(con, a)["result"] == "HI"
    con.close()


def test_a_bad_payload_retries_then_dies_and_an_unknown_kind_too():
    con = jobs.connect()
    clock = Clock(0)
    bad = jobs.enqueue(con, "shout", {"text": 5}, max_attempts=2, now=0)
    odd = jobs.enqueue(con, "translate", {}, max_attempts=1, now=0)
    assert drain(con, HANDLERS, clock=clock) == {"retry": 1, "dead": 1}
    clock.t = 100
    assert drain(con, HANDLERS, clock=clock) == {"dead": 1}
    assert "ValueError" in jobs.get(con, bad)["errors"][-1] and "no handler" in jobs.get(con, odd)["errors"][0]


def test_a_handler_slower_than_its_lease_is_lost_not_done():
    con = jobs.connect()
    clock = Clock(0)

    def slow(payload):
        clock.t += 60               # the work took a minute; the lease was 30 s
        return "late"

    jid = jobs.enqueue(con, "slow", {}, now=0)
    assert work_one(con, {"slow": slow}, lease_s=30, clock=clock) == "lost"
    assert jobs.get(con, jid)["state"] == "leased"     # expired: the next claim takes it back
    assert jobs.claim(con, now=clock.t)["id"] == jid
'''

_t('lease_queue', 'Job queue with leases and retries (SQLite)',
   'A durable job queue where a claim is a lease with a token: only the live holder can finish a job, a dead worker just lets the lease run out, failures back off exponentially and end as dead jobs with every error kept. Dedupe keys, delayed jobs, heartbeats, and an injected clock so every timing rule is tested without sleeping.',
   'python / sqlite', ['api', 'data'], {
       **COMMON,
       'README.md': '# __NAME__\n\nA job queue on SQLite where a claim is a **lease**.\n\n    python -m __PKG__ enqueue shout \'{"text": "hello"}\'\n    python -m __PKG__ enqueue echo \'{"n": 1}\' --key order-1   # same key = enqueued once\n    python -m __PKG__ work\n    python -m __PKG__ status\n    python -m pytest -q\n\nHow it behaves:\n\n- `claim` gives a worker the job for `lease_s` seconds and a token. Only that token, while the lease is\n  live, can `complete`, `fail` or `heartbeat` the job (`LeaseLost` otherwise).\n- A worker that dies does nothing: its lease runs out and the next `claim` takes the job back.\n- A failure goes back to the queue after 2, 4, 8 ... seconds (capped at 300). After `max_attempts` the\n  job is `dead`, with every error kept on it. A lease that runs out on the last attempt also ends dead.\n- Every function takes `now=` so the timing rules are tested without sleeping.\n\nStates: `ready`, `leased`, `done`, `dead`. The database is `jobs.db` (or `--db`, or `JOBS_DB`).\n',
       'pyproject.toml': PYPROJECT,
       '__PKG__/__init__.py': '"""__NAME__: a job queue with leases and retries."""\n',
       '__PKG__/__main__.py': LQ_MAIN,
       '__PKG__/jobs.py': LQ_JOBS,
       '__PKG__/worker.py': LQ_WORKER,
       '__PKG__/handlers.py': LQ_HANDLERS,
       'tests/test_jobs.py': LQ_TEST_JOBS,
       'tests/test_worker.py': LQ_TEST_WORKER,
   }, 'python -m __PKG__ status')


# ================================================================================================ api_gateway
GW_LIMITER = r'''"""Token buckets: each client may spend `burst` requests at once, refilled at `rate` per second. The clock is a
parameter so the arithmetic is testable without waiting."""
import threading
import time
from collections import OrderedDict


class TokenBucket:
    def __init__(self, rate, burst, clock=time.monotonic):
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.rate, self.burst, self.clock = float(rate), float(burst), clock
        self.tokens, self.stamp = float(burst), clock()
        self.lock = threading.Lock()

    def take(self, cost=1.0):
        """-> (allowed, seconds until enough tokens are back)."""
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + max(0.0, now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens >= cost:
                self.tokens -= cost
                return True, 0.0
            return False, (cost - self.tokens) / self.rate


class Limiter:
    """One bucket per client, made on first use; past max_clients the least recently seen bucket is forgotten."""

    def __init__(self, rate, burst, clock=time.monotonic, max_clients=10000):
        self.rate, self.burst, self.clock, self.max_clients = rate, burst, clock, max_clients
        self.buckets = OrderedDict()
        self.lock = threading.Lock()

    def take(self, client, cost=1.0):
        with self.lock:
            bucket = self.buckets.get(client)
            if bucket is None:
                bucket = self.buckets[client] = TokenBucket(self.rate, self.burst, self.clock)
                while len(self.buckets) > self.max_clients:
                    self.buckets.popitem(last=False)
            self.buckets.move_to_end(client)
        return bucket.take(cost)
'''

GW_GATEWAY = r'''"""The gateway: API key -> client, client -> token bucket, path prefix -> upstream. Everything it refuses, it
refuses with a JSON reason and the right status (401, 404, 413, 429 + Retry-After, 502, 504)."""
import json
import math
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .limiter import Limiter

MAX_BODY = 1024 * 1024
UPSTREAM_TIMEOUT_S = 10.0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # upstreams are addressed directly


def match_route(routes, path):
    """Longest prefix that matches on a path boundary: '/api' matches '/api' and '/api/x', never '/apix'."""
    best = None
    for prefix in routes:
        p = prefix.rstrip("/") or "/"
        if path == p or path.startswith(p.rstrip("/") + "/"):
            if best is None or len(p) > len(best[0]):
                best = (p, prefix)
    return best


class Handler(BaseHTTPRequestHandler):
    server_version = "__PKG__/0.1"

    def log_message(self, fmt, *args):
        pass                        # quiet: per-client counts live in server.stats

    def _reply(self, code, body, content_type="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        gw = self.server
        path = self.path.split("?", 1)[0]
        if path == "/gateway/health":
            return self._reply(200, {"ok": True, "routes": sorted(gw.routes), "stats": gw.snapshot()})
        client = gw.keys.get(self.headers.get("X-Api-Key", ""))
        if client is None:
            return self._reply(401, {"error": "missing or unknown X-Api-Key"})
        allowed, wait = gw.limiter.take(client)
        if not allowed:
            gw.count(client, "limited")
            return self._reply(429, {"error": "rate limit", "retry_after_s": round(wait, 3)},
                               headers={"Retry-After": str(max(1, math.ceil(wait)))})
        route = match_route(gw.routes, path)
        if route is None:
            return self._reply(404, {"error": f"no route for {path}"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._reply(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return self._reply(413, {"error": f"body larger than {MAX_BODY} bytes"})
        body = self.rfile.read(length) if length else None
        matched, prefix = route
        target = gw.routes[prefix].rstrip("/") + self.path[len(matched.rstrip("/")):]
        req = urllib.request.Request(target, data=body, method=self.command, headers={
            "Content-Type": self.headers.get("Content-Type", "application/json"), "X-Client-Id": client})
        try:
            with _OPENER.open(req, timeout=gw.upstream_timeout) as r:
                status, data, ctype = r.status, r.read(), r.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as e:         # the upstream answered with an error: pass it through
            status, data, ctype = e.code, e.read(), e.headers.get("Content-Type", "application/octet-stream")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason)
            return self._reply(504 if timed_out else 502,
                               {"error": f"upstream {'timed out' if timed_out else 'unreachable'}: {reason}"})
        gw.count(client, "allowed")
        return self._reply(status, data, content_type=ctype)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, routes, keys, rate=5.0, burst=10, clock=None, host="127.0.0.1", port=0,
                 upstream_timeout=UPSTREAM_TIMEOUT_S):
        super().__init__((host, port), Handler)
        self.routes, self.keys, self.upstream_timeout = dict(routes), dict(keys), upstream_timeout
        self.limiter = Limiter(rate, burst, clock) if clock else Limiter(rate, burst)
        self.stats, self._stats_lock = {}, threading.Lock()

    def count(self, client, what):
        with self._stats_lock:
            row = self.stats.setdefault(client, {"allowed": 0, "limited": 0})
            row[what] += 1

    def snapshot(self):
        with self._stats_lock:
            return {k: dict(v) for k, v in self.stats.items()}


def load_config(path):
    """gateway.json -> keyword arguments for Gateway. Refuses a file without routes or keys."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("routes", "keys"):
        if not isinstance(cfg.get(key), dict) or not cfg[key]:
            raise ValueError(f"{path}: {key!r} must be a non-empty object")
    return {"routes": cfg["routes"], "keys": cfg["keys"], "rate": float(cfg.get("rate", 5)),
            "burst": int(cfg.get("burst", 10)), "port": int(cfg.get("port", 8080))}
'''

GW_MAIN = r'''import sys

from .gateway import Gateway, load_config


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else "gateway.json"
    try:
        cfg = load_config(path)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    gw = Gateway(**cfg)
    print(f"gateway on http://127.0.0.1:{gw.server_address[1]}  routes: {', '.join(sorted(cfg['routes']))}")
    try:
        gw.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        gw.server_close()
    return 0


sys.exit(main())
'''

GW_CONFIG = r'''{
  "port": 8080,
  "rate": 5,
  "burst": 10,
  "routes": {"/notes": "http://127.0.0.1:9001", "/search": "http://127.0.0.1:9002/v1/search"},
  "keys": {"local-dev-key": "dev", "local-batch-key": "batch"}
}
'''

GW_TEST_LIMITER = TEST_HEAD + r'''
import pytest  # noqa: E402

from __PKG__.limiter import Limiter, TokenBucket  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_burst_then_refill_at_rate():
    clock = Clock()
    b = TokenBucket(rate=2, burst=3, clock=clock)
    assert [b.take()[0] for _ in range(4)] == [True, True, True, False]
    assert b.take() == (False, 0.5)
    clock.t = 0.5
    assert b.take()[0] and not b.take()[0]
    clock.t = 100
    assert [b.take()[0] for _ in range(4)] == [True, True, True, False]   # never more than the burst


def test_each_client_has_its_own_bucket_and_old_ones_are_forgotten():
    lim = Limiter(rate=1, burst=1, clock=Clock(), max_clients=2)
    assert lim.take("a")[0] and not lim.take("a")[0] and lim.take("b")[0]
    lim.take("c")
    assert list(lim.buckets) == ["b", "c"]


def test_nonsense_settings_are_refused():
    with pytest.raises(ValueError):
        TokenBucket(rate=0, burst=1)
'''

GW_TEST_GATEWAY = TEST_HEAD + r'''
import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import pytest  # noqa: E402

from __PKG__.gateway import Gateway, load_config, match_route  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _echo(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        code = 418 if self.path.endswith("/teapot") else 200
        data = json.dumps({"path": self.path, "method": self.command, "client": self.headers.get("X-Client-Id"),
                           "body": body}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _echo


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def start(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def gw():
    up = start(ThreadingHTTPServer(("127.0.0.1", 0), Upstream))
    clock = Clock()
    routes = {"/notes": f"http://127.0.0.1:{up.server_address[1]}/v2", "/down": f"http://127.0.0.1:{closed_port()}"}
    g = start(Gateway(routes, {"k1": "alice", "k2": "bob"}, rate=1, burst=2, clock=clock))
    yield f"http://127.0.0.1:{g.server_address[1]}", g, clock
    for s in (g, up):
        s.shutdown()
        s.server_close()


def call(url, key=None, body=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Api-Key"] = key
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with OPENER.open(req, timeout=5) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())


def test_routes_match_on_path_boundaries():
    routes = {"/api": "a", "/api/v2/": "b"}
    assert match_route(routes, "/api/x") == ("/api", "/api")
    assert match_route(routes, "/api/v2/items") == ("/api/v2", "/api/v2/")
    assert match_route(routes, "/apix") is None


def test_a_request_is_proxied_with_the_prefix_rewritten(gw):
    url, g, _ = gw
    code, _, out = call(url + "/notes/7?full=1", key="k1", body={"text": "hi"})
    assert code == 200 and out == {"path": "/v2/7?full=1", "method": "POST", "client": "alice", "body": '{"text": "hi"}'}
    assert g.snapshot() == {"alice": {"allowed": 1, "limited": 0}}


def test_no_key_or_a_wrong_key_is_401(gw):
    url, _, _ = gw
    assert call(url + "/notes")[0] == 401 and call(url + "/notes", key="nope")[0] == 401


def test_the_limit_is_per_client_and_says_when_to_come_back(gw):
    url, g, clock = gw
    assert [call(url + "/notes", key="k1")[0] for _ in range(3)] == [200, 200, 429]
    code, headers, out = call(url + "/notes", key="k1")
    assert code == 429 and headers["Retry-After"] == "1" and out["retry_after_s"] == 1.0
    assert call(url + "/notes", key="k2")[0] == 200           # bob has his own bucket
    clock.t = 1.0
    assert call(url + "/notes", key="k1")[0] == 200
    assert g.snapshot()["alice"] == {"allowed": 3, "limited": 2}


def test_unknown_route_upstream_errors_and_a_dead_upstream(gw):
    url, _, clock = gw
    assert call(url + "/elsewhere", key="k1")[0] == 404
    clock.t = 10
    assert call(url + "/notes/teapot", key="k1")[0] == 418      # the upstream's own error is passed through
    clock.t = 20
    code, _, out = call(url + "/down/x", key="k1")
    assert code == 502 and "unreachable" in out["error"]


def test_health_needs_no_key_and_config_is_checked(gw, tmp_path):
    url, _, _ = gw
    code, _, out = call(url + "/gateway/health")
    assert code == 200 and out["routes"] == ["/down", "/notes"]
    p = tmp_path / "gateway.json"
    p.write_text(json.dumps({"routes": {}, "keys": {"a": "b"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="routes"):
        load_config(str(p))
    shipped = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway.json")
    assert load_config(shipped)["burst"] == 10
'''

_t('api_gateway', 'Rate-limited API gateway (token buckets)',
   'A loopback gateway in front of your local services: API keys map to clients, each client has its own token bucket (429 with Retry-After), path prefixes route to upstreams with the prefix rewritten, and dead or slow upstreams answer 502/504 with a reason. Tests run a real upstream and a real gateway on free ports with an injected clock.',
   'python / http.server', ['api'], {
       **COMMON,
       'README.md': '# __NAME__\n\nA gateway in front of local services.\n\n    python -m __PKG__ gateway.json\n    python -m pytest -q\n\nFor every request, in this order:\n\n1. `GET /gateway/health` answers without a key (routes and per-client counts).\n2. `X-Api-Key` must name a client in `keys` (401 otherwise).\n3. The client spends one token from its own bucket: `burst` at once, refilled at `rate` per second.\n   Out of tokens: 429 with `Retry-After` (whole seconds) and `retry_after_s`.\n4. The longest matching prefix in `routes` picks the upstream, matched on a path boundary (`/notes` matches\n   `/notes/7`, never `/notesx`), and the prefix is replaced by the upstream URL\'s own path.\n5. The upstream\'s answer (including its errors) is passed back; an unreachable upstream is 502, a slow one 504.\n\nThe upstream sees `X-Client-Id` so it knows who asked. Bodies above 1 MiB are refused (413).\n',
       'pyproject.toml': PYPROJECT,
       'gateway.json': GW_CONFIG,
       '__PKG__/__init__.py': '"""__NAME__: a rate-limited gateway for local services."""\n',
       '__PKG__/__main__.py': GW_MAIN,
       '__PKG__/limiter.py': GW_LIMITER,
       '__PKG__/gateway.py': GW_GATEWAY,
       'tests/test_limiter.py': GW_TEST_LIMITER,
       'tests/test_gateway.py': GW_TEST_GATEWAY,
   }, 'python -m __PKG__ gateway.json')

# ================================================================================================ event_store
ES_STORE = r'''"""An append-only event store on SQLite. A stream (one account, one order) is a numbered list of events; you append
only when you say which version you read (optimistic concurrency), so two writers deciding on the same old state
cannot both win. Triggers make UPDATE and DELETE impossible: history is never rewritten."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT NOT NULL,
    version INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    UNIQUE (stream, version)
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class Conflict(Exception):
    """The stream moved on since you read it: load again, decide again."""

    def __init__(self, stream, expected, actual):
        super().__init__(f"{stream}: expected version {expected}, found {actual}")
        self.stream, self.expected, self.actual = stream, expected, actual


def _row(r):
    return {"position": r[0], "stream": r[1], "version": r[2], "type": r[3], "data": json.loads(r[4]), "recorded_at": r[5]}


class EventStore:
    def __init__(self, path=":memory:", clock=time.time):
        self.con = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.con.executescript(SCHEMA)
        self.clock = clock

    def close(self):
        self.con.close()

    def version(self, stream):
        return self.con.execute("SELECT COALESCE(MAX(version), 0) FROM events WHERE stream = ?", (stream,)).fetchone()[0]

    def append(self, stream, events, expected_version):
        """events: list of (type, data dict). -> the stream's new version. Raises Conflict unless the stream is at
        expected_version (0 = a new stream). All events land, or none."""
        if not events:
            raise ValueError("nothing to append")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            current = self.version(stream)
            if current != expected_version:
                raise Conflict(stream, expected_version, current)
            for i, (etype, data) in enumerate(events, start=1):
                self.con.execute("INSERT INTO events (stream, version, type, data, recorded_at) VALUES (?, ?, ?, ?, ?)",
                                 (stream, current + i, etype, json.dumps(data, sort_keys=True), self.clock()))
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return current + len(events)

    def read(self, stream, after_version=0):
        cur = self.con.execute("SELECT position, stream, version, type, data, recorded_at FROM events "
                               "WHERE stream = ? AND version > ? ORDER BY version", (stream, after_version))
        return [_row(r) for r in cur]

    def read_all(self, after_position=0, limit=1000):
        """Every stream in the order things happened: the feed projections are built from."""
        cur = self.con.execute("SELECT position, stream, version, type, data, recorded_at FROM events "
                               "WHERE position > ? ORDER BY position LIMIT ?", (after_position, limit))
        return [_row(r) for r in cur]
'''

ES_ACCOUNT = r'''"""A worked example: a bank account as events. `decide` turns a command into events (or refuses), `evolve` folds
events into state; both are pure, so the rules are tested without a database. Swap this file for your own domain."""
from .store import Conflict


class Refused(ValueError):
    """The rules do not allow this command right now. Nothing was written."""


def stream(account_id):
    return f"account-{account_id}"


def evolve(state, event):
    t, d = event["type"], event["data"]
    if t == "Opened":
        return {"owner": d["owner"], "balance": 0, "open": True}
    if t == "Deposited":
        return dict(state, balance=state["balance"] + d["amount"])
    if t == "Withdrew":
        return dict(state, balance=state["balance"] - d["amount"])
    if t == "Closed":
        return dict(state, open=False)
    raise ValueError(f"unknown event type {t!r}")


def fold(events, state=None):
    for e in events:
        state = evolve(state, e)
    return state


def _amount(args):
    amount = args.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise Refused("amount must be a whole number of cents above 0")
    return amount


def decide(state, command, args):
    """-> list of (type, data). Raises Refused."""
    if command == "open":
        if state is not None:
            raise Refused("account already exists")
        owner = str(args.get("owner", "")).strip()
        if not owner:
            raise Refused("an account needs an owner")
        return [("Opened", {"owner": owner})]
    if state is None or not state["open"]:
        raise Refused("no open account")
    if command == "deposit":
        return [("Deposited", {"amount": _amount(args)})]
    if command == "withdraw":
        amount = _amount(args)
        if amount > state["balance"]:
            raise Refused(f"balance {state['balance']} is less than {amount}")
        return [("Withdrew", {"amount": amount})]
    if command == "close":
        if state["balance"] != 0:
            raise Refused("withdraw the balance before closing")
        return [("Closed", {})]
    raise Refused(f"unknown command {command!r}")


def load(store, account_id):
    """-> (state or None, version)."""
    events = store.read(stream(account_id))
    return fold(events), (events[-1]["version"] if events else 0)


def handle(store, account_id, command, args=None, retries=3):
    """Load, decide, append at the version that was read. On a Conflict someone else wrote first: load the new
    state and decide AGAIN (the rules may now say no). -> the new state."""
    args = args or {}
    for _ in range(retries):
        state, version = load(store, account_id)
        events = decide(state, command, args)
        try:
            store.append(stream(account_id), events, version)
        except Conflict:
            continue
        return fold([{"type": t, "data": d} for t, d in events], state)
    raise Conflict(stream(account_id), version, store.version(stream(account_id)))
'''

ES_PROJECTIONS = r'''"""A read model built from the global feed: balances per account and totals per owner. It remembers the last
position it saw, so catch_up() only reads what is new; rebuild() starts from zero and must reach the same answer."""
from .account import evolve


class Balances:
    def __init__(self):
        self.position = 0
        self.accounts = {}

    def apply(self, event):
        if event["stream"].startswith("account-"):
            key = event["stream"][len("account-"):]
            self.accounts[key] = evolve(self.accounts.get(key), event)
        self.position = event["position"]

    def catch_up(self, store, batch=500):
        """-> how many events were applied."""
        n = 0
        while True:
            events = store.read_all(self.position, batch)
            for e in events:
                self.apply(e)
            n += len(events)
            if len(events) < batch:
                return n

    def by_owner(self):
        out = {}
        for state in self.accounts.values():
            out[state["owner"]] = out.get(state["owner"], 0) + state["balance"]
        return out


def rebuild(store):
    view = Balances()
    view.catch_up(store)
    return view
'''

ES_MAIN = r'''import json
import sys

from .account import Refused, handle
from .projections import rebuild
from .store import EventStore


def main(argv=None):
    """python -m __PKG__ DB ACCOUNT COMMAND [JSON args]   e.g.  bank.db 1 open '{"owner": "Ada"}'
    python -m __PKG__ DB balances"""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[1] == "balances":
        store = EventStore(argv[0])
        view = rebuild(store)
        store.close()
        print(json.dumps({"accounts": view.accounts, "by_owner": view.by_owner(), "position": view.position}, indent=2))
        return 0
    if len(argv) not in (3, 4):
        print(main.__doc__, file=sys.stderr)
        return 2
    store = EventStore(argv[0])
    try:
        state = handle(store, argv[1], argv[2], json.loads(argv[3]) if len(argv) == 4 else {})
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(state))
    return 0


sys.exit(main())
'''

ES_TEST_STORE = TEST_HEAD + r'''
import sqlite3  # noqa: E402

import pytest  # noqa: E402

from __PKG__.store import Conflict, EventStore  # noqa: E402


def test_append_and_read_back_in_order():
    s = EventStore(clock=lambda: 42.0)
    assert s.append("a", [("X", {"n": 1}), ("Y", {"n": 2})], expected_version=0) == 2
    assert s.append("b", [("X", {})], expected_version=0) == 1
    assert [(e["version"], e["type"], e["data"]) for e in s.read("a")] == [(1, "X", {"n": 1}), (2, "Y", {"n": 2})]
    assert [e["stream"] for e in s.read_all()] == ["a", "a", "b"] and s.read("a", after_version=1)[0]["type"] == "Y"
    assert s.read_all()[0]["recorded_at"] == 42.0


def test_a_stale_writer_gets_a_conflict_and_writes_nothing(tmp_path):
    db = str(tmp_path / "events.db")
    one, two = EventStore(db), EventStore(db)          # two processes, one file
    one.append("acct", [("Opened", {})], 0)
    two.append("acct", [("Deposited", {"amount": 5})], 1)
    with pytest.raises(Conflict) as e:
        one.append("acct", [("Deposited", {"amount": 9}), ("Deposited", {"amount": 1})], 1)
    assert (e.value.expected, e.value.actual) == (1, 2) and one.version("acct") == 2
    one.close()
    two.close()


def test_history_cannot_be_rewritten():
    s = EventStore()
    s.append("a", [("X", {})], 0)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.con.execute("UPDATE events SET type = 'Z'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.con.execute("DELETE FROM events")
    assert s.read("a")[0]["type"] == "X"


def test_an_empty_append_is_refused():
    with pytest.raises(ValueError):
        EventStore().append("a", [], 0)
'''

ES_TEST_ACCOUNT = TEST_HEAD + r'''
import pytest  # noqa: E402

from __PKG__ import account  # noqa: E402
from __PKG__.projections import Balances, rebuild  # noqa: E402
from __PKG__.store import EventStore  # noqa: E402


def test_the_rules_hold():
    s = EventStore()
    account.handle(s, "1", "open", {"owner": "Ada"})
    account.handle(s, "1", "deposit", {"amount": 500})
    assert account.handle(s, "1", "withdraw", {"amount": 200}) == {"owner": "Ada", "balance": 300, "open": True}
    for command, args, why in (("withdraw", {"amount": 301}, "less than"), ("deposit", {"amount": 0}, "above 0"),
                               ("deposit", {"amount": True}, "above 0"), ("close", {}, "withdraw the balance"),
                               ("open", {"owner": "Bo"}, "already exists"), ("fly", {}, "unknown command")):
        with pytest.raises(account.Refused, match=why):
            account.handle(s, "1", command, args)
    assert s.version("account-1") == 3
    with pytest.raises(account.Refused, match="no open account"):
        account.handle(s, "2", "deposit", {"amount": 1})


def test_a_concurrent_write_makes_handle_decide_again(tmp_path):
    db = str(tmp_path / "e.db")
    s, other = EventStore(db), EventStore(db)
    account.handle(s, "1", "open", {"owner": "Ada"})
    account.handle(s, "1", "deposit", {"amount": 100})
    original = s.append
    interfered = []

    def append_after_someone_else(stream, events, expected):
        if not interfered:                      # between our read and our write, another writer withdraws 80
            interfered.append(True)
            other.append(stream, [("Withdrew", {"amount": 80})], expected)
        return original(stream, events, expected)

    s.append = append_after_someone_else
    with pytest.raises(account.Refused, match="balance 20 is less than 50"):
        account.handle(s, "1", "withdraw", {"amount": 50})     # re-decided on the NEW state, so refused
    assert account.load(s, "1") == ({"owner": "Ada", "balance": 20, "open": True}, 3)
    s.close()
    other.close()


def test_the_projection_catches_up_and_a_rebuild_agrees():
    s = EventStore()
    for acct, owner in (("1", "Ada"), ("2", "Ada"), ("3", "Grace")):
        account.handle(s, acct, "open", {"owner": owner})
        account.handle(s, acct, "deposit", {"amount": 100 * int(acct)})
    live = Balances()
    assert live.catch_up(s) == 6
    account.handle(s, "3", "withdraw", {"amount": 50})
    assert live.catch_up(s, batch=1) == 1 and live.catch_up(s) == 0
    assert live.by_owner() == {"Ada": 300, "Grace": 250}
    fresh = rebuild(s)
    assert fresh.accounts == live.accounts and fresh.position == live.position == 7
'''

_t('event_store', 'Event-sourced store (append-only, optimistic concurrency)',
   'State as an append-only list of events on SQLite: appends name the version they read (a stale writer gets a Conflict and the command is decided again on the new state), triggers forbid rewriting history, and a projection catches up from the global feed and must agree with a rebuild from zero. Ships a bank-account example.',
   'python / sqlite', ['data', 'api'], {
       **COMMON,
       'README.md': '# __NAME__\n\nState as events. Nothing is updated in place; every change is a new event in a numbered stream.\n\n    python -m __PKG__ bank.db 1 open \'{"owner": "Ada"}\'\n    python -m __PKG__ bank.db 1 deposit \'{"amount": 500}\'\n    python -m __PKG__ bank.db 1 withdraw \'{"amount": 900}\'     # refused: balance 500 is less than 900\n    python -m __PKG__ bank.db balances\n    python -m pytest -q\n\n- `store.py`: the append-only store. `append(stream, events, expected_version)` writes only when the stream is\n  still at the version you read, or raises `Conflict`. SQLite triggers refuse UPDATE and DELETE.\n- `account.py`: a worked domain. `decide(state, command, args)` returns events or raises `Refused`;\n  `evolve(state, event)` folds them. `handle()` loads, decides and appends, and on a `Conflict` decides\n  **again** on the new state (a withdrawal that was fine a moment ago may now be refused).\n- `projections.py`: a read model fed from `read_all()`. `catch_up()` reads only what is new; `rebuild()`\n  starts from zero and has to reach the same answer.\n\nAmounts are whole cents.\n',
       'pyproject.toml': PYPROJECT,
       '__PKG__/__init__.py': '"""__NAME__: an event-sourced store."""\n',
       '__PKG__/__main__.py': ES_MAIN,
       '__PKG__/store.py': ES_STORE,
       '__PKG__/account.py': ES_ACCOUNT,
       '__PKG__/projections.py': ES_PROJECTIONS,
       'tests/test_store.py': ES_TEST_STORE,
       'tests/test_account.py': ES_TEST_ACCOUNT,
   }, 'python -m __PKG__ bank.db balances')


# ================================================================================================ model_cli
MC_CLIENT = r'''"""The one function that talks to the model: a local server speaking Ollama's /api/chat (MODEL_HOST, default
127.0.0.1:11434). Swap it for another transport with the same signature; everything else stays."""
import json
import os
import time
import urllib.request

DEFAULT_HOST = "127.0.0.1:11434"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # a local server, never through a proxy


class ModelUnavailable(RuntimeError):
    """No answer from the model server (not running, wrong host, model missing)."""


def host():
    h = os.environ.get("MODEL_HOST", DEFAULT_HOST)
    return h.rstrip("/") if h.startswith("http") else "http://" + h.rstrip("/")


def chat_http(model, messages, options, timeout=300):
    """-> {"text", "tokens", "seconds"}. Raises ModelUnavailable."""
    body = json.dumps({"model": model, "messages": messages, "stream": False, "options": options}).encode()
    req = urllib.request.Request(host() + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise ModelUnavailable(f"{host()}: {e}") from e
    if "message" not in reply:
        raise ModelUnavailable(f"{host()}: unexpected reply {str(reply)[:200]}")
    return {"text": reply["message"].get("content", ""), "tokens": reply.get("eval_count"),
            "seconds": round(time.monotonic() - started, 3)}
'''

MC_CACHE = r'''"""Replies this machine already produced, kept in SQLite so the same question never costs twice. Only a
REPEATABLE request is kept: temperature 0, or a fixed seed. Anything else would replay one random draw forever."""
import hashlib
import json
import sqlite3
import time


def why_not(options):
    """-> None when the request is repeatable, else the reason in plain words."""
    if options.get("seed") is not None:
        return None
    if float(options.get("temperature", 0.8)) == 0.0:
        return None
    return f"temperature {options.get('temperature', 0.8)} without a seed gives a different reply each time"


def key(model, messages, options):
    blob = json.dumps({"model": model, "messages": messages, "options": options}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ReplyCache:
    def __init__(self, path):
        self.con = sqlite3.connect(path, isolation_level=None)
        self.con.execute("CREATE TABLE IF NOT EXISTS replies (key TEXT PRIMARY KEY, model TEXT NOT NULL, reply TEXT NOT NULL, "
                         "created REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0)")

    def close(self):
        self.con.close()

    def get(self, k):
        row = self.con.execute("SELECT reply FROM replies WHERE key = ?", (k,)).fetchone()
        if row is None:
            return None
        self.con.execute("UPDATE replies SET hits = hits + 1 WHERE key = ?", (k,))
        return json.loads(row[0])

    def put(self, k, model, reply):
        self.con.execute("INSERT OR REPLACE INTO replies (key, model, reply, created) VALUES (?, ?, ?, ?)",
                         (k, model, json.dumps(reply), time.time()))

    def stats(self):
        n, hits, size = self.con.execute("SELECT COUNT(*), COALESCE(SUM(hits), 0), COALESCE(SUM(LENGTH(reply)), 0) FROM replies").fetchone()
        return {"entries": n, "hits": hits, "bytes": size}

    def clear(self):
        n = self.con.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
        self.con.execute("DELETE FROM replies")
        return n
'''

MC_ASK = r'''"""ask(): build the request, look in the cache, call the model only on a miss, keep the reply when it is
repeatable. The answer always says whether it came from the cache, and if it was not kept, why not."""
from . import cache as cache_mod
from .client import chat_http


def ask(prompt, model, system="", temperature=0.0, seed=None, cache=None, transport=chat_http):
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    options = {"temperature": temperature}
    if seed is not None:
        options["seed"] = seed
    reason = cache_mod.why_not(options) if cache is not None else "cache off"
    k = cache_mod.key(model, messages, options)
    if reason is None:
        hit = cache.get(k)
        if hit is not None:
            return dict(hit, cached=True, why_not_cached=None)
    reply = transport(model, messages, options)
    if reason is None:
        cache.put(k, model, reply)
    return dict(reply, cached=False, why_not_cached=reason)
'''

MC_CLI = r'''"""The command line. main() returns an exit code (0 ok, 2 usage, 3 model unavailable) and never calls sys.exit."""
import argparse
import json
import os
import sys

from .ask import ask
from .cache import ReplyCache
from .client import ModelUnavailable, chat_http

EXIT_OK, EXIT_USAGE, EXIT_MODEL = 0, 2, 3


def build_parser():
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: ask a local model, with a reply cache.")
    ap.add_argument("--cache", default=os.environ.get("MODEL_CLI_CACHE", ".model_cache.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ask", help="ask one question ('-' reads it from stdin)")
    p.add_argument("prompt", nargs="+")
    p.add_argument("--model", default=os.environ.get("MODEL_CLI_MODEL", "qwen2.5-coder:7b"))
    p.add_argument("--system", default="")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--seed", type=int)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", action="store_true")
    c = sub.add_parser("cache", help="look at or empty the reply cache")
    c.add_argument("action", choices=["stats", "clear"])
    return ap


def main(argv=None, transport=chat_http, stdin=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code in (0, None) else EXIT_USAGE
    store = ReplyCache(args.cache)
    try:
        if args.cmd == "cache":
            out = store.stats() if args.action == "stats" else {"removed": store.clear()}
            print(json.dumps(out))
            return EXIT_OK
        prompt = " ".join(args.prompt)
        if prompt == "-":
            prompt = (stdin or sys.stdin).read()
        if not prompt.strip():
            print("error: empty prompt", file=sys.stderr)
            return EXIT_USAGE
        try:
            reply = ask(prompt, args.model, args.system, args.temperature, args.seed,
                        cache=None if args.no_cache else store, transport=transport)
        except ModelUnavailable as e:
            print(f"error: model unavailable: {e}", file=sys.stderr)
            return EXIT_MODEL
        print(json.dumps(reply) if args.json else reply["text"])
        if not args.json:
            note = "from cache" if reply["cached"] else (f"not cached: {reply['why_not_cached']}" if reply["why_not_cached"] else "cached for next time")
            print(f"({note})", file=sys.stderr)
        return EXIT_OK
    finally:
        store.close()
'''

MC_MAIN = r'''import sys

from .cli import main

sys.exit(main())
'''

MC_TEST_ASK = TEST_HEAD + r'''
from __PKG__ import cache  # noqa: E402
from __PKG__.ask import ask  # noqa: E402
from __PKG__.cache import ReplyCache  # noqa: E402


class Scripted:
    """Stands in for the model server: answers from a counter and records every request."""

    def __init__(self):
        self.calls = []

    def __call__(self, model, messages, options):
        self.calls.append((model, messages, options))
        return {"text": f"reply {len(self.calls)}", "tokens": 3, "seconds": 0.0}


def test_the_same_question_is_answered_once(tmp_path):
    store, model = ReplyCache(str(tmp_path / "c.db")), Scripted()
    first = ask("what is a lease?", "m1", cache=store, transport=model)
    second = ask("what is a lease?", "m1", cache=store, transport=model)
    assert first["text"] == second["text"] == "reply 1" and len(model.calls) == 1
    assert not first["cached"] and second["cached"] and store.stats()["hits"] == 1
    store.close()


def test_a_different_model_system_or_seed_is_a_different_question(tmp_path):
    store, model = ReplyCache(str(tmp_path / "c.db")), Scripted()
    ask("q", "m1", cache=store, transport=model)
    ask("q", "m2", cache=store, transport=model)
    ask("q", "m1", system="be brief", cache=store, transport=model)
    ask("q", "m1", temperature=0.7, seed=1, cache=store, transport=model)
    assert len(model.calls) == 4 and store.stats()["entries"] == 4
    store.close()


def test_a_random_draw_is_never_kept_and_says_why(tmp_path):
    store, model = ReplyCache(str(tmp_path / "c.db")), Scripted()
    a = ask("q", "m1", temperature=0.7, cache=store, transport=model)
    b = ask("q", "m1", temperature=0.7, cache=store, transport=model)
    assert len(model.calls) == 2 and not b["cached"] and "without a seed" in a["why_not_cached"]
    assert store.stats()["entries"] == 0
    assert ask("q", "m1", cache=None, transport=model)["why_not_cached"] == "cache off"
    store.close()


def test_the_key_ignores_dict_order_and_the_cache_persists(tmp_path):
    msgs = [{"role": "user", "content": "x"}]
    assert cache.key("m", msgs, {"temperature": 0, "seed": 1}) == cache.key("m", msgs, {"seed": 1, "temperature": 0})
    path = str(tmp_path / "c.db")
    s = ReplyCache(path)
    s.put("k", "m", {"text": "kept"})
    s.close()
    s = ReplyCache(path)
    assert s.get("k") == {"text": "kept"} and s.clear() == 1 and s.get("k") is None
    s.close()
'''

MC_TEST_CLI = TEST_HEAD + r'''
import io  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import pytest  # noqa: E402

from __PKG__ import cli, client  # noqa: E402


class LocalModel(BaseHTTPRequestHandler):
    """A loopback stand-in for the model server's /api/chat: echoes the last user message upper-cased."""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.dumps({"message": {"role": "assistant", "content": body["messages"][-1]["content"].upper()},
                           "eval_count": 7, "model": body["model"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("MODEL_HOST", f"127.0.0.1:{srv.server_address[1]}")
    yield srv
    srv.shutdown()
    srv.server_close()


def run(capsys, *args, **kw):
    code = cli.main(list(args), **kw)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_ask_over_real_http_then_from_the_cache(server, tmp_path, capsys):
    db = str(tmp_path / "c.db")
    code, out, err = run(capsys, "--cache", db, "ask", "hello", "there")
    assert code == 0 and out == "HELLO THERE\n" and "cached for next time" in err
    code, out, err = run(capsys, "--cache", db, "ask", "hello", "there", "--json")
    reply = json.loads(out)
    assert code == 0 and reply["cached"] and reply["text"] == "HELLO THERE" and reply["tokens"] == 7
    code, out, _ = run(capsys, "--cache", db, "cache", "stats")
    assert json.loads(out)["entries"] == 1 and json.loads(out)["hits"] == 1


def test_stdin_prompt_no_cache_and_clear(server, tmp_path, capsys):
    db = str(tmp_path / "c.db")
    code, out, err = run(capsys, "--cache", db, "ask", "-", "--no-cache", stdin=io.StringIO("from stdin"))
    assert code == 0 and out == "FROM STDIN\n" and "cache off" in err
    run(capsys, "--cache", db, "ask", "kept")
    assert json.loads(run(capsys, "--cache", db, "cache", "clear")[1]) == {"removed": 1}


def test_an_absent_model_server_is_exit_3_and_bad_usage_is_2(tmp_path, capsys, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("MODEL_HOST", f"http://127.0.0.1:{port}")
    assert client.host() == f"http://127.0.0.1:{port}"
    code, _, err = run(capsys, "--cache", str(tmp_path / "c.db"), "ask", "anyone?")
    assert code == cli.EXIT_MODEL and "model unavailable" in err
    assert run(capsys, "--cache", str(tmp_path / "c.db"), "frobnicate")[0] == cli.EXIT_USAGE
    assert run(capsys, "--cache", str(tmp_path / "c.db"), "ask", "  ")[0] == cli.EXIT_USAGE
'''

_t('model_cli', 'CLI over a local model (with a reply cache)',
   'Ask a local model from the shell: one transport function for an Ollama-style /api/chat server, a SQLite reply cache that keeps only repeatable requests (temperature 0 or a seed) and always says why a reply was not kept, stdin prompts, --json, and exit codes. Tests use a loopback stand-in server, so no model is needed.',
   'python / urllib / sqlite', ['api'], {
       **COMMON,
       'README.md': '# __NAME__\n\nAsk a local model from the shell; the same question never costs twice.\n\n    python -m __PKG__ ask "what is a lease?"\n    git diff | python -m __PKG__ ask - --system "review this diff"\n    python -m __PKG__ ask "a name for a cat" --temperature 0.9            # not cached: says why\n    python -m __PKG__ ask "a name for a cat" --temperature 0.9 --seed 4   # cached: repeatable\n    python -m __PKG__ cache stats\n    python -m __PKG__ cache clear\n    python -m pytest -q          # a loopback stand-in plays the model server; no model needed\n\nSettings: `MODEL_HOST` (default 127.0.0.1:11434, a server speaking `/api/chat`), `MODEL_CLI_MODEL`,\n`MODEL_CLI_CACHE` (default `.model_cache.db`).\n\n**The cache keeps only repeatable requests**: temperature 0, or a fixed seed. A reply drawn at random would\notherwise be replayed forever as if it were the answer. Every reply says whether it came from the cache, and if\nit was not kept, why not. The key is a hash of the model, the messages and the options.\n\nExit codes: 0 ok, 2 bad usage or an empty prompt, 3 the model server did not answer.\n',
       'pyproject.toml': PYPROJECT,
       '__PKG__/__init__.py': '"""__NAME__: a local model on the command line, with a reply cache."""\n',
       '__PKG__/__main__.py': MC_MAIN,
       '__PKG__/client.py': MC_CLIENT,
       '__PKG__/cache.py': MC_CACHE,
       '__PKG__/ask.py': MC_ASK,
       '__PKG__/cli.py': MC_CLI,
       'tests/test_ask.py': MC_TEST_ASK,
       'tests/test_cli.py': MC_TEST_CLI,
   }, 'python -m __PKG__ cache stats')

# ================================================================================================ telemetry_collector
TC_STORE = r'''"""What the collector keeps: metric points (counter, gauge, histogram) per series and spans per trace, both
bounded so a chatty client cannot fill the memory. Every input is checked; a bad one is refused with a reason."""
import math
import threading
from collections import OrderedDict, deque

KINDS = ("counter", "gauge", "histogram")


def series(name, labels=None):
    """'name{a=1,b=2}' with the labels sorted, so the same labels in any order are one series."""
    if not labels:
        return name
    return name + "{" + ",".join(f"{k}={labels[k]}" for k in sorted(labels)) + "}"


def percentile(values, q):
    """Nearest-rank percentile of a list (q in 0..100). None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class Collector:
    def __init__(self, max_samples=10000, max_traces=1000):
        self.max_samples, self.max_traces = max_samples, max_traces
        self.counters, self.gauges, self.histograms = {}, {}, {}
        self.traces = OrderedDict()
        self.lock = threading.Lock()

    def add_metric(self, m):
        if not isinstance(m, dict) or not isinstance(m.get("name"), str) or not m["name"]:
            raise ValueError("a metric needs a name")
        if m.get("kind") not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if not _number(m.get("value")):
            raise ValueError("value must be a finite number")
        if m["kind"] == "counter" and m["value"] < 0:
            raise ValueError("a counter only goes up")
        labels = m.get("labels") or {}
        if not isinstance(labels, dict):
            raise ValueError("labels must be an object")
        key = series(m["name"], labels)
        with self.lock:
            if m["kind"] == "counter":
                self.counters[key] = self.counters.get(key, 0) + m["value"]
            elif m["kind"] == "gauge":
                self.gauges[key] = m["value"]
            else:
                self.histograms.setdefault(key, deque(maxlen=self.max_samples)).append(m["value"])

    def add_span(self, s):
        if not isinstance(s, dict):
            raise ValueError("a span is an object")
        for k in ("trace_id", "span_id", "name"):
            if not isinstance(s.get(k), str) or not s[k]:
                raise ValueError(f"a span needs {k}")
        if not _number(s.get("start")) or not _number(s.get("end")) or s["end"] < s["start"]:
            raise ValueError("start and end must be numbers with end >= start")
        span = {k: s.get(k) for k in ("trace_id", "span_id", "parent_id", "name", "start", "end")}
        span["attrs"] = s.get("attrs") if isinstance(s.get("attrs"), dict) else {}
        with self.lock:
            spans = self.traces.setdefault(span["trace_id"], {})
            spans[span["span_id"]] = span
            self.traces.move_to_end(span["trace_id"])
            while len(self.traces) > self.max_traces:
                self.traces.popitem(last=False)

    def summary(self):
        with self.lock:
            hist = {k: list(v) for k, v in self.histograms.items()}
            out = {"counters": dict(self.counters), "gauges": dict(self.gauges), "histograms": {}}
        for k, values in hist.items():
            out["histograms"][k] = {"count": len(values), "min": min(values), "max": max(values),
                                    "p50": percentile(values, 50), "p95": percentile(values, 95), "p99": percentile(values, 99)}
        return out

    def trace(self, trace_id):
        """-> {"trace_id", "duration_ms", "roots", "orphans", "critical_path"} or None. A span whose parent never
        arrived is an orphan: shown as a root and listed, never dropped."""
        with self.lock:
            spans = dict(self.traces.get(trace_id) or {})
        if not spans:
            return None
        children = {}
        for s in spans.values():
            children.setdefault(s["parent_id"], []).append(s)

        def node(s):
            kids = sorted(children.get(s["span_id"], []), key=lambda c: c["start"])
            return {"name": s["name"], "span_id": s["span_id"], "ms": round((s["end"] - s["start"]) * 1000, 3),
                    "children": [node(k) for k in kids]}

        roots = [s for s in spans.values() if not s["parent_id"] or s["parent_id"] not in spans]
        roots.sort(key=lambda s: s["start"])
        orphans = sorted(s["span_id"] for s in roots if s["parent_id"])
        path, cur = [], max(roots, key=lambda s: s["end"] - s["start"])
        while cur is not None:
            path.append(cur["name"])
            kids = children.get(cur["span_id"], [])
            cur = max(kids, key=lambda c: c["end"]) if kids else None
        start, end = min(s["start"] for s in spans.values()), max(s["end"] for s in spans.values())
        return {"trace_id": trace_id, "duration_ms": round((end - start) * 1000, 3), "roots": [node(r) for r in roots],
                "orphans": orphans, "critical_path": path}

    def trace_ids(self):
        with self.lock:
            return list(reversed(self.traces))
'''

TC_SERVER = r'''"""The collector over loopback HTTP.
    POST /v1/metrics {"points": [...]}     POST /v1/spans {"spans": [...]}
    GET  /v1/summary                       GET  /v1/traces          GET /v1/traces/<trace_id>
A batch is accepted item by item: the answer lists what was refused and why (400 only when nothing was accepted)."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import Collector

MAX_BODY = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "__PKG__/0.1"

    def log_message(self, fmt, *args):
        pass                        # a collector that logs every request it collects would log itself

    def _send(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        c = self.server.collector
        if self.path == "/v1/summary":
            return self._send(200, c.summary())
        if self.path == "/v1/traces":
            return self._send(200, {"traces": c.trace_ids()})
        if self.path.startswith("/v1/traces/"):
            t = c.trace(self.path[len("/v1/traces/"):])
            return self._send(200, t) if t else self._send(404, {"error": "no such trace"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        routes = {"/v1/metrics": ("points", self.server.collector.add_metric),
                  "/v1/spans": ("spans", self.server.collector.add_span)}
        if self.path not in routes:
            return self._send(404, {"error": "not found"})
        field, add = routes[self.path]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return self._send(413, {"error": f"body larger than {MAX_BODY} bytes"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        items = body.get(field) if isinstance(body, dict) else None
        if not isinstance(items, list):
            return self._send(400, {"error": f"expected {{\"{field}\": [...]}}"})
        accepted, refused = 0, []
        for i, item in enumerate(items):
            try:
                add(item)
                accepted += 1
            except ValueError as e:
                refused.append({"index": i, "reason": str(e)})
        return self._send(200 if accepted or not items else 400, {"accepted": accepted, "refused": refused})


def make_server(host="127.0.0.1", port=0, collector=None):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.collector = collector or Collector()
    return srv
'''

TC_TRACER = r'''"""The sending side: time work with `with tracer.span("name"):`, count things, then flush() one batch per kind to
the collector. Nesting is tracked, so a span opened inside another becomes its child."""
import json
import secrets
import time
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Tracer:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.trace_id = secrets.token_hex(8)
        self.spans, self.points, self._stack = [], [], []

    class _Span:
        def __init__(self, tracer, name, attrs):
            self.tracer, self.name, self.attrs = tracer, name, attrs

        def __enter__(self):
            t = self.tracer
            self.span_id = secrets.token_hex(4)
            self.parent = t._stack[-1] if t._stack else None
            self.start = t.clock()
            t._stack.append(self.span_id)
            return self

        def __exit__(self, exc_type, exc, tb):
            t = self.tracer
            t._stack.pop()
            attrs = dict(self.attrs, error=exc_type.__name__) if exc_type else dict(self.attrs)
            t.spans.append({"trace_id": t.trace_id, "span_id": self.span_id, "parent_id": self.parent, "name": self.name,
                            "start": self.start, "end": t.clock(), "attrs": attrs})
            return False

    def span(self, name, **attrs):
        return Tracer._Span(self, name, attrs)

    def count(self, name, value=1, **labels):
        self.points.append({"name": name, "kind": "counter", "value": value, "labels": labels})

    def observe(self, name, value, **labels):
        self.points.append({"name": name, "kind": "histogram", "value": value, "labels": labels})

    def flush(self, base_url, timeout=5):
        """POST what was collected. -> {"spans": answer, "points": answer}. Sent items are forgotten."""
        out = {}
        for path, field, items in (("/v1/spans", "spans", self.spans), ("/v1/metrics", "points", self.points)):
            if not items:
                continue
            req = urllib.request.Request(base_url.rstrip("/") + path, data=json.dumps({field: items}).encode(),
                                         headers={"Content-Type": "application/json"})
            with _OPENER.open(req, timeout=timeout) as r:
                out[field] = json.loads(r.read())
            items.clear()
        return out
'''

TC_MAIN = r'''import os
import sys

from .server import make_server


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else int(os.environ.get("COLLECTOR_PORT", "4319"))
    srv = make_server(port=port)
    print(f"collector on http://127.0.0.1:{srv.server_address[1]}  (POST /v1/metrics, /v1/spans; GET /v1/summary, /v1/traces)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        srv.server_close()
    return 0


sys.exit(main())
'''

TC_TEST_STORE = TEST_HEAD + r'''
import pytest  # noqa: E402

from __PKG__.store import Collector, percentile, series  # noqa: E402


def test_percentiles_are_nearest_rank():
    values = list(range(1, 101))
    assert (percentile(values, 50), percentile(values, 95), percentile(values, 99)) == (50, 95, 99)
    assert percentile([7], 99) == 7 and percentile([], 50) is None and percentile([3, 1, 2], 0) == 1


def test_counters_gauges_histograms_and_label_order():
    c = Collector()
    c.add_metric({"name": "calls", "kind": "counter", "value": 2, "labels": {"tier": "worker", "model": "m"}})
    c.add_metric({"name": "calls", "kind": "counter", "value": 3, "labels": {"model": "m", "tier": "worker"}})
    c.add_metric({"name": "vram_gb", "kind": "gauge", "value": 5.5})
    c.add_metric({"name": "vram_gb", "kind": "gauge", "value": 6.1})
    for v in (10, 20, 30, 40):
        c.add_metric({"name": "latency_ms", "kind": "histogram", "value": v})
    s = c.summary()
    assert s["counters"] == {series("calls", {"model": "m", "tier": "worker"}): 5} and s["gauges"] == {"vram_gb": 6.1}
    assert s["histograms"]["latency_ms"] == {"count": 4, "min": 10, "max": 40, "p50": 20, "p95": 40, "p99": 40}


@pytest.mark.parametrize("bad, why", [
    ({"kind": "counter", "value": 1}, "name"), ({"name": "x", "kind": "meter", "value": 1}, "kind"),
    ({"name": "x", "kind": "gauge", "value": float("nan")}, "finite"), ({"name": "x", "kind": "gauge", "value": True}, "finite"),
    ({"name": "x", "kind": "counter", "value": -1}, "only goes up"),
])
def test_bad_metrics_are_refused_with_a_reason(bad, why):
    with pytest.raises(ValueError, match=why):
        Collector().add_metric(bad)


def span(sid, parent, name, start, end, trace="t1"):
    return {"trace_id": trace, "span_id": sid, "parent_id": parent, "name": name, "start": start, "end": end}


def test_a_trace_becomes_a_tree_with_its_critical_path_and_orphans():
    c = Collector()
    c.add_span(span("a", None, "run", 0.0, 1.0))
    c.add_span(span("b", "a", "plan", 0.0, 0.2))
    c.add_span(span("c", "a", "build", 0.2, 0.9))
    c.add_span(span("d", "c", "tests", 0.5, 0.9))
    c.add_span(span("e", "lost", "late", 0.95, 1.2))
    t = c.trace("t1")
    assert t["duration_ms"] == 1200.0 and t["critical_path"] == ["run", "build", "tests"] and t["orphans"] == ["e"]
    assert [r["name"] for r in t["roots"]] == ["run", "late"]
    assert [k["name"] for k in t["roots"][0]["children"]] == ["plan", "build"] and c.trace("nope") is None
    with pytest.raises(ValueError, match="end >= start"):
        c.add_span(span("z", None, "backwards", 2.0, 1.0))


def test_traces_are_bounded_oldest_first():
    c = Collector(max_traces=2)
    for t in ("t1", "t2", "t3"):
        c.add_span(span("a", None, "x", 0, 1, trace=t))
    assert c.trace_ids() == ["t3", "t2"] and c.trace("t1") is None
'''

TC_TEST_SERVER = TEST_HEAD + r'''
import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402

from __PKG__.server import make_server  # noqa: E402
from __PKG__.tracer import Tracer  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def base():
    srv = make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url):
    try:
        with OPENER.open(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        self.t += 0.25
        return self.t


def test_a_tracer_flushes_nested_spans_and_metrics_to_a_real_collector(base):
    tr = Tracer(clock=Clock())
    with tr.span("request", route="/notes"):
        with tr.span("db"):
            tr.count("queries")
        tr.observe("latency_ms", 12.5)
    with pytest.raises(RuntimeError):
        with tr.span("broken"):
            raise RuntimeError("x")
    answers = tr.flush(base)
    assert answers["spans"] == {"accepted": 3, "refused": []} and answers["points"]["accepted"] == 2
    assert tr.spans == [] and tr.points == []
    code, t = get(f"{base}/v1/traces/{tr.trace_id}")
    assert code == 200 and t["critical_path"] == ["request", "db"] and t["orphans"] == []
    assert [r["name"] for r in t["roots"]] == ["request", "broken"]
    summary = get(base + "/v1/summary")[1]
    assert summary["counters"] == {"queries": 1} and summary["histograms"]["latency_ms"]["count"] == 1
    assert get(base + "/v1/traces")[1] == {"traces": [tr.trace_id]}


def test_a_batch_is_accepted_item_by_item(base):
    code, out = post(base + "/v1/metrics", {"points": [{"name": "ok", "kind": "gauge", "value": 1},
                                                       {"name": "bad", "kind": "gauge", "value": "high"}]})
    assert code == 200 and out == {"accepted": 1, "refused": [{"index": 1, "reason": "value must be a finite number"}]}
    assert post(base + "/v1/metrics", {"points": [{"name": "bad"}]})[0] == 400
    assert post(base + "/v1/spans", {"wrong": []})[0] == 400
    assert get(base + "/v1/traces/missing")[0] == 404 and get(base + "/v2")[0] == 404
'''

_t('telemetry_collector', 'Metrics and trace collector (loopback HTTP)',
   'A small collector for your own services and agent runs: counters, gauges and histograms (p50/p95/p99) per labelled series, spans folded into trace trees with the critical path and orphans named, bounded memory, batches accepted item by item with reasons. Ships a stdlib tracer (nested spans, counts, flush) and tests over a real loopback server.',
   'python / http.server', ['api', 'data'], {
       **COMMON,
       'README.md': '# __NAME__\n\nMetrics and traces from your own programs, collected on 127.0.0.1.\n\n    python -m __PKG__              # listens on 127.0.0.1:4319 (or COLLECTOR_PORT)\n    python -m pytest -q\n\nSend from Python with the tracer in `__PKG__/tracer.py`:\n\n    tr = Tracer()\n    with tr.span("request", route="/notes"):\n        with tr.span("db"):\n            tr.count("queries")\n        tr.observe("latency_ms", 12.5)\n    tr.flush("http://127.0.0.1:4319")\n\n| request | what it does |\n|---------|--------------|\n| `POST /v1/metrics` `{"points": [{"name", "kind", "value", "labels"}]}` | counter (adds, never negative), gauge (last value), histogram (samples) |\n| `POST /v1/spans` `{"spans": [{"trace_id", "span_id", "parent_id", "name", "start", "end"}]}` | spans, grouped by trace |\n| `GET /v1/summary` | totals, last values, count/min/max/p50/p95/p99 |\n| `GET /v1/traces/<id>` | the span tree, total duration, the critical path, orphans (spans whose parent never arrived) |\n\nA batch is checked item by item: the answer says how many were accepted and why each refused one was refused.\nMemory is bounded (`max_samples` per histogram, `max_traces`, oldest trace dropped first).\n',
       'pyproject.toml': PYPROJECT,
       '__PKG__/__init__.py': '"""__NAME__: a metrics and trace collector."""\n',
       '__PKG__/__main__.py': TC_MAIN,
       '__PKG__/store.py': TC_STORE,
       '__PKG__/server.py': TC_SERVER,
       '__PKG__/tracer.py': TC_TRACER,
       'tests/test_store.py': TC_TEST_STORE,
       'tests/test_server.py': TC_TEST_SERVER,
   }, 'python -m __PKG__')

# ================================================================================================ fts_rag
FR_LIBRARY = r'''"""The library: text files cut into chunks and kept in a SQLite FTS5 table, ranked with bm25. No vectors, no
embedding model: words in, the best passages out. A file that did not change is not indexed twice."""
import hashlib
import os
import re
import sqlite3

STOPWORDS = frozenset("a an and are as at be by do does for from how i in is it of on or the this to what when where which who why with".split())


def fts5_available():
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.close()
        return True
    except sqlite3.OperationalError:
        return False


def chunk(text, max_chars=800):
    """Paragraphs packed into chunks of at most max_chars; a longer paragraph is cut at sentence ends, and a
    sentence longer than max_chars at max_chars."""
    pieces = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not para:
            continue
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            while len(sentence) > max_chars:
                cut = sentence.rfind(" ", 0, max_chars + 1)
                cut = cut if cut > 0 else max_chars           # one enormous word: cut it anyway
                pieces.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                pieces.append(sentence)
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 1 + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def to_match(query):
    """A free-text question -> a safe FTS5 query: each word quoted, joined with OR, stopwords dropped. None when no
    word is left. Quoting means punctuation in the question can never be read as FTS5 syntax."""
    words = [w for w in re.findall(r"\w+", query.lower()) if w not in STOPWORDS]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words)) or None


class Library:
    def __init__(self, path=":memory:"):
        if not fts5_available():
            raise RuntimeError("this Python's sqlite3 was built without FTS5")
        self.con = sqlite3.connect(path)
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, chunks INTEGER NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(text, path UNINDEXED, seq UNINDEXED, tokenize='porter unicode61');
        """)

    def close(self):
        self.con.close()

    def add_text(self, path, text, max_chars=800):
        """-> "added" | "updated" | "unchanged"."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = self.con.execute("SELECT sha256 FROM docs WHERE path = ?", (path,)).fetchone()
        if row and row[0] == digest:
            return "unchanged"
        parts = chunk(text, max_chars)
        with self.con:
            self.con.execute("DELETE FROM passages WHERE path = ?", (path,))
            self.con.executemany("INSERT INTO passages (text, path, seq) VALUES (?, ?, ?)",
                                 [(p, path, i) for i, p in enumerate(parts)])
            self.con.execute("INSERT OR REPLACE INTO docs (path, sha256, chunks) VALUES (?, ?, ?)", (path, digest, len(parts)))
        return "updated" if row else "added"

    def add_folder(self, folder, exts=(".md", ".txt")):
        """-> {"added": n, "updated": n, "unchanged": n}. Paths are stored relative to the folder, with '/'."""
        seen = {"added": 0, "updated": 0, "unchanged": 0}
        for d, dirs, files in os.walk(folder):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for name in sorted(files):
                if name.lower().endswith(exts):
                    full = os.path.join(d, name)
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    seen[self.add_text(os.path.relpath(full, folder).replace(os.sep, "/"), text)] += 1
        return seen

    def search(self, query, k=5):
        """-> [{"path", "seq", "text", "score"}], best first (bm25: lower is better). [] when nothing matches."""
        match = to_match(query)
        if match is None:
            return []
        rows = self.con.execute("SELECT path, seq, text, bm25(passages) AS score FROM passages WHERE passages MATCH ? "
                                "ORDER BY score LIMIT ?", (match, k)).fetchall()
        return [{"path": p, "seq": s, "text": t, "score": round(sc, 4)} for p, s, t, sc in rows]

    def count(self):
        return self.con.execute("SELECT COUNT(*), COALESCE(SUM(chunks), 0) FROM docs").fetchone()
'''

FR_ANSWER = r'''"""Answer from the library, checked in code: no passage found means no model call and a plain "not found"; the
model must cite passages as [n]; a citation to a passage it was not given is caught, not trusted."""
import re

NOT_FOUND = "I could not find this in the documents."

SYSTEM = ("Answer only from the numbered sources. Cite every claim with its source number in square brackets, like [1]. "
          "If the sources do not contain the answer, say so.")


def build_messages(question, hits):
    """Stable part first (instructions, sources), the question last."""
    sources = "\n\n".join(f"[{i}] ({h['path']})\n{h['text']}" for i, h in enumerate(hits, start=1))
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"SOURCES\n\n{sources}\n\nQUESTION\n{question}"}]


def citations(reply, n_sources):
    """-> (cited numbers that exist, cited numbers that do not)."""
    nums = [int(n) for n in re.findall(r"\[(\d+)\]", reply)]
    good = sorted({n for n in nums if 1 <= n <= n_sources})
    bad = sorted({n for n in nums if not 1 <= n <= n_sources})
    return good, bad


def answer(library, question, chat_fn, k=4):
    """-> {"answer", "verdict", "sources", "cited", "bad_citations"}. verdict: grounded | uncited | bad_citation |
    no_sources. chat_fn(messages) -> text; it is not called when nothing was found."""
    hits = library.search(question, k)
    if not hits:
        return {"answer": NOT_FOUND, "verdict": "no_sources", "sources": [], "cited": [], "bad_citations": []}
    reply = chat_fn(build_messages(question, hits))
    good, bad = citations(reply, len(hits))
    verdict = "bad_citation" if bad else "grounded" if good else "uncited"
    return {"answer": reply, "verdict": verdict, "sources": [f"{h['path']}#{h['seq']}" for h in hits],
            "cited": [f"{hits[n - 1]['path']}#{hits[n - 1]['seq']}" for n in good], "bad_citations": bad}
'''

FR_MODEL = r'''"""The model call for `ask`: a local server speaking Ollama's /api/chat (RAG_HOST, default 127.0.0.1:11434;
RAG_MODEL). Temperature 0: the same question over the same sources gives the same answer."""
import json
import os
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def chat(messages, timeout=300):
    h = os.environ.get("RAG_HOST", "127.0.0.1:11434").rstrip("/")
    url = (h if h.startswith("http") else "http://" + h) + "/api/chat"
    body = {"model": os.environ.get("RAG_MODEL", "qwen2.5-coder:7b"), "messages": messages, "stream": False,
            "options": {"temperature": 0}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]
'''

FR_MAIN = r'''import argparse
import json
import os
import sys

from .answer import answer
from .library import Library
from .model import chat


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: questions answered from your own documents.")
    ap.add_argument("--db", default=os.environ.get("RAG_DB", "library.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="index a folder of .md/.txt files").add_argument("folder")
    p = sub.add_parser("search", help="show the best passages")
    p.add_argument("query", nargs="+")
    p.add_argument("-k", type=int, default=5)
    p = sub.add_parser("ask", help="answer from the passages with a local model")
    p.add_argument("question", nargs="+")
    args = ap.parse_args(argv)
    lib = Library(args.db)
    try:
        if args.cmd == "index":
            print(json.dumps(lib.add_folder(args.folder)))
        elif args.cmd == "search":
            for h in lib.search(" ".join(args.query), args.k):
                print(f"{h['score']:>8}  {h['path']}#{h['seq']}  {h['text'][:100]}")
        else:
            try:
                out = answer(lib, " ".join(args.question), chat)
            except OSError as e:
                print(f"error: no answer from the model server: {e}", file=sys.stderr)
                return 3
            print(out["answer"])
            print(f"\n[{out['verdict']}] cited: {', '.join(out['cited']) or 'nothing'}", file=sys.stderr)
            return 0 if out["verdict"] in ("grounded", "no_sources") else 1
    finally:
        lib.close()
    return 0


sys.exit(main())
'''

FR_DOC_LEASES = '# Leases\n\nA worker that claims a job gets a lease: the job is its own for a fixed number of seconds. While the lease is live, only\nthat worker may finish the job.\n\nIf the worker dies, nobody has to notice. The lease simply runs out and the next claim takes the job back.\n'
FR_DOC_BACKOFF = '# Backoff\n\nA failed job waits before its next attempt. The wait doubles each time: two seconds, then four, then eight, up to a cap.\n\nAfter the last attempt the job is dead. Every error is kept on it so a person can read what went wrong.\n'

FR_TEST_LIBRARY = TEST_HEAD + r'''
import pytest  # noqa: E402

from __PKG__.library import Library, chunk, fts5_available, to_match  # noqa: E402

if not fts5_available():
    pytest.skip("this Python's sqlite3 has no FTS5", allow_module_level=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_chunks_respect_the_limit_and_keep_every_word():
    text = "First paragraph here.\n\nSecond one. It has two sentences.\n\n" + "word " * 300
    parts = chunk(text, max_chars=120)
    assert all(len(p) <= 120 for p in parts) and parts[0].startswith("First paragraph here. Second one.")
    assert " ".join(parts).split() == text.split()


def test_questions_become_safe_fts_queries():
    assert to_match("What is a lease?") == '"lease"'
    assert to_match('C++ "quotes" AND (NEAR) -x*') == '"c" OR "quotes" OR "near" OR "x"'
    assert to_match("what is the") is None


def test_the_relevant_file_ranks_first_and_unchanged_files_are_skipped(tmp_path):
    lib = Library(str(tmp_path / "lib.db"))
    assert lib.add_folder(os.path.join(ROOT, "docs")) == {"added": 2, "updated": 0, "unchanged": 0}
    assert lib.search("when does a lease run out?")[0]["path"] == "leases.md"
    assert lib.search("how long does a failed job wait")[0]["path"] == "backoff.md"
    assert lib.search("waits doubling")[0]["path"] == "backoff.md"        # porter stemming: waits/wait, doubling/doubles
    assert lib.add_folder(os.path.join(ROOT, "docs")) == {"added": 0, "updated": 0, "unchanged": 2}
    assert lib.search("photosynthesis") == [] and lib.search("?!") == []
    lib.close()


def test_a_changed_file_replaces_its_old_passages():
    lib = Library()
    assert lib.add_text("a.md", "apples are red") == "added"
    assert lib.add_text("a.md", "bananas are yellow") == "updated"
    assert lib.search("apples") == [] and lib.search("bananas")[0]["path"] == "a.md"
    assert lib.count() == (1, 1)
    lib.close()
'''

FR_TEST_ANSWER = TEST_HEAD + r'''
import pytest  # noqa: E402

from __PKG__.answer import NOT_FOUND, answer, build_messages, citations  # noqa: E402
from __PKG__.library import Library, fts5_available  # noqa: E402

if not fts5_available():
    pytest.skip("this Python's sqlite3 has no FTS5", allow_module_level=True)


@pytest.fixture
def lib():
    library = Library()
    library.add_text("leases.md", "A lease gives a worker the job for a fixed time. When the lease runs out the job goes back.")
    library.add_text("backoff.md", "A failed job waits before the next attempt; the wait doubles each time.")
    yield library
    library.close()


class Model:
    def __init__(self, reply):
        self.reply, self.seen = reply, []

    def __call__(self, messages):
        self.seen.append(messages)
        return self.reply


def test_nothing_found_means_no_model_call(lib):
    model = Model("should not be asked")
    out = answer(lib, "photosynthesis in plants", model)
    assert out["answer"] == NOT_FOUND and out["verdict"] == "no_sources" and model.seen == []


def test_a_cited_answer_is_grounded_and_names_its_passage(lib):
    model = Model("The job goes back when the lease runs out [1].")
    out = answer(lib, "what happens when a lease runs out?", model)
    assert out["verdict"] == "grounded" and out["cited"] == ["leases.md#0"]
    user = model.seen[0][1]["content"]
    assert user.startswith("SOURCES") and user.rstrip().endswith("what happens when a lease runs out?")


def test_uncited_and_invented_citations_are_caught(lib):
    assert answer(lib, "lease", Model("Leases are timed."))["verdict"] == "uncited"
    out = answer(lib, "lease", Model("Leases are timed [1][7]."))
    assert out["verdict"] == "bad_citation" and out["bad_citations"] == [7]


def test_citations_and_messages():
    assert citations("a [1] b [2] c [1] d [0] e [9]", 2) == ([1, 2], [0, 9])
    msgs = build_messages("q?", [{"path": "x.md", "seq": 0, "text": "t"}])
    assert msgs[0]["role"] == "system" and "[1] (x.md)\nt" in msgs[1]["content"]
'''

_t('fts_rag', 'Small RAG over SQLite FTS5 (no vectors)',
   'Questions answered from your own documents: files cut into chunks in an SQLite FTS5 table with bm25 ranking and porter stemming, safe query building, re-indexing only what changed, and answers checked in code (no passage found = no model call; every [n] citation must point at a passage the model was given).',
   'python / sqlite fts5', ['data', 'api'], {
       **COMMON,
       'README.md': '# __NAME__\n\nAsk questions of your own documents with a local model. Retrieval is SQLite FTS5 (bm25 ranking, porter\nstemming): no vectors, no embedding model, nothing to download.\n\n    python -m __PKG__ index docs\n    python -m __PKG__ search "when does a lease run out"\n    python -m __PKG__ ask "what happens when a worker dies?"\n    python -m pytest -q        # no model needed: the model is a function the tests replace\n\nWhat code checks, not the model:\n\n- **Nothing found, nothing asked.** When no passage matches, the answer is "I could not find this in the\n  documents." and the model is never called.\n- **Citations are verified.** The model gets numbered passages and must cite them as `[n]`. The verdict is\n  `grounded` (cites real passages), `uncited`, or `bad_citation` (cites a number it was never given).\n- **Questions cannot break the query.** Every word is quoted before it reaches FTS5, so `C++ (draft) -x*`\n  is just words.\n- **Unchanged files are skipped** (sha256 per file); a changed file replaces all its old passages.\n\nSettings: `RAG_DB` (default `library.db`), `RAG_HOST` (default 127.0.0.1:11434, a server speaking\n`/api/chat`), `RAG_MODEL`. If your Python\'s sqlite3 lacks FTS5, `Library()` says so and the tests skip.\n',
       'pyproject.toml': PYPROJECT,
       'docs/leases.md': FR_DOC_LEASES,
       'docs/backoff.md': FR_DOC_BACKOFF,
       '__PKG__/__init__.py': '"""__NAME__: answers from your own documents, over SQLite FTS5."""\n',
       '__PKG__/__main__.py': FR_MAIN,
       '__PKG__/library.py': FR_LIBRARY,
       '__PKG__/answer.py': FR_ANSWER,
       '__PKG__/model.py': FR_MODEL,
       'tests/test_library.py': FR_TEST_LIBRARY,
       'tests/test_answer.py': FR_TEST_ANSWER,
   }, 'python -m __PKG__ search lease')


# ================================================================================================ suggestion keywords
# Phrases specific enough to win over the broad words in arch_templates.KEYWORDS ("api", "queue", "agent", "cli").
KEYWORDS.update({
    'tool_server': r"\b(tool server|tools? over http|http tools?|local tool (?:server|api)|function[- ]calling server|tool endpoint)\b",
    'agent_fleet': r"\b(agent fleet|fleet of agents|fleet runner|multi[- ]agent (?:runner|orchestrat\w*)|several agents|parallel agents|agents in parallel)\b",
    'lease_queue': r"\b(leases?|leased jobs?|job leases?|visibility timeout|lease[- ]based|at[- ]least[- ]once)\b",
    'api_gateway': r"\b(api gateway|gateway|rate[- ]limit(?:s|ed|ing|er)?|throttl\w+|token bucket|reverse proxy)\b",
    'event_store': r"\b(event[- ]sourc\w*|event store|append[- ]only (?:log|store)|cqrs|optimistic concurrency)\b",
    'model_cli': r"\b((?:llm|model|ollama) cli|cli (?:over|for|around) (?:a |my )?(?:local )?(?:model|llm)|ask (?:a |my )?local model|reply cache|prompt cache)\b",
    'telemetry_collector': r"\b(metrics? collector|trace collector|telemetry|tracing|traces and metrics|observability|p95|p99)\b",
    'fts_rag': r"\b(rag|retrieval[- ]augmented|fts5|full[- ]text search|search (?:my|the) (?:docs|documents|notes)|questions? (?:over|about) (?:my|the) (?:docs|documents|notes))\b",
})

_base_suggest = T.suggest


def suggest(goal):
    """These starters are more specific than the broad words the base list checks first ("api", "agent", "cli",
    "queue"), so their phrases are tried before it; anything else goes to arch_templates' own order unchanged."""
    g = (goal or "").lower()
    for tid in EXTRA_IDS:
        m = re.search(KEYWORDS[tid], g)
        if m:
            return tid, f"goal mentions '{m.group(0)}'"
    return _base_suggest(goal)


suggest.extra = True
if not getattr(T.suggest, "extra", False):
    T.suggest = suggest
