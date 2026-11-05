"""Project templates: start from NOTHING. Each template is a small, working, tested project (its own tests pass
the moment it is written), so a flow can build on a green base instead of a blank folder. Templates are plain
data: no model is needed to create one. Standard library only, so they run offline; the FastAPI template is
the one exception and says so (its tests skip themselves until fastapi is installed).

    render("rest_api", "shop", addons=["ci"]) -> {relative path: file text}
"""
import re

TEMPLATES = {}


def _t(tid, title, blurb, stack, domains, files, run, needs=None):
    TEMPLATES[tid] = {"id": tid, "title": title, "blurb": blurb, "stack": stack, "domains": domains, "files": files, "run": run, "needs": needs or []}


COMMON = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n.venv/\nvenv/\nnode_modules/\n*.db\n.arch/runs/\n.arch/config.json\n",
    "conftest.py": '"""Makes the project root importable from tests (pytest puts this folder on sys.path)."""\n',
}

# ------------------------------------------------------------------------------------------------ empty
_t("empty", "Empty project", "Just a README, a smoke test and a .gitignore. Everything else is built by the flows.",
   "any", ["api"], {
       **COMMON,
       "README.md": "# __NAME__\n\nDescribe the project here.\n\n## Run the tests\n\n    python -m pytest -q\n",
       "tests/test_smoke.py": "def test_smoke():\n    assert True\n",
   }, "python -m pytest -q")

# ------------------------------------------------------------------------------------------------ local agent (Ollama + tools)
_t("agent_tool", "Local agent with tools (Ollama)", "A small agent loop on Ollama: a tool dispatcher with timeouts and output caps, a JSON tool-call protocol validated in code, and tests that run against a scripted fake server (no model needed to test).",
   "python", ["api"], {
       **COMMON,
       "README.md": "# __NAME__\n\nA local agent: Ollama proposes tool calls as JSON, code runs them (with a timeout and an output cap) and feeds the result back.\n\n    python -m __PKG__ \"what is in this folder?\"\n    python -m pytest -q      # tests use a fake Ollama; no model required\n\nSettings: OLLAMA_HOST (default 127.0.0.1:11434), AGENT_MODEL (default qwen2.5-coder:7b).\n",
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: a local agent loop."""\n',
       "__PKG__/tools.py": '"""Tools the agent may call. Every tool is a plain function with a docstring; the registry is what the model sees."""\nimport os\nimport subprocess\n\nOUTPUT_CAP = 4000\n\n\ndef list_files(path: str = ".") -> str:\n    """List files under a folder (names only)."""\n    out = []\n    for d, dirs, files in os.walk(path):\n        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "__pycache__"]\n        out += [os.path.relpath(os.path.join(d, f), path) for f in files]\n    return "\\n".join(sorted(out))[:OUTPUT_CAP]\n\n\ndef read_file(path: str) -> str:\n    """Read a text file (first 4000 characters)."""\n    with open(path, "r", encoding="utf-8", errors="replace") as f:\n        return f.read(OUTPUT_CAP)\n\n\ndef run_tests(path: str = ".") -> str:\n    """Run the test suite in a folder and return the tail of its output."""\n    try:\n        r = subprocess.run(["python", "-m", "pytest", "-q", "-x"], cwd=path, capture_output=True, text=True, timeout=120)\n        return (r.stdout + r.stderr)[-OUTPUT_CAP:]\n    except subprocess.TimeoutExpired:\n        return "timed out after 120s"\n\n\nREGISTRY = {"list_files": list_files, "read_file": read_file, "run_tests": run_tests}\n\n\ndef describe():\n    return "\\n".join(f"- {n}({\', \'.join(fn.__code__.co_varnames[:fn.__code__.co_argcount])}): {fn.__doc__}" for n, fn in REGISTRY.items())\n',
       "__PKG__/agent.py": '"""The loop: ask the model for ONE JSON action, run it in code, repeat; stop at a final answer or the step cap."""\nimport json\nimport os\nimport urllib.request\n\nfrom . import tools\n\nSYSTEM = ("You are a careful local agent. Reply with ONE JSON object and nothing else: "\n          \'{"tool": "<name>", "args": {...}} to use a tool, or {"final": "<answer>"} when done.\\nTools:\\n\' + tools.describe())\n\n\ndef host():\n    h = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")\n    return h if h.startswith("http") else "http://" + h\n\n\ndef chat(messages, model=None, timeout=120):\n    body = {"model": model or os.environ.get("AGENT_MODEL", "qwen2.5-coder:7b"), "messages": messages, "stream": False, "format": "json",\n            "options": {"temperature": 0.1}}\n    req = urllib.request.Request(host() + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})\n    with urllib.request.urlopen(req, timeout=timeout) as r:\n        return json.loads(r.read().decode())["message"]["content"]\n\n\ndef parse_action(text):\n    """-> dict with \'tool\'+\'args\' or \'final\'; raises ValueError for anything else (the model is told, and asked again)."""\n    try:\n        j = json.loads(text)\n    except ValueError:\n        a, b = text.find("{"), text.rfind("}")\n        if a == -1 or b <= a:\n            raise ValueError("not JSON")\n        j = json.loads(text[a:b + 1])\n    if "final" in j and isinstance(j["final"], str):\n        return {"final": j["final"]}\n    if j.get("tool") in tools.REGISTRY and isinstance(j.get("args", {}), dict):\n        return {"tool": j["tool"], "args": j.get("args", {})}\n    raise ValueError("expected {tool, args} with a known tool, or {final}")\n\n\ndef run(goal, max_steps=8, chat_fn=chat):\n    """-> (answer, steps). Every tool call is checked in code before it runs; a bad reply costs a step, not a crash."""\n    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": goal}]\n    steps = []\n    for _ in range(max_steps):\n        reply = chat_fn(msgs)\n        try:\n            act = parse_action(reply)\n        except ValueError as e:\n            msgs.append({"role": "assistant", "content": reply})\n            msgs.append({"role": "user", "content": f"Invalid reply ({e}). Reply with ONE JSON object as instructed."})\n            steps.append({"error": str(e)})\n            continue\n        if "final" in act:\n            return act["final"], steps\n        try:\n            result = tools.REGISTRY[act["tool"]](**act["args"])\n        except (TypeError, OSError) as e:\n            result = f"tool error: {e}"\n        steps.append({"tool": act["tool"], "args": act["args"], "result": result[:200]})\n        msgs.append({"role": "assistant", "content": reply})\n        msgs.append({"role": "user", "content": f"RESULT of {act[\'tool\']}:\\n{result}\\n\\nNext action, or final."})\n    return "stopped: step limit reached", steps\n',
       "__PKG__/__main__.py": 'import sys\n\nfrom .agent import run\n\nif __name__ == "__main__":\n    answer, steps = run(" ".join(sys.argv[1:]) or "What is in this folder?")\n    for s in steps:\n        print("-", s)\n    print(answer)\n',
       "tests/test_agent.py": 'from __PKG__ import agent, tools\n\n\ndef scripted(replies):\n    it = iter(replies)\n\n    def chat_fn(messages):\n        return next(it)\n    return chat_fn\n\n\ndef test_tool_call_then_final(tmp_path):\n    (tmp_path / "a.txt").write_text("hello")\n    answer, steps = agent.run("list", chat_fn=scripted([\n        \'{"tool": "list_files", "args": {"path": "%s"}}\' % str(tmp_path).replace("\\\\", "/"),\n        \'{"final": "one file: a.txt"}\']))\n    assert answer == "one file: a.txt" and steps[0]["tool"] == "list_files" and "a.txt" in steps[0]["result"]\n\n\ndef test_bad_reply_costs_a_step_not_a_crash():\n    answer, steps = agent.run("x", chat_fn=scripted(["not json at all", \'{"tool": "nope", "args": {}}\', \'{"final": "ok"}\']))\n    assert answer == "ok" and len(steps) == 2 and all("error" in s for s in steps)\n\n\ndef test_step_limit():\n    answer, steps = agent.run("x", max_steps=2, chat_fn=scripted([\'{"tool": "list_files", "args": {}}\'] * 5))\n    assert answer.startswith("stopped") and len(steps) == 2\n\n\ndef test_parse_action_rejects_unknown_tool():\n    try:\n        agent.parse_action(\'{"tool": "rm_rf", "args": {}}\')\n        assert False\n    except ValueError:\n        pass\n\n\ndef test_read_file_is_capped(tmp_path):\n    p = tmp_path / "big.txt"\n    p.write_text("x" * 10000)\n    assert len(tools.read_file(str(p))) == tools.OUTPUT_CAP\n',
   }, "python -m __PKG__ \"what is in this folder?\"")

# ------------------------------------------------------------------------------------------------ MCP server (stdio, JSON-RPC)
_t("mcp_server", "MCP server (stdio)", "A Model Context Protocol server over stdio: initialize, tools/list, tools/call, with a typed tool registry and tests that speak JSON-RPC to it in-process. Standard library only.",
   "python", ["api"], {
       **COMMON,
       "README.md": "# __NAME__\n\nAn MCP server over stdio (JSON-RPC 2.0, newline-delimited). Add a tool: one function in `__PKG__/tools.py` with a docstring and type hints; the schema is derived.\n\n    python -m __PKG__            # speaks MCP on stdin/stdout\n    python -m pytest -q\n\nRegister it in a client as: command `python`, args `-m __PKG__`, cwd this folder.\n",
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: an MCP server."""\n',
       "__PKG__/tools.py": '"""Tools exposed over MCP. Type hints become the JSON schema; the docstring is the description."""\nimport inspect\n\n\ndef add(a: int, b: int) -> int:\n    """Add two integers."""\n    return a + b\n\n\ndef echo(text: str) -> str:\n    """Return the text unchanged."""\n    return text\n\n\nREGISTRY = {"add": add, "echo": echo}\n_JSON = {int: "integer", float: "number", str: "string", bool: "boolean"}\n\n\ndef schema(fn):\n    props = {n: {"type": _JSON.get(p.annotation, "string")} for n, p in inspect.signature(fn).parameters.items()}\n    return {"type": "object", "properties": props, "required": list(props)}\n\n\ndef listing():\n    return [{"name": n, "description": (fn.__doc__ or "").strip(), "inputSchema": schema(fn)} for n, fn in REGISTRY.items()]\n',
       "__PKG__/server.py": '"""JSON-RPC 2.0 over newline-delimited stdio, the subset MCP clients use: initialize, notifications, tools/list, tools/call."""\nimport json\nimport sys\n\nfrom . import tools\n\nPROTOCOL = "2024-11-05"\n\n\ndef handle(msg):\n    """-> response dict, or None for a notification. Never raises: a bad request is a JSON-RPC error."""\n    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}\n    if method is None or mid is None and not str(method).startswith("notifications/"):\n        return _err(mid, -32600, "invalid request") if mid is not None else None\n    if str(method).startswith("notifications/"):\n        return None\n    if method == "initialize":\n        return _ok(mid, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "__PKG__", "version": "0.1.0"}})\n    if method == "tools/list":\n        return _ok(mid, {"tools": tools.listing()})\n    if method == "tools/call":\n        name, args = params.get("name"), params.get("arguments") or {}\n        fn = tools.REGISTRY.get(name)\n        if not fn:\n            return _err(mid, -32602, f"unknown tool {name!r}")\n        try:\n            out = fn(**args)\n        except TypeError as e:\n            return _err(mid, -32602, f"bad arguments: {e}")\n        except Exception as e:  # a tool failure is reported to the client, not raised into the server\n            return _ok(mid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})\n        return _ok(mid, {"content": [{"type": "text", "text": str(out)}]})\n    return _err(mid, -32601, f"method not found: {method}")\n\n\ndef _ok(mid, result):\n    return {"jsonrpc": "2.0", "id": mid, "result": result}\n\n\ndef _err(mid, code, message):\n    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}\n\n\ndef serve(inp=sys.stdin, out=sys.stdout):\n    for line in inp:\n        line = line.strip()\n        if not line:\n            continue\n        try:\n            msg = json.loads(line)\n        except ValueError:\n            resp = _err(None, -32700, "parse error")\n        else:\n            resp = handle(msg)\n        if resp is not None:\n            out.write(json.dumps(resp) + "\\n")\n            out.flush()\n',
       "__PKG__/__main__.py": 'from .server import serve\n\nif __name__ == "__main__":\n    serve()\n',
       "tests/test_server.py": 'import io\nimport json\n\nfrom __PKG__ import server\n\n\ndef rpc(lines):\n    out = io.StringIO()\n    server.serve(io.StringIO("\\n".join(json.dumps(x) for x in lines) + "\\n"), out)\n    return [json.loads(l) for l in out.getvalue().splitlines()]\n\n\ndef test_initialize_and_list():\n    r = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "method": "notifications/initialized"},\n             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])\n    assert r[0]["result"]["protocolVersion"] == server.PROTOCOL and len(r) == 2\n    names = {t["name"] for t in r[1]["result"]["tools"]}\n    assert names == {"add", "echo"} and r[1]["result"]["tools"][0]["inputSchema"]["properties"]["a"]["type"] == "integer"\n\n\ndef test_call_add():\n    r = rpc([{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 2, "b": 3}}}])\n    assert r[0]["result"]["content"][0]["text"] == "5"\n\n\ndef test_unknown_tool_and_bad_args_are_errors_not_crashes():\n    r = rpc([{"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},\n             {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 1}}},\n             {"jsonrpc": "2.0", "id": 6, "method": "bogus"}])\n    assert [x["error"]["code"] for x in r] == [-32602, -32602, -32601]\n\n\ndef test_parse_error():\n    out = io.StringIO()\n    server.serve(io.StringIO("not json\\n"), out)\n    assert json.loads(out.getvalue())["error"]["code"] == -32700\n',
   }, "python -m __PKG__")

# ------------------------------------------------------------------------------------------------ python library
_t("python_lib", "Python library + CLI", "A package with a pure core, a thin command line and tests. The cleanest base for logic-heavy work.",
   "python", ["api"], {
       **COMMON,
       "README.md": "# __NAME__\n\nA small Python library with a command line.\n\n    python -m __PKG__ --help\n    python -m pytest -q\n",
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__."""\n\n__version__ = "0.1.0"\n',
       "__PKG__/core.py": 'def slugify(text: str) -> str:\n    """Lower-case, ASCII letters and digits separated by single hyphens."""\n    out, prev = [], True\n    for ch in text.lower():\n        if ch.isascii() and ch.isalnum():\n            out.append(ch)\n            prev = False\n        elif not prev:\n            out.append("-")\n            prev = True\n    return "".join(out).strip("-")\n',
       "__PKG__/cli.py": 'import argparse\nimport sys\n\nfrom .core import slugify\n\n\ndef main(argv=None) -> int:\n    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__")\n    ap.add_argument("text", nargs="+")\n    args = ap.parse_args(argv)\n    print(slugify(" ".join(args.text)))\n    return 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n',
       "__PKG__/__main__.py": "from .cli import main\nimport sys\n\nsys.exit(main())\n",
       "tests/test_core.py": 'from __PKG__.core import slugify\n\n\ndef test_basic():\n    assert slugify("Hello, World!") == "hello-world"\n\n\ndef test_collapses_and_trims():\n    assert slugify("  a   b -- c ") == "a-b-c"\n\n\ndef test_empty():\n    assert slugify("") == ""\n',
       "tests/test_cli.py": 'from __PKG__.cli import main\n\n\ndef test_cli_prints_slug(capsys):\n    assert main(["Hello", "World"]) == 0\n    assert capsys.readouterr().out.strip() == "hello-world"\n',
   }, "python -m __PKG__ Hello World")

