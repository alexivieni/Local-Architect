"""Self-test for the Architect. A scripted fake Ollama plays every role, so this checks the engine, the
verifiers, the flows, the router and the safety rules end to end, with no model and no internet.   python architect.py selftest
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SPEC = """# Spec: Booking service
## Goals
Let customers book appointment slots and let staff see the day's schedule reliably.
## Non-goals
Payments, multi-tenant billing and native mobile apps are out of scope for the first release.
## Users and key scenarios
Customers book and cancel slots; staff view a daily schedule.
## Requirements
1. R1: A customer can book a free slot.
2. R2: A slot cannot be booked twice.
3. R3: Staff can list the bookings for a day.
## Constraints and assumptions
Assume a single region and SQLite-class load for the first release.
## Risks and open questions
Double booking under concurrency is the main risk.
"""
ARCH_GOOD = """# Architecture
## Overview
A small API in front of one relational store; satisfies R1-R3.
## Components
| component | responsibility | owns data | talks to |
|---|---|---|---|
| api | HTTP endpoints for booking (R1, R2) | no | store |
| store | persists slots and bookings (R2, R3) | yes | - |
## Diagram
```mermaid
flowchart LR
  client[Client] -->|HTTPS JSON| api[Booking API]
  api -->|SQL| store[(Relational store)]
  staff[Staff UI] -->|HTTPS JSON| api
```
## Data flow
1. Client posts a booking. 2. API checks the slot inside a transaction. 3. Store enforces uniqueness.
## Failure modes
Store down: API returns 503 and health check fails. Double booking: unique constraint rejects the second insert.
## Testing strategy
Contract tests from openapi.json; unit tests for the booking rule; a concurrency test for R2.
## Security and privacy basics
Validate input, authenticate staff routes, store only name and contact.
"""
ADR_BAD = "# ADR 1: Use a relational store\n## Status\nAccepted\n## Context\nWe need strong uniqueness guarantees for slots under concurrent booking requests.\n## Decision\nUse a relational database with a unique constraint on slot time.\n"
ADR_GOOD = ADR_BAD + "## Alternatives considered\nA document store was rejected because uniqueness across documents needs application locking. A cache-based lock was rejected as it adds a moving part.\n## Consequences\nGood: the database enforces R2. Bad: scaling writes later needs partitioning or a different design.\n"
OPENAPI_BAD = json.dumps({"openapi": "3.0.3", "info": {"title": "Booking", "version": "1"}, "paths": {"/bookings": {"post": {"responses": {"201": {"description": "ok"}}}}}})
OPENAPI_GOOD = json.dumps({"openapi": "3.0.3", "info": {"title": "Booking", "version": "1"},
                           "paths": {"/bookings": {"post": {"operationId": "createBooking", "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Booking"}}}}, "responses": {"201": {"description": "created"}}},
                                                  "get": {"operationId": "listBookings", "responses": {"200": {"description": "ok"}}}}},
                           "components": {"schemas": {"Booking": {"type": "object", "properties": {"slot": {"type": "string"}}}}}})
SCHEMA = "create table slots(id integer primary key, starts_at text not null unique);\ncreate table bookings(id integer primary key, slot_id integer not null references slots(id), customer text not null);\ncreate index idx_bookings_slot on bookings(slot_id);\n"


def doc(path, content):
    return f"DOC: {path}\n{content}\n"


def good_bundle():
    return doc("docs/architecture.md", ARCH_GOOD) + doc("docs/adr/0001-relational-store.md", ADR_GOOD) + doc("api/openapi.json", OPENAPI_GOOD) + doc("db/schema.sql", SCHEMA)


def bad_bundle():
    return doc("docs/architecture.md", ARCH_GOOD) + doc("docs/adr/0001-relational-store.md", ADR_BAD) + doc("api/openapi.json", OPENAPI_BAD) + doc("db/schema.sql", SCHEMA)


GOOD_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in</title>
<style>body{background:#101418;color:#e8edf2;font-family:system-ui;display:grid;place-items:center;min-height:100vh;margin:0}
form{display:grid;gap:.75rem;width:min(22rem,90vw)}input,button{padding:.6rem;font:inherit}input:focus-visible,button:focus-visible{outline:2px solid #6cf}
@media (max-width:480px){form{width:92vw}}</style></head><body><main><h1>Sign in</h1><form><label for="e">Email</label><input id="e" type="email" required>
<label for="p">Password</label><input id="p" type="password" required><button type="submit">Sign in</button></form></main></body></html>
"""
BAD_HTML = "<div><input placeholder='email'><button></button><img src='x.png'>"