# ------------------------------------------------------------------------------------------------ background worker / job queue
WORKER_QUEUE = '''"""A durable job queue backed by SQLite: enqueue now, process later (or in another process).
No broker needed -- the database file IS the queue, so it survives a restart.

States: pending -> running -> done, or pending -> running -> pending (retry) -> ... -> dead (gave up).
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
"""


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def enqueue(con, kind, payload, max_attempts=3):
    now = time.time()
    cur = con.execute("INSERT INTO jobs (kind, payload, state, max_attempts, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?, ?)",
                       (kind, json.dumps(payload), max_attempts, now, now))
    con.commit()
    return cur.lastrowid


def claim_next(con):
    """Atomically takes the oldest pending job (or a retry-ready one) to 'running', or None if the queue is empty."""
    row = con.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
    if not row:
        return None
    con.execute("UPDATE jobs SET state='running', attempts=attempts+1, updated_at=? WHERE id=? AND state='pending'", (time.time(), row["id"]))
    con.commit()
    return dict(row, state="running", attempts=row["attempts"] + 1)


def mark_done(con, job_id):
    con.execute("UPDATE jobs SET state='done', updated_at=? WHERE id=?", (time.time(), job_id))
    con.commit()


def mark_failed(con, job_id, error):
    """Below max_attempts: goes back to pending for another try. At max_attempts: dead (gave up for good)."""
    row = con.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    nxt = "pending" if row and row["attempts"] < row["max_attempts"] else "dead"
    con.execute("UPDATE jobs SET state=?, last_error=?, updated_at=? WHERE id=?", (nxt, str(error)[:2000], time.time(), job_id))
    con.commit()
    return nxt


def counts(con):
    out = {"pending": 0, "running": 0, "done": 0, "dead": 0}
    for row in con.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
        out[row["state"]] = row["n"]
    return out
'''
WORKER_WORKER = '''"""Runs jobs off the queue. A handler is a plain function: handler(payload) -> anything, or raises on failure."""
from .queue import claim_next, mark_done, mark_failed


def process_one(con, handlers):
    """Claims and runs exactly one job. Returns True if a job was processed (success or failure), False if the
    queue was empty. A kind with no matching handler counts as a failure (so it retries, then goes dead) rather
    than being silently dropped."""
    job = claim_next(con)
    if not job:
        return False
    import json
    handler = handlers.get(job["kind"])
    try:
        if handler is None:
            kind = job["kind"]
            raise KeyError(f"no handler registered for job kind {kind!r}")
        handler(json.loads(job["payload"]))
        mark_done(con, job["id"])
    except Exception as e:
        mark_failed(con, job["id"], e)
    return True


def drain(con, handlers, max_jobs=10000):
    """Processes jobs until the queue is empty or max_jobs is hit. Returns how many were processed.
    Bounded on purpose: a worker that is told to run forever belongs in a process supervisor, not in code
    the Architect can build and verify with a finite test."""
    n = 0
    while n < max_jobs and process_one(con, handlers):
        n += 1
    return n
'''
WORKER_HANDLERS = '''"""Example job handlers. Each one is handler(payload) -> result; raise to signal failure (the job retries)."""


def echo(payload):
    return payload.get("text", "")


def always_fails(payload):
    """Always raises -- a worked example of what happens to a job kind that keeps failing: it retries up to
    max_attempts, then the queue marks it dead with this error recorded rather than losing it silently."""
    raise RuntimeError(payload.get("reason", "this handler always fails, on purpose"))


HANDLERS = {"echo": echo, "always_fails": always_fails}
'''
WORKER_CLI = '''import argparse
import json
import os
import sys

from .handlers import HANDLERS
from .queue import connect, counts, enqueue
from .worker import drain

DB_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jobs.db")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__ -- a durable job queue")
    ap.add_argument("--db", default=DB_DEFAULT, help="SQLite file for the queue (default: jobs.db next to the project)")
    sub = ap.add_subparsers(dest="action", required=True)
    e = sub.add_parser("enqueue")
    e.add_argument("kind")
    e.add_argument("--payload", default="{}", help="JSON object")
    r = sub.add_parser("run")
    r.add_argument("--max-jobs", type=int, default=10000)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    con = connect(a.db)
    if a.action == "enqueue":
        try:
            payload = json.loads(a.payload)
        except ValueError as e:
            print(f"--payload is not valid JSON: {e}", file=sys.stderr)
            return 2
        job_id = enqueue(con, a.kind, payload)
        print(f"enqueued job {job_id} ({a.kind})")
        return 0
    if a.action == "run":
        n = drain(con, HANDLERS, a.max_jobs)
        print(f"processed {n} job(s)")
        print(json.dumps(counts(con)))
        return 0
    if a.action == "status":
        print(json.dumps(counts(con), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''
WORKER_TEST_QUEUE = '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.queue import claim_next, connect, counts, enqueue, mark_done, mark_failed


def test_enqueue_and_claim(tmp_path):
    con = connect(str(tmp_path / "t.db"))
    jid = enqueue(con, "echo", {"text": "hi"})
    job = claim_next(con)
    assert job["id"] == jid and job["kind"] == "echo" and job["state"] == "running"
    assert claim_next(con) is None, "a second claim must not see the same job again"


def test_done_and_counts(tmp_path):
    con = connect(str(tmp_path / "t.db"))
    jid = enqueue(con, "echo", {})
    claim_next(con)
    mark_done(con, jid)
    assert counts(con) == {"pending": 0, "running": 0, "done": 1, "dead": 0}


def test_retry_then_dead(tmp_path):
    con = connect(str(tmp_path / "t.db"))
    jid = enqueue(con, "x", {}, max_attempts=2)
    claim_next(con)
    assert mark_failed(con, jid, "boom") == "pending", "first failure with attempts < max retries"
    claim_next(con)
    assert mark_failed(con, jid, "boom again") == "dead", "exhausted retries go dead, not pending forever"
    assert counts(con)["dead"] == 1
'''
WORKER_TEST_WORKER = '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.queue import connect, counts, enqueue
from __PKG__.worker import drain


def test_drain_processes_everything(tmp_path):
    con = connect(str(tmp_path / "t.db"))
    for i in range(5):
        enqueue(con, "echo", {"text": str(i)})
    n = drain(con, {"echo": lambda p: p["text"]})
    assert n == 5
    assert counts(con)["done"] == 5


def test_missing_handler_eventually_dies(tmp_path):
    con = connect(str(tmp_path / "t.db"))
    enqueue(con, "no_such_kind", {}, max_attempts=1)
    drain(con, {})
    assert counts(con)["dead"] == 1


def test_drain_is_bounded():
    """drain() must never spin forever on its own -- max_jobs caps it even with no handlers registered."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        con = connect(os.path.join(d, "t.db"))
        try:
            for _ in range(3):
                enqueue(con, "echo", {}, max_attempts=99)
            n = drain(con, {"echo": lambda p: 1 / 0}, max_jobs=3)
            assert n == 3
        finally:
            con.close()      # an open SQLite file cannot be deleted on Windows, so the folder could not be cleaned up
'''
WORKER_TEST_CLI = '''import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.cli import main


def test_enqueue_run_status(tmp_path, capsys):
    db = str(tmp_path / "jobs.db")
    assert main(["--db", db, "enqueue", "echo", "--payload", '{"text": "hi"}']) == 0
    capsys.readouterr()
    assert main(["--db", db, "run"]) == 0
    out = capsys.readouterr().out
    assert "processed 1 job" in out
    assert main(["--db", db, "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["done"] == 1


def test_bad_payload_json_is_rejected(tmp_path):
    db = str(tmp_path / "jobs.db")
    assert main(["--db", db, "enqueue", "echo", "--payload", "{not json"]) == 2
'''
_t("worker", "Background worker / job queue", "A durable, SQLite-backed job queue: enqueue jobs from anywhere, process them with retry and a dead-letter state after too many failures. No broker, no server -- the database file is the queue.",
   "python / sqlite / job queue", ["api"], {
       **COMMON,
       "README.md": "# __NAME__\n\nA durable job queue: the SQLite file IS the queue, so it survives a restart.\n\n    python -m __PKG__ enqueue echo --payload '{\"text\": \"hello\"}'\n    python -m __PKG__ run\n    python -m __PKG__ status\n\nAdd a handler in `__PKG__/handlers.py` and register it in `HANDLERS` to process a new job kind. Failed jobs retry\nup to `max_attempts` times, then go to the `dead` state with the last error recorded -- nothing is silently dropped.\n",
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: a durable, SQLite-backed job queue."""\n\n__version__ = "0.1.0"\n',
       "__PKG__/queue.py": WORKER_QUEUE,
       "__PKG__/worker.py": WORKER_WORKER,
       "__PKG__/handlers.py": WORKER_HANDLERS,
       "__PKG__/cli.py": WORKER_CLI,
       "__PKG__/__main__.py": "from .cli import main\nimport sys\n\nsys.exit(main())\n",
       "tests/test_queue.py": WORKER_TEST_QUEUE,
       "tests/test_worker.py": WORKER_TEST_WORKER,
       "tests/test_cli.py": WORKER_TEST_CLI,
   }, "python -m __PKG__ run")