class Fake(BaseHTTPRequestHandler):
    state = {"calls": [], "n": {}}

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        d = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._send({"models": [{"name": "fake:7b", "digest": "x", "size": 4000000000, "details": {"parameter_size": "7B"}}]})
        self._send({}, 404)

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/api/chat":
            return self._send({}, 404)
        msgs = b["messages"]
        full = "\n".join(m["content"] for m in msgs)
        role = (full.rsplit("[role:", 1)[1].split("]")[0]) if "[role:" in full else "?"
        user = msgs[-1]["content"]
        if b.get("think") == "low":
            return self._send({"error": "think level not supported"}, 400)
        st = self.state
        key = role + ":" + str(hash(user) % 997)
        st["n"][role] = st["n"].get(role, 0) + 1
        nctx = (b.get("options") or {}).get("num_ctx")
        st["calls"].append({"role": role, "model": b["model"], "think": b.get("think"), "num_ctx": nctx, "num_predict": (b.get("options") or {}).get("num_predict")})
        txt = self.script(role, user, full, st)
        if st.get("stall_s"):
            import time as _t
            _t.sleep(st["stall_s"])
        # simulate Ollama truncating a prompt that does not fit: it reports a prompt that exactly fills the window
        pec = nctx if (st.get("overflow_below") and nctx and nctx < st["overflow_below"]) else 10
        try:
            self._respond(b, txt, pec)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _respond(self, b, txt, pec):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if b.get("stream") is False:
            # real Ollama sends ONE JSON object (no trailing newline framing) when stream=false
            self.wfile.write(json.dumps({"message": {"role": "assistant", "content": txt}, "done": True, "done_reason": "stop",
                                          "prompt_eval_count": pec, "eval_count": 50, "eval_duration": 10 ** 9}).encode())
            return
        for i in range(0, len(txt), 400):
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": txt[i:i + 400]}, "done": False}) + "\n").encode())
        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": pec, "eval_count": 50, "eval_duration": 10 ** 9}) + "\n").encode())

    def script(self, role, user, full, st):
        if role == "router":
            # a sensible scripted model: follows the keyword hint when there is one, and understands a few plain-language requests the keywords miss
            m = re.search(r"keyword matcher suggests: (\w+)", user)
            req = user.split("REQUEST:", 1)[-1].lower()
            if "slow" in req and "page" in req:
                return '{"flow": "optimize", "why": "performance complaint"}'
            return json.dumps({"flow": m.group(1) if m else "build", "why": "scripted model"})
        if "Write the SPEC" in user:
            return SPEC
        if "Write the design for the spec" in user:
            return good_bundle() if "was rejected" in user else bad_bundle()
        if role == "evaluator":
            ok = "software design" in user or True
            return json.dumps({"scores": {"traceability": {"score": 4, "evidence": "R1-R3 named"}, "simplicity": {"score": 4, "evidence": "two components"}, "failure_modes": {"score": 4, "evidence": "listed"},
                                          "testability": {"score": 4, "evidence": "listed"}, "decisions": {"score": 4, "evidence": "alternatives"}, "no_stubs": {"score": 4, "evidence": "tests pass"}}, "verdict": "pass", "issues": []})
        if "Plan the work as small SPRINTS" in user:
            if "login page" in user:
                return json.dumps({"sprints": [{"name": "login page", "kind": "ui", "goal": "login page", "files": ["index.html"], "acceptance": [{"id": "A1", "text": "has labelled email and password fields"}]}]})
            return json.dumps({"sprints": [{"name": "slugify", "kind": "backend", "goal": "slugify function", "files": ["slugify.py", "tests/test_slugify.py"],
                                            "acceptance": [{"id": "A1", "text": "lowercases and hyphenates"}, {"id": "A2", "text": "strips punctuation"}]}]})
        if "Implement this sprint" in user:
            if "login page" in user:
                return "FILE: index.html\n```html\n" + GOOD_HTML + "```\n"
            test = "FILE: tests/test_slugify.py\n```python\nimport sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom slugify import slugify\n\ndef test_basic():\n    assert slugify('Hello, World!') == 'hello-world'\n\ndef test_spaces():\n    assert slugify('  a   b ') == 'a-b'\n```\n"
            if "FEEDBACK" in user:
                return "FILE: slugify.py\n```python\nimport re\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n```\n" + test
            return "FILE: slugify.py\n```python\ndef slugify(text):\n    return text.lower().replace(' ', '-')\n```\n" + test      # first try is wrong on purpose
        if "reproduces this bug" in user:
            return "FILE: tests/test_repro_pages.py\n```python\nimport sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom app import paginate\n\ndef test_first_page_is_one_based():\n    assert paginate([1, 2, 3, 4], 1, 2) == [1, 2]\n```\n"
        if "Fix this bug" in user:
            return "EDIT: app.py\n<<<<<<< SEARCH\n    start = page * size\n=======\n    start = (page - 1) * size\n>>>>>>> REPLACE\n"
        if "CHARACTERIZATION" in user:
            return "FILE: tests/characterization/test_pricing.py\n```python\nimport sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))\nfrom pricing import price\n\ndef test_member():\n    assert price(100, True) == 90\n\ndef test_guest():\n    assert price(100, False) == 100\n```\n"
        if "Refactor as described" in user:
            return ("EDIT: tests/characterization/test_pricing.py\n<<<<<<< SEARCH\n== 90\n=======\n== 91\n>>>>>>> REPLACE\n"
                    "EDIT: pricing.py\n<<<<<<< SEARCH\n    if member:\n        return amount - amount * 10 / 100\n    return amount - amount * 0 / 100\n=======\n    rate = 10 if member else 0\n    return amount - amount * rate / 100\n>>>>>>> REPLACE\n")
        if role == "reviewer":
            return "- no concerns"
        return "ok"


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_checks as ck
    import arch_core as core
    import arch_flows as fl
    import arch_roles as roles

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{fake.server_address[1]}"
    # The scripted model below answers the SAME prompt differently on successive calls (a flawed draft, then a good revision). A real
    # model at temperature 0 never does that, so the reply cache would correctly collapse the two and the script would fall out of step.
    # Every suite that scripts a model therefore runs with the cache off; arch_selftest_speed turns it back on to test it properly.
    _nc = os.environ.get("ARCHITECT_NO_CACHE")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    # And the suite must not read -- or write -- the install's own learned data. The ledger feeds a past accepted reply into new prompts
    # as a format example, so a machine that had already run this suite once fed the scripted model its own earlier answer and the
    # "first draft is flawed" script never fired. The test was reproducible only on a machine that had never used the app.
    _dd = os.environ.get("ARCHITECT_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="archtest_")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "_data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    quiet = lambda m: None
    try:
        # ---- parsing and safety
        b = core.parse_blocks("noise\nFILE: a/b.py\n```python\nx = 1\n```\nEDIT: c.py\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        check("edit blocks parse (FILE and EDIT)", len(b) == 2 and b[0]["code"] == "x = 1\n" and b[1]["pairs"] == [("old", "new")])
        check("a reply cut off mid-file is flagged, not applied", core.parse_blocks("FILE: a.py\n```python\nx = 1\n")[0]["truncated"])
        cfg = core.load_config()
        for bad in ("../x.py", "/etc/passwd", "C:/x.py", ".git/config", "a/../../b.py", "architect_config.json", "x.exe", ".arch/handoff.json"):
            try:
                core.safe_path(tmp, bad, cfg)
                check(f"path refused: {bad}", False)
            except ValueError:
                pass
        check("unsafe paths are refused (traversal, absolute, .git, own settings, odd types)", True)
        os.makedirs(os.path.join(tmp, "p0"))
        open(os.path.join(tmp, "p0", "f.py"), "w").write("a = 1\nb = 1\nb = 1\n")
        p = core.prepare_block(os.path.join(tmp, "p0"), cfg, {"kind": "edit", "path": "f.py", "pairs": [("a = 1", "a = 2")]})
        check("EDIT applies when SEARCH is unique", p["status"] == "ok" and "a = 2" in p["new"])
        check("EDIT is refused when SEARCH is missing or ambiguous",
              core.prepare_block(os.path.join(tmp, "p0"), cfg, {"kind": "edit", "path": "f.py", "pairs": [("zzz", "y")]})["status"] == "error"
              and core.prepare_block(os.path.join(tmp, "p0"), cfg, {"kind": "edit", "path": "f.py", "pairs": [("b = 1", "b = 2")]})["status"] == "error")

        # ---- verifiers
        h_good, h_bad = ck.check_html(GOOD_HTML, "p"), ck.check_html(BAD_HTML, "p")
        check("HTML checks pass a clean accessible page", all(r["ok"] for r in h_good), [r for r in h_good if not r["ok"]])
        bad_names = {r["name"] for r in h_bad if not r["ok"]}
        check("HTML checks catch: no label, empty button, no alt, unclosed tags, no viewport",
              all(any(k in n for n in bad_names) for k in ("label", "buttons", "alt", "balanced", "viewport")), bad_names)
        check("OpenAPI check passes a good contract", all(r["ok"] for r in ck.check_openapi(OPENAPI_GOOD)), [r for r in ck.check_openapi(OPENAPI_GOOD) if not r["ok"]])
        check("OpenAPI check catches missing operationId / request body", not ck.summarize(ck.check_openapi(OPENAPI_BAD))["ok"])
        check("SQL check passes a keyed, indexed schema", all(r["ok"] for r in ck.check_sql(SCHEMA)), [r for r in ck.check_sql(SCHEMA) if not r["ok"]])
        bad_sql = ck.check_sql("create table t(a text); create table u(id integer primary key, t_id integer references nope(id));")
        check("SQL check catches no primary key and a foreign key to nowhere", len([r for r in bad_sql if not r["ok"]]) >= 2)
        check("SQL assertions run queries on the schema", ck.run_sql_assertions(SCHEMA, [{"setup": "insert into slots(starts_at) values('9');", "sql": "select count(*) from slots", "expect": [[1]]}])[0]["ok"])
        check("Mermaid check accepts a diagram and rejects broken ones", ck.check_mermaid(ARCH_GOOD)[1]["ok"] and not ck.check_mermaid("```mermaid\nflowchart LR\n a[x\n```")[1]["ok"])
        check("ADR check requires alternatives and consequences", ck.summarize(ck.check_adr(ADR_GOOD))["ok"] and not ck.summarize(ck.check_adr(ADR_BAD))["ok"])

        # ---- roles
        models = [{"name": "coder:7b", "params": "7B", "agg": {"backend_fix": {"score": 90}, "backend_feature": {"score": 85}, "format": {"score": 95}, "speed": {"score": 80}, "ui": {"score": 40}}, "rec": {"verdict": "fast"}, "know": {"kind": "coder"}},
                  {"name": "thinker:9b", "params": "9B", "agg": {"reasoning": {"score": 95}, "design_doc": {"score": 85}, "format": {"score": 80}, "api_design": {"score": 80}, "ui": {"score": 85}}, "rec": {"verdict": "split"}, "know": {"kind": "reasoning"}},
                  {"name": "huge:70b", "params": "70B", "agg": {}, "rec": {"verdict": "too_big"}, "know": {"kind": "general"}}]
        ranked, plan = roles.recommend(models)
        check("role recommendation: coder for code, thinker for planning/judging, UI by measured score",
              ranked["generator"][0]["model"] == "coder:7b" and ranked["planner"][0]["model"] == "thinker:9b" and ranked["evaluator"][0]["model"] == "thinker:9b"
              and ranked["generator_ui"][0]["model"] == "thinker:9b", {r: x[0]["model"] for r, x in ranked.items()})
        check("a model that does not fit is never recommended", all(x["score"] == 0 for r in ranked.values() for x in r if x["model"] == "huge:70b"))
        check("measured vs guess is labelled", ranked["generator"][0]["basis"] in ("measured", "partly measured") and next(x for x in ranked["router"] if x["model"] == "huge:70b")["basis"] == "guess")
        check("assignment reuses a model within tolerance to avoid swaps", plan["distinct_models"] <= 2, plan)

        # ---- flows against the fake model
        def eng(root, goal, flow, review="auto", **over):
            c = core.load_config(root)
            c["roles"] = {r: "fake:7b" for r in roles.ROLES}
            c["limits"]["max_revisions"] = 3
            c["behavior"]["playwright"] = over.pop("playwright", "off")
            c["behavior"]["battery_saver"] = "off"     # these checks are about the engine, not the power cable: on battery the saver
                                                       # (correctly) shrinks the window, which made them pass or fail by time of night
            c = core.deep_merge(c, over)
            return core.Engine(root, flow, goal, c, review=review, say=quiet)

        # router
        r0 = os.path.join(tmp, "r0")
        os.makedirs(r0)
        e = eng(r0, "x", "route")
        e.cfg["behavior"]["routing"] = "keywords"
        check("router (keywords mode): keywords decide clear cases",
              fl.route(e, "Fix the crash: Traceback (most recent call last) when saving")[0] == "bugfix"
              and fl.route(e, "Refactor and clean up the payment module")[0] == "refactor"
              and fl.route(e, "Design the architecture and write an ADR for the booking service")[0] == "design")
        check("router: ambiguous goals fall back to the router model", fl.route(e, "do the thing with orders")[2].startswith("router model"))
        e.cfg["behavior"]["routing"] = "model"
        fr_empty = fl.route(e, "the checkout page feels really slow when I open it")
        check("router (default): in an EMPTY folder the code overrides a test/optimize/review pick and starts a project", fr_empty[0] == "new", fr_empty)
        r1 = os.path.join(tmp, "r1")
        os.makedirs(r1)
        open(os.path.join(r1, "app.py"), "w").write("x = 1\n")
        e = eng(r1, "x", "route")
        e.cfg["behavior"]["routing"] = "model"
        fr = fl.route(e, "Refactor and clean up the payment module")
        check("router (default): the model decides and says whether it agrees with the keyword hint", fr[0] == "refactor" and "agrees with the keyword hint" in fr[2], fr)
        fr2 = fl.route(e, "the checkout page feels really slow when I open it")
        check("router (default): the model catches a request the keywords miss", fr2[0] == "optimize" and fr2[2].startswith("router model"), fr2)
        e.cfg["roles"]["router"] = "gone:1b"
        core.Engine.call, _orig_call = (lambda self, *a, **k: (_ for _ in ()).throw(core.LLMError("down"))), core.Engine.call
        try:
            fr3 = fl.route(e, "Fix the crash: Traceback (most recent call last) when saving")
            fr4 = fl.route(e, "do the thing with orders")
        finally:
            core.Engine.call = _orig_call
        check("router: when no model answers, a clear keyword match still routes and says so; vague requests get a default", fr3[0] == "bugfix" and "no model answered" in fr3[2] and fr4[0] in fl.FLOWS, (fr3, fr4))
        check("router: domains are detected", fl.detect_domains(e, "REST endpoint with a postgres schema") == ["api", "data"] and "web" in fl.detect_domains(e, "responsive landing page"))

        # design (first bundle is flawed on purpose -> revise loop must fix it)
        d1 = os.path.join(tmp, "design")
        os.makedirs(d1)
        e = eng(d1, "Design a booking API with a database", "design")
        out = fl.run_flow(e, "design", {"domains": ["api", "data"]})
        files_ok = all(os.path.isfile(os.path.join(d1, f)) for f in ("docs/spec.md", "docs/architecture.md", "docs/adr/0001-relational-store.md", "api/openapi.json", "db/schema.sql"))
        check("design flow writes spec, architecture, ADR, OpenAPI and schema", out["ok"] and files_ok, (out, e.report_lines[-12:]))
        adr = open(os.path.join(d1, "docs/adr/0001-relational-store.md")).read()
        check("design flow's checks rejected the flawed first draft and the revision fixed it", "Alternatives considered" in adr and Fake.state["n"].get("architect", 0) >= 2)
        ho = core.load_json(os.path.join(d1, ".arch", "handoff.json"))
        check("design flow leaves a handoff for the build flow", ho.get("next") == "build" and "api/openapi.json" in ho.get("artifacts", []))
        tr = [json.loads(l) for l in open(os.path.join(e.run_dir, "trace.jsonl"))]
        marks = {t["id"]: t["accepted"] for t in tr if t["type"] == "mark"}
        arch_calls = [t["id"] for t in tr if t["type"] == "call" and t["role"] == "architect"]
        check("every call is traced and outcome-labelled (rejected draft = False, accepted revision = True)", marks.get(arch_calls[0]) is False and marks.get(arch_calls[-1]) is True, (arch_calls, marks))

        # build backend (first try wrong -> fix loop)
        b1 = os.path.join(tmp, "build")
        os.makedirs(b1)
        e = eng(b1, "Build a slugify function with tests", "build")
        out = fl.run_flow(e, "build", {"domains": ["api"]})
        code = open(os.path.join(b1, "slugify.py")).read() if os.path.isfile(os.path.join(b1, "slugify.py")) else ""
        check("build flow: wrong first attempt is caught by tests, fixed on feedback, sprint passes", out["ok"] and "re.sub" in code, e.report_lines[-14:])
        check("build flow: contract and handoff are saved", os.path.isfile(os.path.join(b1, ".arch", "contract.json")) and core.load_json(os.path.join(b1, ".arch", "handoff.json")).get("sprints_done"))
        ev_calls = [c for c in Fake.state["calls"] if c["role"] == "evaluator"]
        check("build flow: the strict judge runs once at the end", len(ev_calls) >= 2, (len(ev_calls), [c["role"] for c in Fake.state["calls"]]))

        # build UI with real browser smoke test when available
        u1 = os.path.join(tmp, "ui")
        os.makedirs(u1)
        e = eng(u1, "Build a login page", "build", playwright="auto")
        out = fl.run_flow(e, "build", {"domains": ["web"]})
        check("build flow (UI): page passes the accessibility/structure checks", out["ok"] and os.path.isfile(os.path.join(u1, "index.html")), e.report_lines[-8:])
        check("UI work used the UI coder role", any(t for t in [json.loads(l) for l in open(os.path.join(e.run_dir, "trace.jsonl"))] if t["type"] == "call" and t["role"] == "generator_ui"))

        # bugfix
        g1 = os.path.join(tmp, "bug")
        os.makedirs(os.path.join(g1, "tests"))
        open(os.path.join(g1, "app.py"), "w").write("def paginate(items, page, size):\n    start = page * size\n    return items[start:start + size]\n")
        e = eng(g1, "paginate returns the wrong items: page 1 should be the first items", "bugfix")
        out = fl.run_flow(e, "bugfix", {})
        check("bugfix flow: failing repro test, minimal fix, repro now passes", out["ok"] and "(page - 1)" in open(os.path.join(g1, "app.py")).read() and os.path.isfile(os.path.join(g1, "tests", "test_repro_pages.py")), e.report_lines[-10:])

        # refactor (model also tries to weaken the safety-net test: must be blocked)
        f1 = os.path.join(tmp, "ref")
        os.makedirs(f1)
        open(os.path.join(f1, "pricing.py"), "w").write("def price(amount, member):\n    if member:\n        return amount - amount * 10 / 100\n    return amount - amount * 0 / 100\n")
        e = eng(f1, "Refactor pricing.py to remove the duplicated discount calculation", "refactor")
        out = fl.run_flow(e, "refactor", {})
        newp = open(os.path.join(f1, "pricing.py")).read()
        chars = open(os.path.join(f1, "tests/characterization/test_pricing.py")).read()
        check("refactor flow: pins behaviour first, refactors, tests stay green", out["ok"] and "rate = 10 if member else 0" in newp, e.report_lines[-10:])
        check("refactor flow: the model cannot edit the safety-net tests", "== 90" in chars and "== 91" not in chars)

        # review modes + undo
        y1 = os.path.join(tmp, "dry")
        os.makedirs(y1)
        e = eng(y1, "Build a login page", "build", review="dry", playwright="off")
        fl.run_flow(e, "build", {"domains": ["web"]})
        check("dry mode writes nothing", not os.path.exists(os.path.join(y1, "index.html")))
        answers = iter(["n"])
        e = eng(y1, "Build a login page", "build", review="ask")
        e.ask = lambda prompt: next(answers, "")
        fl.run_flow(e, "build", {"domains": ["web"]})
        check("ask mode: answering 'n' (or nothing) applies nothing", not os.path.exists(os.path.join(y1, "index.html")))
        run_dir = core.latest_run(f1)
        before = open(os.path.join(f1, "pricing.py")).read()
        check("undo restores edited files and removes created ones", core.undo_run(f1, run_dir) >= 2 and "if member:" in open(os.path.join(f1, "pricing.py")).read() and before != open(os.path.join(f1, "pricing.py")).read())

        # budget and loop guards
        e = eng(r0, "x", "x", limits={"max_calls": 1})
        e.call("router", "hello")
        try:
            e.call("router", "again")
            check("call budget stops a runaway run", False)
        except core.BudgetExceeded:
            check("call budget stops a runaway run", True)
        e = eng(r0, "x", "x")
        e.call("router", "same")
        e.call("router", "same")
        try:
            e.call("router", "same")
            check("loop guard stops the same question asked 3 times", False)
        except core.LoopDetected:
            check("loop guard stops the same question asked 3 times", True)
        e = eng(r0, "x", "x", role_overrides={"planner": {"think": "low"}})
        e.call("planner", "think level test")
        check("a rejected think level falls back instead of failing", True)
        # behavior.model_defaults: off (the default) changes nothing; auto fills only what the person left unset
        e = eng(r0, "x", "x")
        opts0 = {"temperature": 0.33, "top_p": 0.5}
        o_off = dict(opts0)
        md_off = e._model_defaults("qwen3:8b", "planner", {}, {}, o_off)
        check("model_defaults off (the default): no family settings applied, options unchanged",
              e.cfg["behavior"].get("model_defaults", "off") == "off" and md_off == {} and o_off == opts0, (md_off, o_off))
        e.cfg["behavior"]["model_defaults"] = "auto"
        o_on = dict(opts0)
        md_on = e._model_defaults("qwen3:8b", "planner", {"sampling": {"temperature": 0.33}}, {}, o_on)
        check("model_defaults auto: a role's own temperature is kept, only unset keys are filled, and the job is recorded",
              o_on["temperature"] == 0.33 and (md_on == {} or ("job" in md_on and "temperature" not in (md_on.get("sampling") or {}))), (md_on, o_on))
        e = eng(r0, "x", "x")
        e.cfg["roles"].update({"generator": "fake:7b"})
        check("an unset role falls back to the next role in its chain", core.Engine.model_for(e, "generator_sql") == "fake:7b")

        # ---- calibration: context window guard and derived timeouts (arch_tune)
        import arch_tune as tune
        st = Fake.state
        st["overflow_below"] = 4096
        st["calls"].clear()
        e = eng(r0, "x", "x", limits={"num_ctx": 2048})
        res = e.call("router", "fill the window")
        nctxs = [c["num_ctx"] for c in st["calls"]]
        check("a prompt that fills the window is retried once with a doubled window", nctxs == [2048, 4096], nctxs)
        check("...and the retry's answer is clean, with the larger window remembered for the run", res["ctx_overflow"] is None and e.ctx_floor.get("fake:7b") == 4096)
        check("...and the retry counts against the call budget", e.calls == 2)
        st["calls"].clear()
        res = e.call("router", "second call same model")
        check("later calls to that model start at the larger window", [c["num_ctx"] for c in st["calls"]] == [4096])
        e = eng(r0, "x", "x", limits={"num_ctx": 2048, "num_ctx_max": 2048})
        res = e.call("router", "no headroom")
        check("at the ceiling it warns instead of growing, and says what to change", res["ctx_overflow"] == "truncated" and any("WARNING" in m and "Context size" in m for m in e.ctx_events), e.ctx_events)
        e = eng(r0, "x", "x", limits={"num_ctx": 2048, "ctx_autogrow": False})
        st["calls"].clear()
        e.call("router", "autogrow off")
        check("limits.ctx_autogrow=false never retries", len(st["calls"]) == 1)
        st["overflow_below"] = 0
        e = eng(r0, "x", "x", limits={"num_ctx": 2048})
        st["calls"].clear()
        e.call("router", "z" * 7000)
        check("a long prompt lowers the reply limit so prompt + reply fit the window",
              st["calls"][0]["num_predict"] < 1024 and st["calls"][0]["num_predict"] >= tune.MIN_REPLY_TOKENS, st["calls"])
        e = eng(r0, "x", "x", limits={"num_ctx": 8192, "context_chars": 20000})
        check("default context budget (20000 chars) is cut to fit an 8192-token window with room to answer",
              e.effective_context_chars() < 20000 and tune.estimate_tokens(e.effective_context_chars()) + tune.reply_reserve(8192) + tune.FIXED_OVERHEAD_TOKENS <= 8192)
        e = eng(r0, "x", "x", limits={"num_ctx": 32768, "context_chars": 20000})
        check("the user's own smaller context_chars is never raised", e.effective_context_chars() == 20000, (e.effective_context_chars(), e.stats.get("fake:7b"), e.ctx_floor))
        fast = tune.timeouts_for({"gen_tok_s": 120, "prefill_tok_s": 4000, "load_s": 2, "calls": 5}, 6000, 800, 900)
        slow = tune.timeouts_for({"gen_tok_s": 3, "prefill_tok_s": 60, "load_s": 40, "calls": 5}, 6000, 800, 900)
        check("a fast small model gets short timeouts and a slow big one gets long ones",
              fast["first"] < 60 and fast["idle"] <= 40 and slow["first"] > 5 * fast["first"] and slow["total"] > 5 * fast["total"], (fast, slow))
        check("derived timeouts never exceed the configured ceiling", tune.timeouts_for({"gen_tok_s": 0.5, "prefill_tok_s": 5}, 30000, 8000, 300)["total"] <= 300)
        o = tune.observe({}, "m", prompt_chars=9000, prompt_tokens=3000, num_ctx=8192, gen_tok_s=40, prefill_tok_s=900)
        check("chars-per-token is learned from real calls, but not from a truncated prompt",
              abs(o["m"]["cpt"] - 3.0) < 0.01 and "cpt" not in tune.observe({}, "m", prompt_chars=9000, prompt_tokens=8190, num_ctx=8192)["m"])
        st["stall_s"] = 3
        e = eng(r0, "x", "x", limits={"timeout_mode": "fixed", "request_timeout": 1})
        t_0 = __import__("time").time()
        try:
            e.call("router", "stalled server")
            check("a model that never answers is stopped at the timeout with a plain-language reason", False)
        except core.LLMError as ex:
            check("a model that never answers is stopped at the timeout with a plain-language reason",
                  "first token" in str(ex) and __import__("time").time() - t_0 < 2.9, str(ex))
        st["stall_s"] = 0

        # offline guard for tests the model wrote
        os.makedirs(os.path.join(tmp, "net"))
        open(os.path.join(tmp, "net", "t.py"), "w").write("import socket\ns = socket.create_connection(('192.0.2.1', 9), timeout=1)\n")
        core.set_net({"behavior": {"network": "offline"}})  # explicit: don't rely on whatever a prior flow left the shared _NET state as
        rc, out_ = core.run_cmd([sys.executable, "t.py"], os.path.join(tmp, "net"), 30, True)
        check("with behavior.network='offline', generated code cannot reach the network", rc != 0 and "block" in out_.lower(), out_[-200:])

        # the three network modes stay cohesive end to end: behavior.* config -> env vars -> netguard's
        # actual enforcement (a real subprocess, a real blocked/allowed connection -- not asserted on config alone)
        core.set_net(core.deep_merge(core.load_config(tmp), {"behavior": {"network": "open"}}))
        rc_o, out_o = core.run_cmd([sys.executable, "t.py"], os.path.join(tmp, "net"), 30, True)
        check("behavior.network='open' actually lifts the block (not silently coerced back to offline)", "block" not in out_o.lower(), out_o[-200:])
        core.set_net(core.deep_merge(core.load_config(tmp), {"behavior": {"network": "practice", "net_allow": ["203.0.113.5"]}}))
        rc_p, out_p = core.run_cmd([sys.executable, "t.py"], os.path.join(tmp, "net"), 30, True)
        check("behavior.network='practice' still blocks a host that is not on net_allow", "block" in out_p.lower(), out_p[-200:])
        open(os.path.join(tmp, "net", "tool.py"), "w").write("import subprocess\nsubprocess.run(['ssh', 'x'])\n")
        core.set_net(core.deep_merge(core.load_config(tmp), {"behavior": {"network": "open", "net_block_tools": False}}))
        rc_t, out_t = core.run_cmd([sys.executable, "tool.py"], os.path.join(tmp, "net"), 30, True)
        check("behavior.net_block_tools=False actually allows launching a denylisted binary (env var is read, not ignored)", "blocked launching" not in out_t.lower(), out_t[-300:])
        core.set_net(core.load_config(tmp))  # restore defaults for anything that runs after this in the same process
    finally:
        fake.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(HERE, "._guard"), ignore_errors=True)
        if _nc is None:
            os.environ.pop("ARCHITECT_NO_CACHE", None)
        else:
            os.environ["ARCHITECT_NO_CACHE"] = _nc
        if _dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = _dd
    print()
    if all(results):
        print(f"  ALL {len(results)} ARCHITECT CHECKS PASSED")
        return 0
    print(f"  {results.count(False)} of {len(results)} CHECKS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(run())