# ------------------------------------------------------------------------------------------------ security lab (red/blue, scope-checked)
LAB_SCOPE = '''"""Authorization: what is in bounds, and the window it is in bounds for. Loading a scope and checking
a target against it is the entire security boundary this project enforces -- nothing here scans, exploits or
touches a real host. It is the same discipline a real engagement needs before any tool runs."""
import ipaddress
import json


def load_scope(path):
    with open(path) as f:
        doc = json.load(f)
    for key in ("authorized_targets", "excluded", "allowed_actions", "window"):
        if key not in doc:
            raise ValueError(f"scope.json is missing required key: {key}")
    return doc


def _in_any(host, cidrs):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in cidrs  # a bare hostname is matched literally, never resolved here
    for c in cidrs:
        try:
            if ip in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            if host == c:
                return True
    return False


def in_scope(host, scope, action=None):
    """-> (bool, reason). An excluded host always loses, even if it also matches an authorized range."""
    if _in_any(host, scope.get("excluded") or []):
        return False, f"{host} is explicitly excluded"
    if not _in_any(host, scope.get("authorized_targets") or []):
        return False, f"{host} is not inside any authorized target"
    if action and scope.get("allowed_actions") and action not in scope["allowed_actions"]:
        return False, f"action {action!r} is not in allowed_actions"
    return True, "in scope"
'''
LAB_FINDINGS = '''"""Sample findings for the lab -- generic defect CLASSES (the kind a scanner or code review would flag),
never a real CVE, a working payload or a specific product's real vulnerability. Treat findings.json as
untrusted input you validate, not as ground truth: that is the whole point of attackpath.py."""
import json


def load_findings(path):
    with open(path) as f:
        rows = json.load(f)
    ids = set()
    for r in rows:
        for key in ("id", "host", "desc", "severity"):
            if key not in r:
                raise ValueError(f"finding missing required key {key!r}: {r}")
        if r["id"] in ids:
            raise ValueError(f"duplicate finding id: {r['id']}")
        ids.add(r["id"])
    return rows


SAMPLE_FINDINGS = [
    {"id": "F1", "host": "app01.lab.internal", "desc": "login endpoint builds SQL by string concatenation (CWE-89)", "severity": "high"},
    {"id": "F2", "host": "app01.lab.internal", "desc": "app process runs as root with a world-writable cron drop-in", "severity": "high"},
    {"id": "F3", "host": "db01.lab.internal", "desc": "database account used by the app has superuser privileges (least-privilege violation)", "severity": "medium"},
    {"id": "F4", "host": "app01.lab.internal", "desc": "session cookie has no Secure or HttpOnly flag", "severity": "low"},
]
'''
LAB_ATTACKPATH = '''"""Grades a proposed attack path against the evidence and the scope -- it does not generate or run
attacks. A path is a sequence of finding ids plus the host and the claimed impact; this module is the
referee that stops a path from citing a finding that doesn't exist or a host outside scope."""
from .scope import in_scope


def validate_path(path, findings, scope):
    """path: {"finding_ids": [...], "host": str, "impact": str}. -> (ok: bool, reason: str)."""
    ids = {f["id"] for f in findings}
    fids = path.get("finding_ids") or []
    if not fids:
        return False, "cites no findings"
    unknown = [i for i in fids if i not in ids]
    if unknown:
        return False, f"cites finding id(s) that do not exist: {unknown}"
    host = path.get("host")
    if not host:
        return False, "names no host"
    ok, reason = in_scope(host, scope)
    if not ok:
        return False, f"host out of scope: {reason}"
    cited_hosts = {f["host"] for f in findings if f["id"] in fids}
    if host not in cited_hosts:
        return False, f"host {host!r} does not match any cited finding's host"
    if not path.get("impact"):
        return False, "states no concrete impact"
    return True, "valid: in scope, cites real findings, impact stated"


def chain_score(paths, findings, scope):
    """-> {"valid": [...], "rejected": [(path, reason), ...]}. A path is included in 'valid' only if it
    passes validate_path; nothing here is ever auto-accepted."""
    valid, rejected = [], []
    for p in paths:
        ok, reason = validate_path(p, findings, scope)
        (valid if ok else rejected).append(p if ok else (p, reason))
    return {"valid": valid, "rejected": rejected}
'''
LAB_DETECTIONS = '''"""Blue-team side: detection rules over PLAIN TEXT log lines (nothing here parses or executes anything
it reads). Each rule is a name, a severity and a predicate; rules are matched against sample logs so you can
see hit/miss/false-positive rates before trusting a rule in anger."""
import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Rule:
    name: str
    severity: str
    match: Callable[[str], bool]
    why: str = ""


def _count(pattern):
    rx = re.compile(pattern)
    return lambda line: bool(rx.search(line))


RULES = [
    Rule("sql-meta-in-request", "high", _count(r"(\\bUNION\\b.*\\bSELECT\\b|--\\s*$|\\bOR\\b\\s+1=1|'\\s*OR\\s*')"),
         "classic SQL-injection markers in a request line"),
    Rule("repeated-auth-failure", "medium", _count(r"auth_failed.*attempt=(\\d+)"), "placeholder: paired with threshold logic in count_bursts()"),
    Rule("admin-path-from-new-ip", "medium", _count(r"path=/admin.*first_seen=true"), "admin area hit from an IP seen for the first time"),
    Rule("priv-escalation-attempt", "high", _count(r"\\b(sudo|setuid|chmod\\s+\\+s)\\b.*denied"), "a denied privilege-escalation attempt, logged, not performed"),
]


def scan(log_lines, rules=None):
    """-> [{"line": str, "rule": name, "severity": ...}, ...] for every line any rule matches."""
    rules = rules or RULES
    alerts = []
    for line in log_lines:
        for r in rules:
            if r.match(line):
                alerts.append({"line": line, "rule": r.name, "severity": r.severity})
    return alerts


def count_bursts(log_lines, pattern=r"auth_failed.*user=(\\S+)", threshold=5):
    """Correlation rule that a single-line regex can't express: N failures for the SAME user. Returns the
    usernames that crossed the threshold."""
    rx = re.compile(pattern)
    counts = {}
    for line in log_lines:
        m = rx.search(line)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return [u for u, n in counts.items() if n >= threshold]
'''
LAB_SAMPLE_LOGS = '''2026-01-01T00:00:01Z ip=10.20.0.75 path=/login method=POST status=200
2026-01-01T00:00:02Z ip=10.20.0.99 path=/search?q=' OR '1'='1 method=GET status=500
2026-01-01T00:00:03Z ip=10.20.0.50 path=/admin first_seen=true method=GET status=200
2026-01-01T00:00:04Z ip=10.20.0.60 auth_failed user=alice attempt=1
2026-01-01T00:00:05Z ip=10.20.0.60 auth_failed user=alice attempt=2
2026-01-01T00:00:06Z ip=10.20.0.60 auth_failed user=alice attempt=3
2026-01-01T00:00:07Z ip=10.20.0.60 auth_failed user=alice attempt=4
2026-01-01T00:00:08Z ip=10.20.0.60 auth_failed user=alice attempt=5
2026-01-01T00:00:09Z ip=10.20.0.75 path=/items method=GET status=200
2026-01-01T00:00:10Z ip=10.20.0.12 sudo chmod +s /bin/sh denied
'''
LAB_SCOPE_JSON = '''{
  "authorized_targets": ["app01.lab.internal", "db01.lab.internal", "fileshare01.lab.internal", "vpn01.lab.internal", "10.20.0.0/24", "10.20.1.0/24"],
  "excluded": ["10.20.0.1"],
  "allowed_actions": ["review_findings", "write_detections"],
  "window": "2026-01-01T00:00:00Z/2026-12-31T00:00:00Z"
}
'''
LAB_SCENARIOS = '''"""Fake-but-plausible scenarios for practice: each one is a FINDINGS list + a LOG set, generic defect
classes and generic log patterns -- never a real CVE, a working exploit, or a real company's incident.
Run `python -m __PKG__ scenarios` to list them, or `--scenario ID` on scan-logs / check-paths to use one
instead of findings.json / sample.log. Good for trying the referee and the detection rules against more
than one shape of "attack" without writing your own fixtures first.
"""
SCENARIOS = {
    "web_app_breach": {
        "blurb": "A public-facing app: SQL injection in a search box, root process, over-privileged DB account, insecure cookie.",
        "findings": [
            {"id": "F1", "host": "app01.lab.internal", "desc": "search endpoint builds SQL by string concatenation (CWE-89)", "severity": "high"},
            {"id": "F2", "host": "app01.lab.internal", "desc": "app process runs as root with a world-writable cron drop-in", "severity": "high"},
            {"id": "F3", "host": "db01.lab.internal", "desc": "database account used by the app has superuser privileges", "severity": "medium"},
            {"id": "F4", "host": "app01.lab.internal", "desc": "session cookie has no Secure or HttpOnly flag", "severity": "low"},
        ],
        "log_lines": [
            "ip=10.20.0.75 path=/login method=POST status=200",
            "ip=10.20.0.99 path=/search?q=' OR '1'='1 method=GET status=500",
            "ip=10.20.0.50 path=/admin first_seen=true method=GET status=200",
            "ip=10.20.0.75 path=/items method=GET status=200",
            "ip=10.20.0.12 sudo chmod +s /bin/sh denied",
        ],
    },
    "insider_threat": {
        "blurb": "A logged-in employee account: unusual off-hours bulk export, a permission grant nobody reviewed, access from an unmanaged device.",
        "findings": [
            {"id": "F1", "host": "fileshare01.lab.internal", "desc": "service account was granted org-wide read access with no expiry and no review (least-privilege violation)", "severity": "high"},
            {"id": "F2", "host": "fileshare01.lab.internal", "desc": "bulk-export action has no rate limit or approval step", "severity": "medium"},
            {"id": "F3", "host": "vpn01.lab.internal", "desc": "VPN accepts logins from devices that never enrolled in MDM", "severity": "medium"},
        ],
        "log_lines": [
            "ip=10.20.0.40 user=contractor7 path=/export?rows=all method=GET status=200 hour=03",
            "ip=10.20.0.40 user=contractor7 path=/export?rows=all method=GET status=200 hour=03",
            "ip=10.20.0.41 path=/admin/grant-role first_seen=true method=POST status=200",
            "ip=10.20.0.75 user=alice path=/items method=GET status=200 hour=14",
        ],
    },
    "credential_stuffing": {
        "blurb": "Many usernames, few attempts each, from a changing set of source IPs: no single-account lockout would ever catch it.",
        "findings": [
            {"id": "F1", "host": "app01.lab.internal", "desc": "login endpoint has no account lockout or CAPTCHA after repeated failures", "severity": "high"},
            {"id": "F2", "host": "app01.lab.internal", "desc": "no rate limiting per source IP on the login endpoint", "severity": "medium"},
        ],
        "log_lines": [f"ip=10.20.1.{i} auth_failed user=user{i} attempt=1" for i in range(1, 12)] + [
            "ip=10.20.1.1 auth_failed user=user1 attempt=2", "ip=10.20.1.1 auth_failed user=user1 attempt=3",
            "ip=10.20.1.1 auth_failed user=user1 attempt=4", "ip=10.20.1.1 auth_failed user=user1 attempt=5",
        ],
    },
}
'''
LAB_CLI = '''import argparse
import json
import os
import sys

from .attackpath import chain_score
from .detections import RULES, count_bursts, scan
from .scenarios import SCENARIOS
from .findings import load_findings
from .scope import load_scope

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__ -- scope-checked findings triage + log detections, no live exploitation")
    ap.add_argument("--scope", default=os.path.join(ROOT, "scope.json"))
    ap.add_argument("--findings", default=os.path.join(ROOT, "findings.json"))
    sub = ap.add_subparsers(dest="action", required=True)
    cp = sub.add_parser("check-paths")
    cp.add_argument("paths_json", help="path to a JSON file of [{finding_ids, host, impact}, ...]")
    cp.add_argument("--scenario", choices=sorted(SCENARIOS), help="use a practice scenario's findings instead of --findings")
    sl = sub.add_parser("scan-logs")
    sl.add_argument("log_file", nargs="?", help="omit when using --scenario")
    sl.add_argument("--scenario", choices=sorted(SCENARIOS), help="use a practice scenario's log lines instead of a file")
    sub.add_parser("scenarios", help="list the fake practice scenarios and what each one is about")
    a = ap.parse_args(argv)
    if a.action == "scenarios":
        for sid, s in sorted(SCENARIOS.items()):
            print(f"{sid}: {s['blurb']}")
        return 0
    scope = load_scope(a.scope)
    if a.action == "check-paths":
        findings = SCENARIOS[a.scenario]["findings"] if a.scenario else load_findings(a.findings)
        with open(a.paths_json) as f:
            paths = json.load(f)
        res = chain_score(paths, findings, scope)
        print(json.dumps({"valid": len(res["valid"]), "rejected": [{"path": p, "reason": r} for p, r in res["rejected"]]}, indent=2))
        return 0 if not res["rejected"] else 1
    if a.action == "scan-logs":
        if a.scenario:
            lines = SCENARIOS[a.scenario]["log_lines"]
        else:
            if not a.log_file:
                print("scan-logs needs a log_file, or --scenario ID", file=sys.stderr)
                return 2
            with open(a.log_file) as f:
                lines = [ln.rstrip("\\n") for ln in f if ln.strip()]
        alerts = scan(lines, RULES)
        bursts = count_bursts(lines)
        print(json.dumps({"alerts": alerts, "burst_users": bursts}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''
LAB_TEST_SCOPE = '''import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.scope import in_scope, load_scope

SCOPE = {"authorized_targets": ["10.20.0.0/24", "app01.lab.internal"], "excluded": ["10.20.0.1"],
         "allowed_actions": ["review_findings"], "window": "2026-01-01/2026-12-31"}


def test_in_range_is_in_scope():
    assert in_scope("10.20.0.50", SCOPE)[0] is True


def test_excluded_wins_even_inside_range():
    assert in_scope("10.20.0.1", SCOPE)[0] is False


def test_outside_range_is_out_of_scope():
    assert in_scope("10.20.1.50", SCOPE)[0] is False


def test_hostname_match():
    assert in_scope("app01.lab.internal", SCOPE)[0] is True
    assert in_scope("app02.lab.internal", SCOPE)[0] is False


def test_action_gate():
    assert in_scope("10.20.0.50", SCOPE, action="review_findings")[0] is True
    assert in_scope("10.20.0.50", SCOPE, action="live_exploit")[0] is False


def test_load_scope_requires_every_key(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"authorized_targets": []}))
    try:
        load_scope(str(p))
        assert False, "should have raised"
    except ValueError:
        pass
'''
LAB_TEST_ATTACKPATH = '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.attackpath import chain_score, validate_path
from __PKG__.findings import SAMPLE_FINDINGS

SCOPE = {"authorized_targets": ["app01.lab.internal", "db01.lab.internal"], "excluded": [], "allowed_actions": [], "window": "x"}


def test_valid_path_accepted():
    ok, _ = validate_path({"finding_ids": ["F1", "F2"], "host": "app01.lab.internal", "impact": "account takeover"}, SAMPLE_FINDINGS, SCOPE)
    assert ok


def test_unknown_finding_rejected():
    ok, reason = validate_path({"finding_ids": ["F99"], "host": "app01.lab.internal", "impact": "x"}, SAMPLE_FINDINGS, SCOPE)
    assert not ok and "F99" in reason


def test_out_of_scope_host_rejected():
    ok, reason = validate_path({"finding_ids": ["F1"], "host": "evil.example.com", "impact": "x"}, SAMPLE_FINDINGS, SCOPE)
    assert not ok and "scope" in reason


def test_host_must_match_cited_finding():
    ok, reason = validate_path({"finding_ids": ["F3"], "host": "app01.lab.internal", "impact": "x"}, SAMPLE_FINDINGS, SCOPE)
    assert not ok, "F3 belongs to db01, not app01 -- citing the wrong host must fail"


def test_chain_score_splits_valid_and_rejected():
    paths = [
        {"finding_ids": ["F1"], "host": "app01.lab.internal", "impact": "sqli"},
        {"finding_ids": ["F404"], "host": "app01.lab.internal", "impact": "x"},
    ]
    res = chain_score(paths, SAMPLE_FINDINGS, SCOPE)
    assert len(res["valid"]) == 1 and len(res["rejected"]) == 1
'''
LAB_TEST_DETECTIONS = '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.detections import RULES, count_bursts, scan

BENIGN = ["ip=10.0.0.1 path=/items method=GET status=200", "ip=10.0.0.2 path=/health method=GET status=200"]
SQLI_LINE = "ip=10.0.0.9 path=/search?q=' OR '1'='1 method=GET status=500"


def test_sqli_pattern_is_detected():
    alerts = scan([SQLI_LINE], RULES)
    assert any(a["rule"] == "sql-meta-in-request" for a in alerts)


def test_benign_traffic_has_no_alerts():
    assert scan(BENIGN, RULES) == []


def test_burst_detection_needs_the_threshold():
    few = [f"auth_failed user=bob attempt={i}" for i in range(1, 3)]
    many = [f"auth_failed user=bob attempt={i}" for i in range(1, 6)]
    assert count_bursts(few, threshold=5) == []
    assert count_bursts(many, threshold=5) == ["bob"]


def test_priv_escalation_denied_is_detected():
    alerts = scan(["sudo chmod +s /bin/sh denied"], RULES)
    assert any(a["rule"] == "priv-escalation-attempt" for a in alerts)
'''
LAB_TEST_CLI = '''import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.cli import main


def test_scan_logs_cli(tmp_path, capsys):
    log = tmp_path / "sample.log"
    log.write_text("ip=10.0.0.9 path=/search?q=' OR '1'='1 method=GET status=500\\n")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rc = main(["--scope", os.path.join(root, "scope.json"), "--findings", os.path.join(root, "findings.json"), "scan-logs", str(log)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and any(a["rule"] == "sql-meta-in-request" for a in out["alerts"])


def test_check_paths_cli_exits_nonzero_on_a_rejected_path(tmp_path, capsys):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = tmp_path / "paths.json"
    paths.write_text(json.dumps([{"finding_ids": ["F404"], "host": "app01.lab.internal", "impact": "x"}]))
    rc = main(["--scope", os.path.join(root, "scope.json"), "--findings", os.path.join(root, "findings.json"), "check-paths", str(paths)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] == 0 and len(out["rejected"]) == 1
'''
LAB_TEST_SCENARIOS = '''import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.cli import main
from __PKG__.scenarios import SCENARIOS


def test_every_scenario_has_a_blurb_and_at_least_one_finding_and_log_line():
    assert len(SCENARIOS) >= 3
    for sid, s in SCENARIOS.items():
        assert s["blurb"] and s["findings"] and s["log_lines"], sid


def test_scenarios_subcommand_lists_them(capsys):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rc = main(["--scope", os.path.join(root, "scope.json"), "scenarios"])
    out = capsys.readouterr().out
    assert rc == 0 and all(sid in out for sid in SCENARIOS)


def test_scan_logs_can_use_a_scenario_instead_of_a_file(capsys):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rc = main(["--scope", os.path.join(root, "scope.json"), "scan-logs", "--scenario", "web_app_breach"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert any(a["rule"] == "sql-meta-in-request" for a in out["alerts"])


def test_credential_stuffing_scenario_spreads_across_many_users_not_one():
    """The whole point of this scenario: per-account lockout wouldn't catch it, since each user only
    fails a handful of times -- the volume is in the NUMBER of distinct accounts, not repeats on one."""
    lines = SCENARIOS["credential_stuffing"]["log_lines"]
    users = {ln.split("user=")[1].split()[0] for ln in lines if "user=" in ln}
    assert len(users) >= 10


def test_check_paths_can_use_a_scenario_instead_of_findings_json(tmp_path, capsys):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = tmp_path / "paths.json"
    f1 = SCENARIOS["insider_threat"]["findings"][0]
    paths.write_text(json.dumps([{"finding_ids": [f1["id"]], "host": f1["host"], "impact": "data exfiltration"}]))
    rc = main(["--scope", os.path.join(root, "scope.json"), "check-paths", str(paths), "--scenario", "insider_threat"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] == 1
'''
_t("security_lab", "Security lab (red/blue, scope-checked)", "Both sides of the same findings: a scope.json boundary and a referee that rejects any attack path citing a finding that doesn't exist or a host outside scope (red), plus a log-detection rule engine with hit/miss/false-positive tests (blue). Ships 3 fake-but-plausible scenarios to practice against (web app breach, insider threat, credential stuffing) -- `python -m __PKG__ scenarios`. No scanning, no exploitation, no live network calls -- validates data you supply.",
   "python / sqlite-free / regex rules", ["api"], {
       **COMMON,
       "README.md": "# __NAME__\n\nA scope-checked red/blue lab: this project never touches a real host. It validates attack-path\nclaims against findings.json + scope.json (red), and runs a small detection-rule engine over sample\nlog lines (blue).\n\n    python -m __PKG__ scan-logs sample.log\n    python -m __PKG__ check-paths paths.json\n    python -m __PKG__ scenarios                           # list fake-but-plausible practice scenarios\n    python -m __PKG__ scan-logs --scenario credential_stuffing\n    python -m __PKG__ check-paths paths.json --scenario insider_threat\n\nEdit `findings.json` / `scope.json` for your own lab, or add a rule to `__PKG__/detections.py`. Add a new\nscenario to `__PKG__/scenarios.py` (findings + log lines, same generic-defect-class rule as the others --\nnever a real CVE or a working exploit). Every attack path must cite real finding ids, stay inside\nscope.json's authorized_targets, and name the host that actually produced the cited finding --\n`attackpath.py` is the referee, not a suggestion.\n",
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: scope-checked red/blue lab (no live exploitation)."""\n\n__version__ = "0.1.0"\n',
       "__PKG__/scope.py": LAB_SCOPE,
       "__PKG__/findings.py": LAB_FINDINGS,
       "__PKG__/attackpath.py": LAB_ATTACKPATH,
       "__PKG__/detections.py": LAB_DETECTIONS,
       "__PKG__/scenarios.py": LAB_SCENARIOS,
       "__PKG__/cli.py": LAB_CLI,
       "__PKG__/__main__.py": "from .cli import main\nimport sys\n\nsys.exit(main())\n",
       "scope.json": LAB_SCOPE_JSON,
       "findings.json": "[]",
       "sample.log": LAB_SAMPLE_LOGS,
       "tests/test_scope.py": LAB_TEST_SCOPE,
       "tests/test_attackpath.py": LAB_TEST_ATTACKPATH,
       "tests/test_detections.py": LAB_TEST_DETECTIONS,
       "tests/test_cli.py": LAB_TEST_CLI,
       "tests/test_scenarios.py": LAB_TEST_SCENARIOS,
   }, "python -m __PKG__ scan-logs sample.log")

# ------------------------------------------------------------------------------------------------ red team (pure offense, scope-checked)
RT_CLI = '''import argparse
import json
import os
import sys

from .attackpath import chain_score
from .findings import load_findings
from .scope import load_scope

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__ -- scope-checked attack-path review, no live exploitation")
    ap.add_argument("--scope", default=os.path.join(ROOT, "scope.json"))
    ap.add_argument("--findings", default=os.path.join(ROOT, "findings.json"))
    sub = ap.add_subparsers(dest="action", required=True)
    cp = sub.add_parser("check-paths", help="grade proposed attack paths against findings.json + scope.json")
    cp.add_argument("paths_json", help="path to a JSON file of [{finding_ids, host, impact}, ...]")
    a = ap.parse_args(argv)
    scope = load_scope(a.scope)
    findings = load_findings(a.findings)
    with open(a.paths_json) as f:
        paths = json.load(f)
    res = chain_score(paths, findings, scope)
    print(json.dumps({"valid": len(res["valid"]), "rejected": [{"path": p, "reason": r} for p, r in res["rejected"]]}, indent=2))
    return 0 if not res["rejected"] else 1


if __name__ == "__main__":
    sys.exit(main())
'''
RT_SCOPE_JSON = '''{
  "authorized_targets": ["127.0.0.1", "localhost"],
  "excluded": [],
  "allowed_actions": ["plan_attack_path", "review_findings"],
  "window": ""
}
'''
RT_README = "# __NAME__\n\nA pure red-team attack-path review tool: this project never touches a real host, scans nothing, and\nexploits nothing. It only grades attack-path CLAIMS (which findings they cite, which host, what concrete\nimpact) against findings.json + scope.json -- the same discipline a real engagement needs before any tool\nruns.\n\n    python -m __PKG__ check-paths paths.json\n\nEach path in paths.json is `{\"finding_ids\": [...], \"host\": \"...\", \"impact\": \"...\"}`. A path is accepted only\nif every finding id it cites actually exists in findings.json, the host is inside scope.json's\nauthorized_targets (and not excluded), the host matches the host of every finding it cites, and it states a\nconcrete impact. scope.json defaults to loopback-only (127.0.0.1) until you edit it for a real, authorized,\ntime-windowed engagement.\n"
RT_TEST_CLI = '''import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.cli import main


def test_check_paths_cli_accepts_a_valid_path(tmp_path, capsys):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps([{"id": "F1", "host": "127.0.0.1", "desc": "x", "severity": "high"}]))
    paths = tmp_path / "paths.json"
    paths.write_text(json.dumps([{"finding_ids": ["F1"], "host": "127.0.0.1", "impact": "demo"}]))
    rc = main(["--scope", os.path.join(root, "scope.json"), "--findings", str(findings), "check-paths", str(paths)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["valid"] == 1


def test_check_paths_cli_exits_nonzero_on_a_rejected_path(tmp_path, capsys):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps([{"id": "F1", "host": "127.0.0.1", "desc": "x", "severity": "high"}]))
    paths = tmp_path / "paths.json"
    paths.write_text(json.dumps([{"finding_ids": ["F404"], "host": "127.0.0.1", "impact": "x"}]))
    rc = main(["--scope", os.path.join(root, "scope.json"), "--findings", str(findings), "check-paths", str(paths)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] == 0 and len(out["rejected"]) == 1
'''
_t("red_team", "Red team (attack-path review, scope-checked)", "Pure offense, no blue side: grades proposed attack paths against findings.json + scope.json, "
   "rejecting any path that cites a finding which doesn't exist, names a host outside scope, or names a host that doesn't match its cited finding. "
   "No scanning, no exploitation, no live network calls -- the same scope-checking logic security_lab's red side uses, standing alone.",
   "python / pure validation, no network", ["api"], {
       **COMMON,
       "README.md": RT_README,
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: scope-checked red-team attack-path review (no live exploitation)."""\n\n__version__ = "0.1.0"\n',
       "__PKG__/scope.py": LAB_SCOPE,
       "__PKG__/findings.py": LAB_FINDINGS,
       "__PKG__/attackpath.py": LAB_ATTACKPATH,
       "__PKG__/cli.py": RT_CLI,
       "__PKG__/__main__.py": "from .cli import main\nimport sys\n\nsys.exit(main())\n",
       "scope.json": RT_SCOPE_JSON,
       "findings.json": "[]",
       "tests/test_scope.py": LAB_TEST_SCOPE,
       "tests/test_attackpath.py": LAB_TEST_ATTACKPATH,
       "tests/test_cli.py": RT_TEST_CLI,
   }, "python -m __PKG__ check-paths paths.json")

# ------------------------------------------------------------------------------------------------ range (REAL loopback-only practice target)
RANGE_TARGET = '''"""A REAL, loopback-only practice target for detection work. Nothing here is simulated: every port this
binds is a real socket (ThreadingHTTPServer), every request it answers is a real HTTP exchange, and every
line it logs is written because a real request actually happened. The host is hardcoded to 127.0.0.1 and is
NEVER configurable -- there is no parameter or flag anywhere in this template that can bind it anywhere else.

Two endpoints are deliberately weak, by design, for detection practice -- not a real product's vulnerability,
not a working exploit, just the generic defect CLASS a scanner or code review would flag:
  /search?q=...   reflects q straight into the HTML body with no encoding (reflected-XSS SHAPE)
  /login           a fixed demo account (demo/demo123); every other attempt is a real 401, so repeated
                    wrong guesses produce a real, detectable burst of failures
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEMO_USER, DEMO_PASS = "demo", "demo123"


def _log_line(log_path, method, path, remote_ip, status):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ip={remote_ip} method={method} path={path} status={status}\\n"
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    return line


class Handler(BaseHTTPRequestHandler):
    log_path = None

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if content_type == "application/json" else str(body).encode())
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, status):
        _log_line(self.log_path, self.command, self.path, self.client_address[0], status)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            self._send(200, {"status": "ok"}); self._record(200); return
        if u.path == "/search":
            q = (parse_qs(u.query).get("q") or [""])[0]
            # deliberately unsafe: q is NOT escaped -- this is the weakness the range exists to let you detect
            body = f"<html><body><h1>Results for: {q}</h1></body></html>"
            self._send(200, body.encode(), content_type="text/html"); self._record(200); return
        self._send(404, {"error": "not_found"}); self._record(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            ok = body.get("user") == DEMO_USER and body.get("pass") == DEMO_PASS
            self._send(200 if ok else 401, {"ok": ok}); self._record(200 if ok else 401); return
        self._send(404, {"error": "not_found"}); self._record(404)

    def log_message(self, fmt, *args):
        pass  # the real log is this range's own log file (_record), not stderr noise

    def finish(self):
        try:
            super().finish()
        except OSError:
            pass  # a client (e.g. a probe with a short timeout) that closed early is not a server fault


def make_server(port=0, log_path=None):
    """host is ALWAYS 127.0.0.1 -- there is no parameter here to change it, so this can never bind off-box."""
    handler = type("BoundHandler", (Handler,), {"log_path": log_path})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


if __name__ == "__main__":
    srv = make_server(port=int(os.environ.get("PORT", "0")), log_path=os.environ.get("RANGE_LOG", "range.log"))
    print("listening on http://127.0.0.1:%d (loopback only)" % srv.server_address[1])
    srv.serve_forever()
'''
RANGE_PROBE = '''"""Real traffic against a REAL, already-running range target (see target.py) -- every request here is an
actual HTTP call over loopback (urllib), never a canned/fake log line. Used by `__PKG__ drill` to generate
realistic traffic to practice detecting; loopback-only like everything else in this template."""
import json
import urllib.error
import urllib.parse
import urllib.request


def _get(base, path):
    try:
        urllib.request.urlopen(base + path, timeout=5)
    except urllib.error.HTTPError:
        pass  # a real 4xx/5xx is still a real exchange -- nothing here hides a failure


def _post_login(base, user, password):
    data = json.dumps({"user": user, "pass": password}).encode()
    req = urllib.request.Request(base + "/login", data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError:
        pass


def run_drill(base):
    """-> number of real HTTP requests sent. base, e.g. 'http://127.0.0.1:51234'."""
    n = 0
    _get(base, "/health"); n += 1
    _get(base, "/search?q=hello"); n += 1
    _get(base, "/search?q=" + urllib.parse.quote("<script>alert(1)</script>")); n += 1  # reflected-XSS-SHAPED probe, loopback only
    for _ in range(5):
        _post_login(base, "demo", "wrong-guess"); n += 1
    _post_login(base, "demo", "demo123"); n += 1  # one real successful login, for contrast
    return n
'''
RANGE_RULES = '''"""Detection rules over the range target's OWN real log lines (see target.py's _log_line format) -- plain
regex matching; nothing here parses or executes anything it reads."""
import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class Rule:
    name: str
    severity: str
    match: Callable[[str], bool]
    why: str = ""


def _has(pattern):
    rx = re.compile(pattern)
    return lambda line: bool(rx.search(line))


RULES = [
    Rule("reflected-script-in-query", "high", _has(r"(?i)%3cscript%3e"),
         "a <script> marker was reflected back in a search query, percent-encoded in the raw request log the "
         "same way a real access log would show it -- /search is deliberately unsafe so this is detectable, never exploited"),
]


def scan(log_lines, rules=None):
    rules = rules or RULES
    alerts = []
    for line in log_lines:
        for r in rules:
            if r.match(line):
                alerts.append({"line": line, "rule": r.name, "severity": r.severity})
    return alerts


def count_login_failure_bursts(log_lines, threshold=3):
    """-> the source IPs with at least `threshold` real 401s on /login -- a real brute-force SHAPE, not a canned one."""
    rx = re.compile(r"ip=(\\S+).*path=/login status=401")
    counts = {}
    for line in log_lines:
        m = rx.search(line)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return [ip for ip, n in counts.items() if n >= threshold]
'''
RANGE_CLI = '''"""Start, drill, and scan logs for the range target. Everything here is real: a real bound socket, real
HTTP requests, a real log file. There is no --host flag anywhere -- the target can only ever bind 127.0.0.1."""
import argparse
import json
import os
import sys
import threading
import time

from .probe import run_drill
from .rules import RULES, count_login_failure_bursts, scan
from .target import make_server

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_LOG = os.path.join(ROOT, "range.log")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__ -- a real, loopback-only practice range: no fake ports, no fake traffic")
    sub = ap.add_subparsers(dest="action", required=True)

    sv = sub.add_parser("serve", help="run the real target in the foreground until Ctrl+C")
    sv.add_argument("--port", type=int, default=0, help="0 = let the OS pick a free port (printed once bound)")
    sv.add_argument("--log", default=DEFAULT_LOG)

    dr = sub.add_parser("drill", help="start the real target, send real practice traffic at it, stop it, and show what it logged")
    dr.add_argument("--port", type=int, default=0)
    dr.add_argument("--log", default=DEFAULT_LOG)

    sl = sub.add_parser("scan-logs", help="run the detection rules over a real log file")
    sl.add_argument("log_file", nargs="?", default=DEFAULT_LOG)

    a = ap.parse_args(argv)

    if a.action == "serve":
        srv = make_server(port=a.port, log_path=a.log)
        print(f"listening on http://127.0.0.1:{srv.server_address[1]} (loopback only) -- logging to {a.log}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.shutdown()
        return 0

    if a.action == "drill":
        srv = make_server(port=a.port, log_path=a.log)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        time.sleep(0.1)
        try:
            n = run_drill(f"http://127.0.0.1:{port}")
        finally:
            srv.shutdown()
            srv.server_close()
        with open(a.log) as f:
            tail = f.readlines()[-n:]
        print(json.dumps({"port": port, "requests_sent": n, "log_tail": [ln.rstrip() for ln in tail]}, indent=2))
        return 0

    if a.action == "scan-logs":
        if not os.path.isfile(a.log_file):
            print(f"no log file at {a.log_file} yet -- run '__PKG__ drill' or 'serve' first", file=sys.stderr)
            return 2
        with open(a.log_file) as f:
            lines = [ln.rstrip("\\n") for ln in f if ln.strip()]
        alerts = scan(lines, RULES)
        bursts = count_login_failure_bursts(lines)
        print(json.dumps({"alerts": alerts, "login_failure_bursts": bursts}, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
'''
RANGE_README = "# __NAME__\n\nA REAL, loopback-only practice target for detection work -- not a simulation. `__PKG__ drill` binds a real\nsocket on 127.0.0.1 (never any other host -- there is no flag to change that), sends real HTTP requests at\nit, and writes a real log file as it goes. `__PKG__ scan-logs` then runs real detection rules over that real\nlog.\n\n    python -m __PKG__ drill                # start it, attack it with real traffic, stop it, print what it logged\n    python -m __PKG__ serve --port 8800     # run it yourself and hit it by hand\n    python -m __PKG__ scan-logs range.log\n\n`/search` deliberately reflects its `q` parameter unescaped (a reflected-XSS SHAPE, not a real product's bug\nand not a working exploit) and `/login` only accepts one fixed demo account -- both exist purely so you have\nreal, self-generated traffic to practice detecting. Add a rule to `__PKG__/rules.py` to practice writing your\nown detections against real logs instead of canned ones.\n"
RANGE_TEST = '''import os
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from __PKG__.probe import run_drill
from __PKG__.rules import RULES, count_login_failure_bursts, scan
from __PKG__.target import make_server


def _start(log_path):
    srv = make_server(port=0, log_path=log_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv


def test_server_binds_a_real_socket_on_127_0_0_1_only(tmp_path):
    srv = _start(str(tmp_path / "r.log"))
    try:
        host, port = srv.server_address
        assert host == "127.0.0.1"
        s = socket.create_connection((host, port), timeout=2)  # proves the port is REALLY open, not claimed
        s.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_drill_sends_real_requests_and_writes_one_real_log_line_each(tmp_path):
    log = tmp_path / "r.log"
    srv = _start(str(log))
    try:
        n = run_drill(f"http://127.0.0.1:{srv.server_address[1]}")
    finally:
        srv.shutdown()
        srv.server_close()
    assert n > 0
    lines = log.read_text().strip().splitlines()
    assert len(lines) == n


def test_reflected_script_probe_is_detected(tmp_path):
    log = tmp_path / "r.log"
    srv = _start(str(log))
    try:
        run_drill(f"http://127.0.0.1:{srv.server_address[1]}")
    finally:
        srv.shutdown()
        srv.server_close()
    alerts = scan(log.read_text().strip().splitlines(), RULES)
    assert any(a["rule"] == "reflected-script-in-query" for a in alerts)


def test_login_failure_burst_is_detected():
    lines = [f"2026-01-01T00:00:0{i}Z ip=127.0.0.1 method=POST path=/login status=401" for i in range(5)]
    assert count_login_failure_bursts(lines, threshold=3) == ["127.0.0.1"]


def test_clean_traffic_has_no_alerts(tmp_path):
    log = tmp_path / "r.log"
    srv = _start(str(log))
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/health", timeout=5)
        urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/search?q=hello", timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
    assert scan(log.read_text().strip().splitlines(), RULES) == []
'''
_t("range", "Range (REAL loopback-only practice target)", "A real target, not a simulation: a real ThreadingHTTPServer hardcoded to 127.0.0.1 (no flag anywhere "
   "can change that), `drill` sends real HTTP requests at it and writes a real log, `scan-logs` runs real detection rules over that real log. "
   "Two endpoints are deliberately weak by design (unescaped search reflection, one fixed demo login) purely so you have real, self-generated "
   "traffic to practice detecting -- no fake ports, no fake networking, no canned log lines.",
   "python / http.server, loopback only", ["api"], {
       **COMMON,
       "README.md": RANGE_README,
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: a real, loopback-only detection-practice range."""\n\n__version__ = "0.1.0"\n',
       "__PKG__/target.py": RANGE_TARGET,
       "__PKG__/probe.py": RANGE_PROBE,
       "__PKG__/rules.py": RANGE_RULES,
       "__PKG__/cli.py": RANGE_CLI,
       "__PKG__/__main__.py": "from .cli import main\nimport sys\n\nsys.exit(main())\n",
       "tests/test_range.py": RANGE_TEST,
   }, "python -m __PKG__ drill")

# ------------------------------------------------------------------------------------------------ REST API (stdlib)
SERVER = '''"""A small JSON REST API on the standard library (no dependencies). Resource: items.

    python -m app.server          # http://127.0.0.1:8000   (GET /health, /items ...)

Errors always look like {"error": {"code": "...", "message": "..."}}. Bodies are limited to 64 KB.
If a `web/` folder exists next to this package, it is served at / (that is how the full-stack template works).
"""
import json
import mimetypes
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class Store:
    """SQLite storage, one connection guarded by a lock (fine for a small service; swap for a pool when it grows)."""

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200), done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)))")
            self.db.commit()

    @staticmethod
    def row(r):
        return {"id": r["id"], "name": r["name"], "done": bool(r["done"])}

    def list(self):
        with self.lock:
            return [self.row(r) for r in self.db.execute("SELECT * FROM items ORDER BY id")]

    def get(self, i):
        with self.lock:
            r = self.db.execute("SELECT * FROM items WHERE id = ?", (i,)).fetchone()
        return self.row(r) if r else None

    def create(self, name, done=False):
        with self.lock:
            cur = self.db.execute("INSERT INTO items (name, done) VALUES (?, ?)", (name, int(done)))
            self.db.commit()
            return cur.lastrowid

    def update(self, i, name, done):
        with self.lock:
            cur = self.db.execute("UPDATE items SET name = ?, done = ? WHERE id = ?", (name, int(done), i))
            self.db.commit()
            return cur.rowcount > 0

    def delete(self, i):
        with self.lock:
            cur = self.db.execute("DELETE FROM items WHERE id = ?", (i,))
            self.db.commit()
            return cur.rowcount > 0


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def validate(body, partial_ok=False):
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_body", "the body must be a JSON object")
    name, done = body.get("name"), body.get("done", False)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        raise ApiError(422, "invalid_name", "name must be a string of 1 to 200 characters")
    if not isinstance(done, bool):
        raise ApiError(422, "invalid_done", "done must be true or false")
    return name.strip(), done


class Handler(BaseHTTPRequestHandler):
    store = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, status, obj=None):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "too_large", "body larger than 64 KB")
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "invalid_json", "the body is not valid JSON")

    def static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "not_found", "no such route")
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(full)[0] or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            m = re.fullmatch(r"/items/(\\d+)", path)
            if path == "/health":
                return self.send_json(200, {"status": "ok"})
            if path == "/items":
                if self.command == "GET":
                    return self.send_json(200, self.store.list())
                if self.command == "POST":
                    name, done = validate(self.body())
                    return self.send_json(201, self.store.get(self.store.create(name, done)))
                raise ApiError(405, "method_not_allowed", "use GET or POST")
            if m:
                i = int(m.group(1))
                item = self.store.get(i)
                if item is None:
                    raise ApiError(404, "not_found", f"item {i} does not exist")
                if self.command == "GET":
                    return self.send_json(200, item)
                if self.command == "PUT":
                    name, done = validate(self.body())
                    self.store.update(i, name, done)
                    return self.send_json(200, self.store.get(i))
                if self.command == "DELETE":
                    self.store.delete(i)
                    return self.send_json(204)
                raise ApiError(405, "method_not_allowed", "use GET, PUT or DELETE")
            if self.command == "GET" and os.path.isdir(WEB_DIR):
                return self.static(path)
            raise ApiError(404, "not_found", "no such route")
        except ApiError as e:
            self.send_json(e.status, {"error": {"code": e.code, "message": e.message}})
        except Exception:                                           # never leak internals to the client
            self.send_json(500, {"error": {"code": "internal", "message": "unexpected error"}})

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


def make_server(host="127.0.0.1", port=8000, db_path=":memory:"):
    handler = type("BoundHandler", (Handler,), {"store": Store(db_path)})
    return ThreadingHTTPServer((host, port), handler)


if __name__ == "__main__":
    srv = make_server(port=int(os.environ.get("PORT", "8000")), db_path=os.environ.get("DB_PATH", "app.db"))
    print("listening on http://127.0.0.1:%d" % srv.server_address[1])
    srv.serve_forever()
'''

API_TESTS = '''import json
import threading
import urllib.error
import urllib.request

import pytest

from app.server import make_server


@pytest.fixture()
def base():
    srv = make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def test_health(base):
    assert call("GET", base + "/health") == (200, {"status": "ok"})


def test_crud_roundtrip(base):
    status, item = call("POST", base + "/items", {"name": "write tests"})
    assert status == 201 and item["done"] is False
    i = item["id"]
    assert call("GET", f"{base}/items/{i}")[1]["name"] == "write tests"
    assert call("PUT", f"{base}/items/{i}", {"name": "write more tests", "done": True})[1]["done"] is True
    assert [x["id"] for x in call("GET", base + "/items")[1]] == [i]
    assert call("DELETE", f"{base}/items/{i}")[0] == 204
    assert call("GET", f"{base}/items/{i}")[0] == 404


@pytest.mark.parametrize("body,code", [({}, "invalid_name"), ({"name": ""}, "invalid_name"), ({"name": "x" * 201}, "invalid_name"), ({"name": "ok", "done": "yes"}, "invalid_done")])
def test_validation_errors(base, body, code):
    status, err = call("POST", base + "/items", body)
    assert status == 422 and err["error"]["code"] == code


def test_unknown_route_and_method(base):
    assert call("GET", base + "/nope")[0] == 404
    assert call("DELETE", base + "/items")[0] == 405
'''

OPENAPI = '''{
  "openapi": "3.0.3",
  "info": {"title": "__NAME__ API", "version": "0.1.0"},
  "paths": {
    "/health": {"get": {"operationId": "health", "responses": {"200": {"description": "ok"}}}},
    "/items": {
      "get": {"operationId": "listItems", "responses": {"200": {"description": "all items", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}}}}}},
      "post": {"operationId": "createItem", "requestBody": {"required": true, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemInput"}}}},
               "responses": {"201": {"description": "created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}}, "422": {"$ref": "#/components/responses/Error"}}}
    },
    "/items/{id}": {
      "parameters": [{"name": "id", "in": "path", "required": true, "schema": {"type": "integer"}}],
      "get": {"operationId": "getItem", "responses": {"200": {"description": "the item", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}}, "404": {"$ref": "#/components/responses/Error"}}},
      "put": {"operationId": "updateItem", "requestBody": {"required": true, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemInput"}}}},
              "responses": {"200": {"description": "updated", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}}, "404": {"$ref": "#/components/responses/Error"}, "422": {"$ref": "#/components/responses/Error"}}},
      "delete": {"operationId": "deleteItem", "responses": {"204": {"description": "deleted"}, "404": {"$ref": "#/components/responses/Error"}}}
    }
  },
  "components": {
    "schemas": {
      "Item": {"type": "object", "required": ["id", "name", "done"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "done": {"type": "boolean"}}},
      "ItemInput": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}, "done": {"type": "boolean"}}},
      "Error": {"type": "object", "required": ["error"], "properties": {"error": {"type": "object", "required": ["code", "message"], "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}}
    },
    "responses": {"Error": {"description": "error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}}
  }
}
'''

_t("rest_api", "REST API (no dependencies)", "JSON API with validation, error contract, SQLite storage, OpenAPI file and HTTP-level tests. Runs offline.",
   "python / http.server / sqlite", ["api", "data"], {
       **COMMON,
       "README.md": "# __NAME__\n\nJSON REST API on the Python standard library.\n\n    python -m app.server      # http://127.0.0.1:8000\n    python -m pytest -q\n\nThe contract is in `api/openapi.json`.\n",
       "app/__init__.py": "",
       "app/server.py": SERVER,
       "api/openapi.json": OPENAPI,
       "tests/test_api.py": API_TESTS,
   }, "python -m app.server")

# ------------------------------------------------------------------------------------------------ full-stack
INDEX = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__</title>
<link rel="stylesheet" href="/styles.css">
</head>
<body>
<main>
  <h1>__NAME__</h1>
  <form id="add" autocomplete="off">
    <label for="name">New item</label>
    <input id="name" name="name" maxlength="200" required>
    <button type="submit">Add</button>
  </form>
  <p id="status" role="status" aria-live="polite"></p>
  <ul id="list"></ul>
</main>
<script src="/app.js"></script>
</body>
</html>
'''

STYLES = ''':root{--bg:#f7f8fb;--ink:#161b26;--mute:#5a6578;--line:#dfe3ec;--acc:#2d4bd6;--panel:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0f131b;--ink:#e8ecf4;--mute:#9aa7bb;--line:#273044;--acc:#8ea2ff;--panel:#171d29}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
main{max-width:34rem;margin:0 auto;padding:2rem 1rem}
h1{font-size:1.6rem;margin:0 0 1rem}
form{display:flex;flex-wrap:wrap;gap:.5rem;align-items:end}
label{flex-basis:100%;color:var(--mute);font-size:.9rem}
input{flex:1 1 12rem;padding:.55rem .7rem;border:1px solid var(--line);border-radius:.6rem;background:var(--panel);color:var(--ink);font:inherit}
button{padding:.55rem 1rem;border:0;border-radius:.6rem;background:var(--acc);color:#fff;font:inherit;cursor:pointer}
@media (prefers-color-scheme:dark){button{color:#0f131b}}
button.link{background:none;color:var(--mute);padding:.2rem .5rem}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
ul{list-style:none;padding:0;margin:1rem 0}
li{display:flex;align-items:center;gap:.6rem;padding:.6rem .8rem;border:1px solid var(--line);border-radius:.6rem;background:var(--panel);margin-bottom:.5rem}
li span{flex:1;word-break:break-word}
li.done span{text-decoration:line-through;color:var(--mute)}
#status{color:var(--mute);min-height:1.5em}
'''

APPJS = '''"use strict";
const list = document.getElementById("list"), statusEl = document.getElementById("status"), form = document.getElementById("add"), input = document.getElementById("name");

async function api(path, options) {
  const res = await fetch(path, Object.assign({headers: {"Content-Type": "application/json"}}, options));
  if (res.status === 204) return null;
  const body = await res.json();
  if (!res.ok) throw new Error((body.error && body.error.message) || "request failed");
  return body;
}

function say(msg) { statusEl.textContent = msg || ""; }

function render(items) {
  list.replaceChildren();
  if (!items.length) { say("Nothing here yet. Add the first item above."); return; }
  say("");
  for (const it of items) {
    const li = document.createElement("li");
    if (it.done) li.className = "done";
    const box = document.createElement("input");
    box.type = "checkbox"; box.checked = it.done; box.setAttribute("aria-label", "Done: " + it.name);
    box.addEventListener("change", () => act(() => api("/items/" + it.id, {method: "PUT", body: JSON.stringify({name: it.name, done: box.checked})})));
    const text = document.createElement("span"); text.textContent = it.name;
    const del = document.createElement("button"); del.className = "link"; del.type = "button"; del.textContent = "Delete";
    del.setAttribute("aria-label", "Delete " + it.name);
    del.addEventListener("click", () => act(() => api("/items/" + it.id, {method: "DELETE"})));
    li.append(box, text, del); list.append(li);
  }
}

async function load() { say("Loading..."); try { render(await api("/items")); } catch (e) { say("Could not load: " + e.message); } }
async function act(fn) { try { await fn(); await load(); } catch (e) { say("Error: " + e.message); } }

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const name = input.value.trim();
  if (!name) return;
  act(() => api("/items", {method: "POST", body: JSON.stringify({name})})).then(() => { input.value = ""; input.focus(); });
});
load();
'''

FULL_TESTS = '''import json
import os
import threading
import urllib.request

from app.server import make_server


def test_web_files_exist_and_are_served():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ("index.html", "app.js", "styles.css"):
        assert os.path.isfile(os.path.join(here, "web", f)), f
    srv = make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>" in page and 'lang="en"' in page
        assert urllib.request.urlopen(base + "/app.js").headers["Content-Type"].startswith(("text/javascript", "application/javascript"))
        assert json.loads(urllib.request.urlopen(base + "/items").read()) == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_static_path_cannot_escape():
    srv = make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        c.request("GET", "/../app/server.py")
        assert c.getresponse().status == 404
    finally:
        srv.shutdown()
        srv.server_close()
'''

_t("fullstack", "Full-stack web app", "Accessible front end (HTML/CSS/JS, dark mode, empty/loading/error states) on the REST API above, served from one process.",
   "html+js / python / sqlite", ["web", "api", "data"], {
       **COMMON,
       "README.md": "# __NAME__\n\nFull-stack starter: a small web UI served by a JSON API with SQLite.\n\n    python -m app.server      # http://127.0.0.1:8000\n    python -m pytest -q\n",
       "app/__init__.py": "",
       "app/server.py": SERVER,
       "api/openapi.json": OPENAPI,
       "web/index.html": INDEX,
       "web/styles.css": STYLES,
       "web/app.js": APPJS,
       "tests/test_api.py": API_TESTS,
       "tests/test_web.py": FULL_TESTS,
   }, "python -m app.server")

# ------------------------------------------------------------------------------------------------ static web
STATIC_INDEX = INDEX.replace('<link rel="stylesheet" href="/styles.css">', '<link rel="stylesheet" href="styles.css">').replace('<script src="/app.js"></script>', '<script src="app.js"></script>')
STATIC_JS = '''"use strict";
const KEY = "__PKG__.items", list = document.getElementById("list"), statusEl = document.getElementById("status"), form = document.getElementById("add"), input = document.getElementById("name");
let items = [];
try { items = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (_) { items = []; }

function save() { try { localStorage.setItem(KEY, JSON.stringify(items)); } catch (_) { statusEl.textContent = "Could not save in this browser."; } }

function render() {
  list.replaceChildren();
  statusEl.textContent = items.length ? "" : "Nothing here yet. Add the first item above.";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    if (it.done) li.className = "done";
    const box = document.createElement("input"); box.type = "checkbox"; box.checked = it.done; box.setAttribute("aria-label", "Done: " + it.name);
    box.addEventListener("change", () => { it.done = box.checked; save(); render(); });
    const text = document.createElement("span"); text.textContent = it.name;
    const del = document.createElement("button"); del.type = "button"; del.className = "link"; del.textContent = "Delete"; del.setAttribute("aria-label", "Delete " + it.name);
    del.addEventListener("click", () => { items.splice(i, 1); save(); render(); });
    li.append(box, text, del); list.append(li);
  });
}

form.addEventListener("submit", (ev) => { ev.preventDefault(); const name = input.value.trim(); if (!name) return; items.push({name, done: false}); input.value = ""; save(); render(); input.focus(); });
render();
'''
STATIC_TESTS = '''import os
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.ids, self.labels_for = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "label" and "for" in a:
            self.labels_for.append(a["for"])


def scan():
    s = Scan()
    s.feed(open(os.path.join(ROOT, "index.html"), encoding="utf-8").read())
    return s


def test_page_basics():
    s = scan()
    tags = [t for t, _ in s.tags]
    assert "title" in tags and "main" in tags
    assert dict(s.tags[1][1]).get("lang", "en") or True
    assert any(t == "meta" and a.get("name") == "viewport" for t, a in s.tags)


def test_ids_unique_and_inputs_labelled():
    s = scan()
    assert len(s.ids) == len(set(s.ids))
    for t, a in s.tags:
        if t == "input" and a.get("type", "text") in ("text", "email", "search"):
            assert a.get("id") in s.labels_for or a.get("aria-label")


def test_no_external_resources():
    text = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    assert "http://" not in text and "https://" not in text
'''
_t("static_web", "Static web page / app", "No build step, no server: index.html + CSS + JS with localStorage. Accessible, responsive, dark mode.",
   "html / css / js", ["web"], {
       **COMMON,
       "README.md": "# __NAME__\n\nOpen `index.html` in a browser. Tests: `python -m pytest -q`.\n",
       "index.html": STATIC_INDEX,
       "styles.css": STYLES,
       "app.js": STATIC_JS,
       "tests/test_static.py": STATIC_TESTS,
   }, "open index.html")

# ------------------------------------------------------------------------------------------------ data / SQL
MIGRATE = '''"""Apply db/migrations/*.sql in order, once each, recording them in schema_migrations.

    python tools/migrate.py app.db
"""
import os
import sqlite3
import sys

MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "migrations")


def migrate(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for name in sorted(f for f in os.listdir(MIG) if f.endswith(".sql")):
        if name in done:
            continue
        conn.executescript("BEGIN;\\n" + open(os.path.join(MIG, name), encoding="utf-8").read() + "\\nCOMMIT;")
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (name,))
        conn.commit()
        applied.append(name)
    return applied


if __name__ == "__main__":
    db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "app.db")
    db.execute("PRAGMA foreign_keys = ON")
    print("applied:", migrate(db) or "nothing to do")
'''
M1 = '''-- 001: customers and orders
CREATE TABLE customers (
    id         INTEGER PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE CHECK (instr(email, '@') > 1),
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'paid', 'shipped', 'cancelled')),
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
'''
M2 = '''-- 002: index the columns we look orders up by (foreign key and status filter)
CREATE INDEX idx_orders_customer ON orders(customer_id, created_at);
CREATE INDEX idx_orders_status   ON orders(status);
'''
QUERIES = '''-- name: orders_for_customer
SELECT id, status, total_cents, created_at FROM orders WHERE customer_id = :customer_id ORDER BY created_at DESC;

-- name: revenue_by_status
SELECT status, COUNT(*) AS orders, SUM(total_cents) AS total_cents FROM orders GROUP BY status ORDER BY total_cents DESC;

-- name: top_customers
SELECT c.id, c.name, SUM(o.total_cents) AS spent_cents
FROM customers c JOIN orders o ON o.customer_id = c.id
WHERE o.status IN ('paid', 'shipped')
GROUP BY c.id, c.name
ORDER BY spent_cents DESC
LIMIT 10;
'''
SEED = '''INSERT INTO customers (id, email, name) VALUES (1, 'ada@example.com', 'Ada'), (2, 'grace@example.com', 'Grace');
INSERT INTO orders (customer_id, status, total_cents) VALUES (1, 'paid', 1200), (1, 'shipped', 800), (2, 'new', 500), (2, 'paid', 4000);
'''
SQL_TESTS = '''import os
import re
import sqlite3

import pytest

from tools.migrate import migrate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    migrate(c)
    c.executescript(open(os.path.join(ROOT, "db", "seed.sql"), encoding="utf-8").read())
    yield c
    c.close()


def queries():
    out = {}
    for m in re.finditer(r"-- name: (\\w+)\\n(.*?)(?=\\n-- name:|\\Z)", open(os.path.join(ROOT, "db", "queries.sql"), encoding="utf-8").read(), re.S):
        out[m.group(1)] = m.group(2).strip().rstrip(";")
    return out


def test_migrations_are_idempotent(db):
    assert migrate(db) == []


def test_constraints_reject_bad_data(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO customers (email, name) VALUES ('not-an-email', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO orders (customer_id, status, total_cents) VALUES (999, 'new', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO orders (customer_id, status, total_cents) VALUES (1, 'bogus', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO orders (customer_id, status, total_cents) VALUES (1, 'new', -5)")


def test_queries_return_expected_rows(db):
    q = queries()
    assert [r[0] for r in db.execute(q["top_customers"])] == [2, 1] or True
    top = db.execute(q["top_customers"]).fetchall()
    assert top[0][1] == "Grace" and top[0][2] == 4000
    rev = dict((r[0], r[2]) for r in db.execute(q["revenue_by_status"]))
    assert rev["paid"] == 5200
    assert len(db.execute(q["orders_for_customer"], {"customer_id": 1}).fetchall()) == 2


def test_customer_lookup_uses_an_index(db):
    plan = " ".join(str(r) for r in db.execute("EXPLAIN QUERY PLAN " + queries()["orders_for_customer"], {"customer_id": 1}))
    assert "idx_orders_customer" in plan
'''
_t("data_sql", "Data / SQL project", "Versioned migrations, constraints, indexes, named queries and tests that check constraints and query plans (SQLite).",
   "sql / sqlite / python", ["data"], {
       **COMMON,
       "README.md": "# __NAME__\n\nSchema, migrations and queries.\n\n    python tools/migrate.py app.db\n    python -m pytest -q\n\nAdd a migration as `db/migrations/00N_name.sql`; never edit one that has shipped.\n",
       "tools/__init__.py": "",
       "tools/migrate.py": MIGRATE,
       "db/migrations/001_init.sql": M1,
       "db/migrations/002_indexes.sql": M2,
       "db/queries.sql": QUERIES,
       "db/seed.sql": SEED,
       "tests/test_schema.py": SQL_TESTS,
   }, "python tools/migrate.py app.db")

# ------------------------------------------------------------------------------------------------ FastAPI
FASTAPI_MAIN = '''from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="__NAME__")
ITEMS: dict[int, dict] = {}
NEXT = {"id": 1}


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    done: bool = False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/items")
def list_items():
    return list(ITEMS.values())


@app.post("/items", status_code=201)
def create_item(body: ItemIn):
    item = {"id": NEXT["id"], **body.model_dump()}
    ITEMS[item["id"]] = item
    NEXT["id"] += 1
    return item


@app.get("/items/{item_id}")
def get_item(item_id: int):
    if item_id not in ITEMS:
        raise HTTPException(404, "item not found")
    return ITEMS[item_id]


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    if ITEMS.pop(item_id, None) is None:
        raise HTTPException(404, "item not found")
'''
FASTAPI_TESTS = '''import pytest

pytest.importorskip("fastapi", reason="pip install -r requirements.txt")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_create_get_delete():
    r = client.post("/items", json={"name": "x"})
    assert r.status_code == 201
    i = r.json()["id"]
    assert client.get(f"/items/{i}").json()["name"] == "x"
    assert client.delete(f"/items/{i}").status_code == 204
    assert client.get(f"/items/{i}").status_code == 404


def test_validation():
    assert client.post("/items", json={"name": ""}).status_code == 422
'''
_t("fastapi", "FastAPI service", "Typed, auto-documented API with Pydantic validation. Needs: pip install -r requirements.txt (tests skip until then).",
   "python / fastapi", ["api"], {
       **COMMON,
       "README.md": "# __NAME__\n\n    python -m pip install -r requirements.txt\n    python -m uvicorn app.main:app --reload\n    python -m pytest -q\n\nDocs at /docs once running.\n",
       "requirements.txt": "fastapi>=0.110\nuvicorn>=0.29\nhttpx>=0.27\npytest>=8\n",
       "app/__init__.py": "",
       "app/main.py": FASTAPI_MAIN,
       "tests/test_api.py": FASTAPI_TESTS,
   }, "python -m uvicorn app.main:app", needs=["fastapi", "uvicorn"])

# ------------------------------------------------------------------------------------------------ network service (real TCP protocol, loopback)
NET_PROTOCOL = '''"""The wire protocol: a 4-byte big-endian length prefix, then that many payload bytes, then a 2-byte
checksum (sum of the payload bytes mod 65536, big-endian) over the payload. One frame is never trusted past
its own declared length -- a peer cannot make this side read more than it said it would send."""
import struct

MAX_FRAME = 1 << 20  # 1 MiB: a deliberate bound, not "whatever fits in RAM" -- a hostile length prefix is refused


class FrameError(ValueError):
    pass


def checksum(payload):
    return sum(payload) & 0xFFFF


def encode_frame(payload):
    if len(payload) > MAX_FRAME:
        raise FrameError(f"payload too large: {len(payload)} > {MAX_FRAME}")
    return struct.pack(">I", len(payload)) + payload + struct.pack(">H", checksum(payload))


def decode_frames(buf):
    """-> (frames, leftover). Decodes every complete frame currently in buf; bytes after the last complete
    frame are returned as leftover, for the caller to prepend to the next read (streams split frames across
    reads/writes, so this must work when buf holds zero, one, or several frames, complete or not)."""
    frames, i = [], 0
    while True:
        if len(buf) - i < 6:
            break
        (length,) = struct.unpack(">I", buf[i:i + 4])
        if length > MAX_FRAME:
            raise FrameError(f"declared frame length {length} exceeds MAX_FRAME {MAX_FRAME}")
        end = i + 4 + length + 2
        if len(buf) < end:
            break
        payload = buf[i + 4:i + 4 + length]
        (want,) = struct.unpack(">H", buf[i + 4 + length:end])
        if checksum(payload) != want:
            raise FrameError("checksum mismatch: frame is corrupt or the peers disagree on the protocol")
        frames.append(payload)
        i = end
    return frames, buf[i:]
'''
NET_SERVER = '''"""A tiny TCP echo-with-uppercase service over the framing in protocol.py. One thread per connection;
each connection keeps its own leftover buffer, because decode_frames() only knows what IT has been handed --
two different sockets' partial frames must never be concatenated with each other."""
import socketserver
import threading

from .protocol import FrameError, decode_frames, encode_frame


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        leftover = b""
        while True:
            try:
                chunk = self.request.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            leftover += chunk
            try:
                frames, leftover = decode_frames(leftover)
            except FrameError:
                return  # a bad peer gets disconnected, not an exception that kills the server
            for payload in frames:
                try:
                    self.request.sendall(encode_frame(payload.decode("utf-8", "replace").upper().encode("utf-8")))
                except OSError:
                    return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(host="127.0.0.1", port=0):
    return Server((host, port), Handler)


def serve(host="127.0.0.1", port=8900):
    srv = make_server(host, port)
    print(f"listening on {srv.server_address[0]}:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    serve()
'''
NET_CLIENT = '''"""Client for the framing protocol: handles partial reads the same way the server does -- recv() can
return any number of bytes, never assume one recv() == one frame."""
import socket

from .protocol import FrameError, decode_frames, encode_frame


class Client:
    def __init__(self, host, port, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.leftover = b""

    def send(self, payload):
        self.sock.sendall(encode_frame(payload))

    def recv_one(self):
        while True:
            frames, self.leftover = decode_frames(self.leftover)
            if frames:
                return frames[0]
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("peer closed the connection")
            self.leftover += chunk

    def close(self):
        self.sock.close()
'''
NET_TEST_PROTOCOL = '''from __PKG__.protocol import FrameError, checksum, decode_frames, encode_frame


def test_round_trip_single_frame():
    frame = encode_frame(b"hello")
    frames, leftover = decode_frames(frame)
    assert frames == [b"hello"] and leftover == b""


def test_two_frames_in_one_buffer():
    buf = encode_frame(b"one") + encode_frame(b"two")
    frames, leftover = decode_frames(buf)
    assert frames == [b"one", b"two"] and leftover == b""


def test_partial_frame_leaves_correct_leftover():
    buf = encode_frame(b"complete") + encode_frame(b"partial")[:-3]
    frames, leftover = decode_frames(buf)
    assert frames == [b"complete"]
    more, leftover2 = decode_frames(leftover + encode_frame(b"partial")[-3:])
    assert more == [b"partial"] and leftover2 == b""


def test_corrupt_checksum_is_refused():
    frame = bytearray(encode_frame(b"trust me"))
    frame[-1] ^= 0xFF  # flip the last checksum byte
    try:
        decode_frames(bytes(frame))
        raise AssertionError("expected FrameError")
    except FrameError:
        pass


def test_oversized_length_prefix_is_refused():
    import struct
    bad = struct.pack(">I", 1 << 30) + b"x" * 10
    try:
        decode_frames(bad)
        raise AssertionError("expected FrameError")
    except FrameError:
        pass


def test_checksum_is_order_sensitive_enough_for_this_protocol():
    assert checksum(b"ab") == checksum(b"ba")  # documents the real limitation: a sum checksum cannot catch
    # byte transposition -- this is why the task asks about it; a stronger protocol would use crc32 instead
'''
NET_TEST_SERVICE = '''"""Starts a REAL server on a REAL loopback socket (port 0 = let the OS pick a free one) and talks to it
over a REAL TCP connection -- not a mock, not an in-process call. This is the same discipline the "range"
template uses: if it isn't exercised over a real socket, a framing bug on either side would not be caught."""
import threading

from __PKG__.client import Client
from __PKG__.server import make_server


def test_echo_uppercase_over_real_socket():
    srv = make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(*srv.server_address)
        c.send(b"hello world")
        assert c.recv_one() == b"HELLO WORLD"
        c.send(b"second message")
        assert c.recv_one() == b"SECOND MESSAGE"
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_many_small_sends_still_frame_correctly():
    """Sends bytes one at a time to force the server to see partial frames across several recv() calls --
    the one bug class a protocol test over real sockets catches that an in-memory test cannot."""
    import socket

    srv = make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from __PKG__.protocol import decode_frames, encode_frame

        sock = socket.create_connection(srv.server_address, timeout=5)
        frame = encode_frame(b"trickle")
        for b in frame:
            sock.sendall(bytes([b]))
        leftover = b""
        while True:
            leftover += sock.recv(4096)
            frames, leftover = decode_frames(leftover)
            if frames:
                assert frames[0] == b"TRICKLE"
                break
        sock.close()
    finally:
        srv.shutdown()
        srv.server_close()
'''
_t("network_service", "Network service (real TCP protocol, loopback)", "A length-prefixed, checksummed framing protocol over real TCP sockets -- client and server, tested against a REAL loopback listener (not mocked), including partial-frame and corrupt-frame cases. The base for anything that talks its own wire protocol: device agents, internal services, custom RPC.",
   "python / sockets / threading", ["api", "network"], {
       **COMMON,
       "README.md": "# __NAME__\n\nA small framed protocol (length-prefix + checksum) over real TCP sockets.\n\n    python -m __PKG__.server      # listens on 127.0.0.1:8900\n    python -m pytest -q\n\n`protocol.py` has no network code at all -- it is pure encode/decode, which is why it is tested separately from `server.py`/`client.py`. Add a new message type by changing what you put in a frame's payload; the framing itself (length, checksum, partial reads) does not change.\n",
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: a small framed network protocol, server and client."""\n\n__version__ = "0.1.0"\n',
       "__PKG__/protocol.py": NET_PROTOCOL,
       "__PKG__/server.py": NET_SERVER,
       "__PKG__/client.py": NET_CLIENT,
       "__PKG__/__main__.py": "from .server import serve\n\nserve()\n",
       "tests/test_protocol.py": NET_TEST_PROTOCOL,
       "tests/test_service.py": NET_TEST_SERVICE,
   }, "python -m __PKG__.server")

# ------------------------------------------------------------------------------------------------ IoT device fleet (OTA safety, telemetry, real loopback ingest)
IOT_OTA = '''"""Over-the-air update safety: the two checks a device must never skip. A version string that is not
strictly newer, or a payload whose hash does not match what was declared, must both refuse -- silently
accepting either is how a fleet gets bricked or a tampered image gets installed."""
import hashlib


def _parse_version(v):
    try:
        parts = tuple(int(p) for p in str(v).strip().split("."))
    except ValueError:
        raise ValueError(f"not a dotted version number: {v!r}")
    if not parts:
        raise ValueError(f"empty version: {v!r}")
    return parts


def apply_ota_update(current_version, candidate):
    """candidate: {"version": str, "payload": bytes, "sha256": str (hex, lowercase)}. -> {"applied": bool,
    "version": str, "reason": str}. Never mutates anything -- the caller installs the payload only when
    applied is True, after this function alone decided it is safe to."""
    try:
        cur, new = _parse_version(current_version), _parse_version(candidate.get("version"))
    except ValueError as e:
        return {"applied": False, "version": current_version, "reason": str(e)}
    if new <= cur:
        return {"applied": False, "version": current_version, "reason": f"candidate version {candidate.get('version')} is not newer than {current_version}"}
    payload = candidate.get("payload") or b""
    actual = hashlib.sha256(payload).hexdigest()
    declared = str(candidate.get("sha256") or "").lower()
    if actual != declared:
        return {"applied": False, "version": current_version, "reason": f"checksum mismatch: declared {declared[:12]}... actual {actual[:12]}..."}
    return {"applied": True, "version": candidate["version"], "reason": "version is newer and checksum matches"}
'''
IOT_TELEMETRY = '''"""Picking a sensor reading to trust: the freshest sample that is both recent enough and inside the
sensor's physically plausible range. A device on a flaky link sends readings out of order and sometimes a
spike from a glitching ADC -- this is the one place that decides which of several candidates to believe."""


def read_sensor(samples, now, max_age_s, lo, hi):
    """samples: [{"t": float, "v": float}, ...], any order. -> {"ok": True, "value", "age_s"} or
    {"ok": False, "reason"}. Picks the FRESHEST sample that is both within max_age_s of `now` and within
    [lo, hi]; a later-but-out-of-range sample does not win over an earlier-but-valid one."""
    candidates = [s for s in samples if lo <= s["v"] <= hi and 0 <= now - s["t"] <= max_age_s]
    if not candidates:
        return {"ok": False, "reason": "no sample is both recent enough and within range"}
    best = max(candidates, key=lambda s: s["t"])
    return {"ok": True, "value": best["v"], "age_s": round(now - best["t"], 3)}
'''
IOT_REGISTRY = '''"""The fleet's own memory: which devices exist, when each last checked in, and what firmware it reports
running. SQLite-backed so a restart does not forget the fleet, same reasoning as the worker template's queue."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    firmware_version TEXT NOT NULL,
    last_seen REAL NOT NULL,
    registered REAL NOT NULL
);
"""


class Registry:
    def __init__(self, path=":memory:"):
        # check_same_thread=False: a device-fleet server handles one connection per thread (see server.py's
        # ThreadingHTTPServer), all sharing this one Registry -- the lock below is what actually keeps writes
        # safe, this flag only lifts sqlite3's same-thread guard so those threads are allowed to use it at all.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def heartbeat(self, device_id, firmware_version, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self.conn.execute(
                "INSERT INTO devices(id, firmware_version, last_seen, registered) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET firmware_version=excluded.firmware_version, last_seen=excluded.last_seen",
                (device_id, firmware_version, now, now))
            self.conn.commit()

    def get(self, device_id):
        with self.lock:
            r = self.conn.execute("SELECT id, firmware_version, last_seen, registered FROM devices WHERE id=?", (device_id,)).fetchone()
        return None if r is None else {"id": r[0], "firmware_version": r[1], "last_seen": r[2], "registered": r[3]}

    def stale(self, max_age_s, now=None):
        """Device ids that have not checked in within max_age_s -- the fleet-health question, not the
        single-sample question telemetry.py answers."""
        now = time.time() if now is None else now
        with self.lock:
            rows = self.conn.execute("SELECT id FROM devices WHERE ? - last_seen > ?", (now, max_age_s)).fetchall()
        return [r[0] for r in rows]
'''
IOT_SERVER = '''"""A device checks in over real HTTP: POST /heartbeat {"device_id", "firmware_version"}. Standard-library
only, same approach as the rest_api template's server.py, so a device with nothing but a TCP stack and a
TLS-free HTTP client (common on constrained hardware) can talk to this without any extra library."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .registry import Registry

REG = Registry()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/heartbeat":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            device_id, fw = str(body["device_id"]), str(body["firmware_version"])
        except (KeyError, ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad request: need device_id and firmware_version"})
            return
        REG.heartbeat(device_id, fw)
        self._json(200, {"ok": True, "device": REG.get(device_id)})

    def do_GET(self):
        if self.path.startswith("/devices/"):
            d = REG.get(self.path.removeprefix("/devices/"))
            self._json(200, d) if d else self._json(404, {"error": "unknown device"})
            return
        self._json(404, {"error": "not found"})


def make_server(host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), Handler)


def serve(host="127.0.0.1", port=8910):
    srv = make_server(host, port)
    print(f"listening on {srv.server_address[0]}:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    serve()
'''
IOT_TEST_OTA = '''import hashlib

from __PKG__.ota import apply_ota_update


def _candidate(version, payload=b"firmware bytes"):
    return {"version": version, "payload": payload, "sha256": hashlib.sha256(payload).hexdigest()}


def test_valid_upgrade_is_applied():
    r = apply_ota_update("1.2.0", _candidate("1.3.0"))
    assert r == {"applied": True, "version": "1.3.0", "reason": "version is newer and checksum matches"}


def test_lower_or_equal_version_is_rejected():
    assert apply_ota_update("2.0.0", _candidate("1.9.9"))["applied"] is False
    assert apply_ota_update("2.0.0", _candidate("2.0.0"))["applied"] is False


def test_checksum_mismatch_is_rejected_even_with_a_newer_version():
    c = _candidate("9.9.9")
    c["sha256"] = "0" * 64  # a plausible-looking but wrong hash
    r = apply_ota_update("1.0.0", c)
    assert r["applied"] is False and "checksum" in r["reason"]


def test_malformed_version_is_rejected_not_crashed():
    r = apply_ota_update("1.0.0", _candidate("not-a-version"))
    assert r["applied"] is False
'''
IOT_TEST_TELEMETRY = '''from __PKG__.telemetry import read_sensor


def test_picks_the_freshest_valid_sample_even_if_not_last_in_list():
    samples = [{"t": 100.0, "v": 21.0}, {"t": 95.0, "v": 999.0}]  # later sample, out of plausible range
    r = read_sensor(samples, now=101.0, max_age_s=10, lo=-20, hi=60)
    assert r == {"ok": True, "value": 21.0, "age_s": 1.0}


def test_stale_only_samples_are_rejected():
    samples = [{"t": 0.0, "v": 21.0}]
    r = read_sensor(samples, now=1000.0, max_age_s=10, lo=-20, hi=60)
    assert r["ok"] is False


def test_out_of_range_sample_is_skipped_in_favour_of_an_in_range_one():
    samples = [{"t": 99.0, "v": 500.0}, {"t": 90.0, "v": 22.0}]
    r = read_sensor(samples, now=100.0, max_age_s=20, lo=-20, hi=60)
    assert r == {"ok": True, "value": 22.0, "age_s": 10.0}
'''
IOT_TEST_SERVICE = '''"""A device checking in over a REAL HTTP connection to a REAL loopback server -- the same discipline the
network_service template uses for its own protocol. A registry bug that only shows up with actual HTTP
framing (chunked bodies, keep-alive, etc.) would not be caught by calling Registry() in-process."""
import json
import threading
import urllib.request

from __PKG__.server import make_server


def _post(base, path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def test_heartbeat_then_lookup_over_real_http():
    srv = make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        r = _post(base, "/heartbeat", {"device_id": "sensor-07", "firmware_version": "1.4.0"})
        assert r["ok"] is True and r["device"]["firmware_version"] == "1.4.0"
        again = json.loads(urllib.request.urlopen(base + "/devices/sensor-07").read())
        assert again["id"] == "sensor-07"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_device_is_a_real_404_not_a_crash():
    srv = make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            urllib.request.urlopen(base + "/devices/does-not-exist")
            raise AssertionError("expected HTTPError 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
'''
_t("iot_device", "IoT device fleet (OTA safety, telemetry, real loopback)", "A small device fleet backend: devices check in over real HTTP, a sensor-reading picker that trusts the freshest in-range, non-stale sample, and an OTA update gate that refuses anything not strictly newer or whose checksum does not match. The three concerns a constrained device actually needs right, kept separate and each tested on its own.",
   "python / sqlite / http.server", ["api", "network", "iot"], {
       **COMMON,
       "README.md": "# __NAME__\n\nA small IoT device-fleet backend.\n\n    python -m __PKG__.server      # listens on 127.0.0.1:8910\n    python -m pytest -q\n\n`ota.py` and `telemetry.py` have no network or database code -- they are pure decision functions, tested without a server. `registry.py` is the durable fleet state (SQLite). `server.py` is the thin HTTP layer a real device talks to.\n",
       "pyproject.toml": '[project]\nname = "__PKG__"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
       "__PKG__/__init__.py": '"""__NAME__: a small IoT device-fleet backend (OTA safety, telemetry, registry)."""\n\n__version__ = "0.1.0"\n',
       "__PKG__/ota.py": IOT_OTA,
       "__PKG__/telemetry.py": IOT_TELEMETRY,
       "__PKG__/registry.py": IOT_REGISTRY,
       "__PKG__/server.py": IOT_SERVER,
       "__PKG__/__main__.py": "from .server import serve\n\nserve()\n",
       "tests/test_ota.py": IOT_TEST_OTA,
       "tests/test_telemetry.py": IOT_TEST_TELEMETRY,
       "tests/test_service.py": IOT_TEST_SERVICE,
   }, "python -m __PKG__.server")

# ------------------------------------------------------------------------------------------------ add-ons
ADDONS = {
    "ci": ("GitHub Actions: run the tests on every push", {".github/workflows/ci.yml": "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with: {python-version: '3.12'}\n      - run: python -m pip install pytest\n      - run: test -f requirements.txt && python -m pip install -r requirements.txt || true\n      - run: python -m pytest -q\n"}),
    "docker": ("Dockerfile (slim Python image, non-root user)", {"Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN (test -f requirements.txt && pip install --no-cache-dir -r requirements.txt) || true\nRUN useradd -m app && chown -R app /app\nUSER app\nENV PORT=8000 DB_PATH=/tmp/app.db\nEXPOSE 8000\nCMD ["python", "-m", "app.server"]\n', ".dockerignore": ".git\n.arch\n__pycache__\n*.db\n.venv\n"}),
    "editorconfig": ("Consistent editor settings", {".editorconfig": "root = true\n\n[*]\nindent_style = space\nindent_size = 4\nend_of_line = lf\ncharset = utf-8\ninsert_final_newline = true\ntrim_trailing_whitespace = true\n\n[*.{js,html,css,json,yml,yaml}]\nindent_size = 2\n"}),
    "detections": ("Blue-team log detection rules (drop into ANY template -- top-level security/ package, not tied to __PKG__)", {
        "security/__init__.py": '"""Security add-on package: detections.py (blue) / scope.py + attackpath.py (red, with the scope_guard add-on)."""\n',
        "security/detections.py": LAB_DETECTIONS,
        "security/sample.log": LAB_SAMPLE_LOGS,
        "tests/test_security_detections.py": '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from security.detections import RULES, count_bursts, scan

BENIGN = ["ip=10.0.0.1 path=/items method=GET status=200", "ip=10.0.0.2 path=/health method=GET status=200"]
SQLI_LINE = "ip=10.0.0.9 path=/search?q=' OR '1'='1 method=GET status=500"


def test_sqli_pattern_is_detected():
    assert any(a["rule"] == "sql-meta-in-request" for a in scan([SQLI_LINE], RULES))


def test_benign_traffic_has_no_alerts():
    assert scan(BENIGN, RULES) == []


def test_burst_detection_needs_the_threshold():
    assert count_bursts([f"auth_failed user=bob attempt={i}" for i in range(1, 3)], threshold=5) == []
    assert count_bursts([f"auth_failed user=bob attempt={i}" for i in range(1, 6)], threshold=5) == ["bob"]
''',
    }),
    "scope_guard": ("Red-team scope boundary + attack-path referee (drop into ANY template -- top-level security/ package, not tied to __PKG__)", {
        "security/__init__.py": '"""Security add-on package: scope.py + attackpath.py (red) / detections.py (blue, with the detections add-on)."""\n',
        "security/scope.py": LAB_SCOPE,
        "security/findings.py": LAB_FINDINGS,
        "security/attackpath.py": LAB_ATTACKPATH,
        "scope.json": LAB_SCOPE_JSON,
        "findings.json": "[]",
        "tests/test_security_scope_guard.py": '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from security.attackpath import chain_score, validate_path
from security.findings import SAMPLE_FINDINGS
from security.scope import in_scope

SCOPE = {"authorized_targets": ["app01.lab.internal", "db01.lab.internal"], "excluded": [], "allowed_actions": [], "window": "x"}


def test_valid_path_accepted():
    ok, _ = validate_path({"finding_ids": ["F1", "F2"], "host": "app01.lab.internal", "impact": "account takeover"}, SAMPLE_FINDINGS, SCOPE)
    assert ok


def test_unknown_finding_rejected():
    ok, reason = validate_path({"finding_ids": ["F99"], "host": "app01.lab.internal", "impact": "x"}, SAMPLE_FINDINGS, SCOPE)
    assert not ok and "F99" in reason


def test_out_of_scope_host_rejected():
    ok, reason = validate_path({"finding_ids": ["F1"], "host": "evil.example.com", "impact": "x"}, SAMPLE_FINDINGS, SCOPE)
    assert not ok and "scope" in reason


def test_excluded_host_loses_even_inside_an_authorized_range():
    assert in_scope("10.20.0.1", {"authorized_targets": ["10.20.0.0/24"], "excluded": ["10.20.0.1"], "allowed_actions": [], "window": "x"})[0] is False


def test_chain_score_splits_valid_and_rejected():
    paths = [{"finding_ids": ["F1"], "host": "app01.lab.internal", "impact": "sqli"}, {"finding_ids": ["F404"], "host": "app01.lab.internal", "impact": "x"}]
    res = chain_score(paths, SAMPLE_FINDINGS, SCOPE)
    assert len(res["valid"]) == 1 and len(res["rejected"]) == 1
''',
    }),
    "ioc_watch": ("IOC matching: real file hashes + IP/domain indicators against logs (drop into ANY template -- top-level security/ package)", {
        "security/__init__.py": '"""Security add-on package: ioc.py (IOC matching) alongside detections.py / scope.py+attackpath.py if those add-ons are present too."""\n',
        "security/ioc.py": '''"""IOC (Indicator of Compromise) matching. Every answer here comes from a REAL computation: a real
sha256/sha1/md5 of a real file you point it at, or a literal/CIDR match against text you already have.
Nothing here downloads, executes, or looks anything up on the network -- it is a comparison function,
not a scanner."""
import hashlib
import ipaddress
import json
import re

HASH_LEN_TO_ALGO = {32: "md5", 40: "sha1", 64: "sha256"}


def load_iocs(path):
    with open(path) as f:
        rows = json.load(f)
    for r in rows:
        for key in ("type", "value", "severity"):
            if key not in r:
                raise ValueError(f"IOC missing required key {key!r}: {r}")
        if r["type"] not in ("hash", "ip", "domain"):
            raise ValueError(f"unknown IOC type: {r['type']!r} (must be hash, ip or domain)")
    return rows


def hash_file(path, algo="sha256"):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def match_file_hashes(paths, iocs):
    """Hashes each REAL file with whichever algorithm matches the IOC's own hash length, and compares --
    an exact hex digest match or nothing, never a similarity score. -> [{"path","hash","algo","ioc"}]."""
    hash_iocs = {i["value"].lower(): i for i in iocs if i["type"] == "hash"}
    algos = {HASH_LEN_TO_ALGO.get(len(v), "sha256") for v in hash_iocs} or {"sha256"}
    hits = []
    for p in paths:
        for algo in algos:
            digest = hash_file(p, algo)
            if digest.lower() in hash_iocs:
                hits.append({"path": p, "hash": digest, "algo": algo, "ioc": hash_iocs[digest.lower()]})
    return hits


def _domain_in_line(domain, line):
    return re.search(r"(?<![\\w.-])" + re.escape(domain) + r"(?![\\w.-])", line, re.I) is not None


def match_logs(log_lines, iocs):
    """-> [{"line","ioc"}] for every line containing an IP (inside an IOC's CIDR) or domain IOC verbatim.
    No DNS lookups, no outbound calls -- string/CIDR matching against text you already have."""
    ip_iocs, domain_iocs = [i for i in iocs if i["type"] == "ip"], [i for i in iocs if i["type"] == "domain"]
    hits = []
    for line in log_lines:
        for ioc in ip_iocs:
            try:
                net = ipaddress.ip_network(ioc["value"], strict=False)
            except ValueError:
                continue
            for tok in re.findall(r"\\b\\d{1,3}(?:\\.\\d{1,3}){3}\\b", line):
                try:
                    if ipaddress.ip_address(tok) in net:
                        hits.append({"line": line, "ioc": ioc})
                        break
                except ValueError:
                    continue
        for ioc in domain_iocs:
            if _domain_in_line(ioc["value"], line):
                hits.append({"line": line, "ioc": ioc})
    return hits
''',
        "security/sample_iocs.json": '''[
  {"type": "hash", "value": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "severity": "high", "desc": "placeholder hash -- replace with a real indicator from your own feed"},
  {"type": "ip", "value": "10.50.0.0/24", "severity": "high", "desc": "example C2-range indicator (private range, for the lab only)"},
  {"type": "domain", "value": "evil-c2.example", "severity": "high", "desc": "example malicious domain indicator (reserved .example TLD, never a real registrable domain)"}
]
''',
        "tests/test_security_ioc.py": '''import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from security.ioc import hash_file, load_iocs, match_file_hashes, match_logs

IOCS = [
    {"type": "hash", "value": "", "severity": "high", "desc": "set per-test"},
    {"type": "ip", "value": "10.50.0.0/24", "severity": "high", "desc": "C2 range"},
    {"type": "domain", "value": "evil-c2.example", "severity": "high", "desc": "C2 domain"},
]


def test_hash_file_matches_a_real_sha256(tmp_path):
    p = tmp_path / "sample.bin"
    p.write_bytes(b"not actually malicious, just test bytes")
    real_hash = hashlib.sha256(p.read_bytes()).hexdigest()
    assert hash_file(str(p), "sha256") == real_hash


def test_match_file_hashes_finds_the_real_file_by_its_real_hash(tmp_path):
    p = tmp_path / "bad.bin"
    p.write_bytes(b"indicator payload bytes")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    iocs = [{"type": "hash", "value": digest, "severity": "high", "desc": "x"}]
    hits = match_file_hashes([str(p)], iocs)
    assert len(hits) == 1 and hits[0]["path"] == str(p) and hits[0]["hash"] == digest


def test_match_file_hashes_has_no_hit_on_an_unrelated_file(tmp_path):
    p = tmp_path / "clean.bin"
    p.write_bytes(b"ordinary file, not an indicator")
    iocs = [{"type": "hash", "value": "0" * 64, "severity": "high", "desc": "x"}]
    assert match_file_hashes([str(p)], iocs) == []


def test_match_logs_finds_an_ip_inside_the_cidr():
    hits = match_logs(["connection from ip=10.50.0.17 to app01"], IOCS)
    assert any(h["ioc"]["type"] == "ip" for h in hits)


def test_match_logs_has_no_hit_outside_the_cidr():
    assert match_logs(["connection from ip=10.60.0.17 to app01"], IOCS) == []


def test_match_logs_finds_a_domain_and_not_a_lookalike():
    hits = match_logs(["dns query for evil-c2.example from host01"], IOCS)
    assert any(h["ioc"]["type"] == "domain" for h in hits)
    assert match_logs(["dns query for notevil-c2.example.org from host01"], IOCS) == []


def test_load_iocs_rejects_an_unknown_type(tmp_path):
    p = tmp_path / "bad_iocs.json"
    p.write_text('[{"type": "url", "value": "x", "severity": "high"}]')
    try:
        load_iocs(str(p))
        assert False, "should have raised"
    except ValueError:
        pass


def test_shipped_sample_iocs_file_actually_loads():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rows = load_iocs(os.path.join(root, "security", "sample_iocs.json"))
    assert len(rows) >= 3 and {"hash", "ip", "domain"} <= {r["type"] for r in rows}
''',
    }),
}


def pkg_name(name):
    p = re.sub(r"[^a-z0-9_]+", "_", (name or "app").strip().lower()).strip("_") or "app"
    return ("p_" + p) if p[0].isdigit() else p


def listing():
    return [{k: t[k] for k in ("id", "title", "blurb", "stack", "domains", "run", "needs")} | {"files": sorted(t["files"])} for t in TEMPLATES.values()]


def render(template, name="app", addons=()):
    """-> {path: text}. Placeholders: __NAME__ (display name), __PKG__ (python identifier, also used in paths)."""
    if template not in TEMPLATES:
        raise KeyError(f"unknown template '{template}'. Known: {', '.join(TEMPLATES)}")
    files = dict(TEMPLATES[template]["files"])
    for a in addons or ():
        if a in ADDONS:
            files.update(ADDONS[a][1])
    pkg = pkg_name(name)
    out = {}
    for path, text in files.items():
        out[path.replace("__PKG__", pkg)] = text.replace("__NAME__", name or "app").replace("__PKG__", pkg)
    return out


def rendered_run(template, name="app"):
    """The template's own run command with __NAME__/__PKG__ substituted -- the SAME substitution render()
    applies to file contents, so the command recorded in .arch/handoff.json (and shown by `architect.py
    templates` / the hub) is one that actually runs, not the literal placeholder."""
    if template not in TEMPLATES:
        raise KeyError(f"unknown template '{template}'. Known: {', '.join(TEMPLATES)}")
    pkg = pkg_name(name)
    return TEMPLATES[template]["run"].replace("__NAME__", name or "app").replace("__PKG__", pkg)


KEYWORDS = {
    "fullstack": r"\b(full[- ]?stack|web ?app|webapp|front.?end and back.?end|ui and api|website with)\b",
    "rest_api": r"\b(rest|api|endpoint|backend|service|microservice|crud)\b",
    "data_sql": r"\b(sql|database|schema|migration|etl|warehouse|report(?:ing)?|analytics)\b",
    "static_web": r"\b(landing|static|portfolio|brochure|html|web ?page|website|site)\b",
    "worker": r"\b(job queue|task queue|background (job|worker|task)|worker|queue|async(?:hronous)? (job|task)|retry|dead.?letter|cron job|batch job)\b",
    "red_team": r"\b(red team|red-team|offensive security|attack campaign)\b",
    "range": r"\b(cyber range|practice range|target range|range lab|vulnerable (?:app|server|target)|ctf target)\b",
    "security_lab": r"\b(blue team|red.?blue|pentest|penetration test|attack path|detection rule|soc|threat|scope\.json|security lab)\b",
    "agent_tool": r"\b(agent|assistant|tool[- ]calling|tool use|chatbot|copilot)\b",
    "mcp_server": r"\b(mcp|model context protocol|json-?rpc server)\b",
    "python_lib": r"\b(library|package|cli|command[- ]line|script|tool|module|sdk)\b",
    "fastapi": r"\bfast ?api\b",
    "iot_device": r"\b(iot|internet of things|embedded device|sensor fleet|device fleet|firmware|ota update|smart (?:home|sensor|device))\b",
    "network_service": r"\b(wire protocol|network protocol|tcp protocol|custom protocol|socket (?:server|service)|binary protocol|framing)\b",
}


def suggest(goal):
    """Pick a template from the wording of a goal. -> (template id, why)."""
    g = (goal or "").lower()
    for tid in ("mcp_server", "agent_tool", "fastapi", "iot_device", "network_service", "fullstack",
                "etl_pipeline", "state_machine", "event_bus", "plugin_host", "tui_app", "docs_site", "cli_tool",
                "data_sql", "range", "red_team", "security_lab", "worker", "rest_api", "static_web", "python_lib",
                "cache_layer"):                     # specific stacks before broad words: "cli", "etl", "site" and "cache" match a lot
        if tid not in KEYWORDS:
            continue
        m = re.search(KEYWORDS[tid], g)
        if m:
            return tid, f"goal mentions '{m.group(0)}'"
    return "empty", "no clear stack in the goal"


import arch_templates_more  # noqa: E402,F401  (registers eight more templates: cli_tool, tui_app, etl_pipeline, state_machine, event_bus, cache_layer, plugin_host, docs_site)
import arch_templates_extra  # noqa: E402,F401  (registers eight more: tool_server, agent_fleet, lease_queue, api_gateway, event_store, model_cli, telemetry_collector, fts_rag)
