"""Architect Hub: the same local hub app as before (model_hub.py: models, fit math, probes, scoreboard, jobs),
extended for the Architect: roles with per-role model recommendations, flows, a portfolio of apps,
design/SQL/API/refactor tests, and a run console.

    python architect_hub.py            opens http://127.0.0.1:8766
    python architect_hub.py --no-browser --port 9000

Same safety model as the base hub: 127.0.0.1 only, per-start token on every change, Host/Origin checks.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import arch_checks as ck
import arch_core as core
import arch_flows as fl
import arch_params as params
import arch_roles as roles
import arch_runner as runner
import arch_taskkit as taskkit
import arch_tasks
import arch_templates as templates
import model_hub as mh

HERE = os.path.dirname(os.path.abspath(__file__))
_GPU_PLAN = {}            # /api/arch/gpu: the server plan, re-read at most once a minute
_LIVE_LAST = {}           # the live recorder's newest sample (behavior.record_live), reused by /api/arch/gpu while it is fresh
LIVE_FRESH_S = 7.0        # the recorder samples every 5 s; older than this and the endpoint measures for itself


def _keep_recorder_samples(arch_hw):
    """Wrap arch_hw.record_once (looked up by name on every pass of the recorder's loop) so each sample it writes is also kept here.
    The console's GPU strip then reads that sample instead of starting nvidia-smi and tasklist a second time every 5 s."""
    if getattr(arch_hw.record_once, "_keeps_last", False):
        return
    orig = arch_hw.record_once

    def record_once(*a, **k):
        row = orig(*a, **k)
        if isinstance(row, dict):
            _LIVE_LAST.clear()
            _LIVE_LAST.update(row, _at=time.time())
        return row
    record_once._keeps_last = True
    arch_hw.record_once = record_once


def _live_sample(arch_hw, now=None):
    """The same answer as arch_hw.live(): the recorder's sample when it is under LIVE_FRESH_S old, else a fresh measurement (the
    recorder off, stalled or not started yet). The recorder's own fields (time, running servers) are left out."""
    now = time.time() if now is None else now
    row = dict(_LIVE_LAST)
    if row and now - row.get("_at", 0) < LIVE_FRESH_S:
        for k in ("_at", "t", "servers"):
            row.pop(k, None)
        return row
    return arch_hw.live()
PORTFOLIO_FILE = os.path.join(core.HOME, "portfolio.json")

# ---------------------------------------------------------------------------------------------
# Wire the base hub to the Architect
# ---------------------------------------------------------------------------------------------
mh.HERE = HERE
mh.AGENT = os.path.join(HERE, "architect.py")
mh.CONFIG_FILE = core.CONFIG_FILE
mh.UI_FILE = os.path.join(HERE, "hub_ui.html")
mh.ollama_host = core.ollama_host            # one host setting for the hub and the agent (env OLLAMA_HOST or architect_config.json)
mh.DATA_DIR = core.data_dir()
mh.RESULTS_FILE = os.path.join(mh.DATA_DIR, "results.json")
mh.NOTES_FILE = os.path.join(core.HOME, "NOTES.md")
mh.CATEGORIES[:] = roles.CATEGORIES
mh.DEFAULT_WEIGHTS.clear()
mh.DEFAULT_WEIGHTS.update({"backend_fix": 12, "backend_feature": 12, "ui": 14, "reasoning": 10, "security": 4, "format": 8, "toolcall": 0,
                           "speed": 8, "sql": 10, "api_design": 8, "design_doc": 10, "refactor": 4,
                           "testing": 8, "bughunt": 6, "optimize": 6})
arch_tasks.install()


# ---------------------------------------------------------------------------------------------
# New test categories (checked by code, same verifiers the flows use)
# ---------------------------------------------------------------------------------------------
def doc_of(reply):
    t = mh.strip_think(reply).strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", t, re.S)
    return (m.group(1) if m else t)


def _frac(results):
    return sum(1 for r in results if r["ok"]) / max(len(results), 1)


def sql_schema_check(reply):
    sql = mh.code_of(reply)
    res = ck.check_sql(sql)
    low = sql.lower()
    names = [("book" in low), ("member" in low or "patron" in low or "user" in low), ("loan" in low or "borrow" in low or "checkout" in low)]
    s = 0.7 * _frac(res) + 0.3 * (sum(names) / 3)
    bad = [r["name"] for r in res if not r["ok"]]
    return round(s, 2), ("all checks passed" if not bad and all(names) else "missing: " + "; ".join(bad + ([] if all(names) else ["expected books/members/loans tables"]))[:200]), True


def sql_query_check(reply):
    q = mh.code_of(reply).strip().rstrip(";")
    con = sqlite3.connect(":memory:")
    con.executescript("create table customers(id integer primary key, name text not null);create table orders(id integer primary key, customer_id integer not null references customers(id), amount real not null);"
                      "insert into customers values (1,'Ann'),(2,'Bob'),(3,'Cy');insert into orders values (1,1,60),(2,1,70),(3,2,90),(4,3,150),(5,3,10);")
    try:
        rows = [list(r) for r in con.execute(q).fetchall()]
    except sqlite3.Error as e:
        return 0.0, f"query failed: {str(e)[:100]}", False
    norm = [[r[0], float(r[1])] for r in rows if len(r) >= 2]
    return (1.0, "correct rows in the right order", True) if norm == [["Cy", 160.0], ["Ann", 130.0]] else (0.3, f"ran, wrong rows: {rows[:3]}", True)


def sql_migration_check(reply):
    script = mh.code_of(reply)
    con = sqlite3.connect(":memory:")
    con.executescript("create table users(id integer primary key, email text not null);insert into users(email) values ('a@x.com'),('b@x.com');")
    try:
        con.executescript(script)
        st = [r[0] for r in con.execute("select status from users").fetchall()]
        idx = [r[1] for r in con.execute("pragma index_list('users')").fetchall()]
        cols = {r[1]: r for r in con.execute("pragma table_info('users')").fetchall()}
        ok = [st == ["active", "active"], "status" in cols and cols["status"][3] == 1, any(con.execute(f"pragma index_info('{i}')").fetchall()[0][2] == "email" for i in idx) if idx else False]
        return round(sum(ok) / 3, 2), "migration ok" if all(ok) else f"checks (rows kept+default, NOT NULL, index): {ok}", True
    except sqlite3.Error as e:
        return 0.0, f"migration failed: {str(e)[:100]}", False


def api_todo_check(reply):
    txt = mh.code_of(reply)
    res = ck.check_openapi(txt)
    try:
        paths = json.loads(txt).get("paths", {})
    except ValueError:
        return 0.0, "invalid JSON", False
    col = next((v for k, v in paths.items() if re.fullmatch(r"/(api/)?(v\d/)?todos/?", k)), {})
    item = next((v for k, v in paths.items() if re.fullmatch(r"/(api/)?(v\d/)?todos/{\w+}/?", k)), {})
    cov = [("get" in col), ("post" in col), ("get" in item), ("put" in item or "patch" in item), ("delete" in item)]
    s = 0.6 * _frac(res) + 0.4 * (sum(cov) / 5)
    bad = [r["name"] for r in res if not r["ok"]]
    return round(s, 2), "complete" if not bad and all(cov) else f"gaps: {'; '.join(bad)[:120]} coverage={cov}", True


def api_login_check(reply):
    txt = mh.code_of(reply)
    res = ck.check_openapi(txt)
    try:
        d = json.loads(txt)
    except ValueError:
        return 0.0, "invalid JSON", False
    op = next((v.get("post") for k, v in (d.get("paths") or {}).items() if "login" in k), None) or {}
    resp = op.get("responses") or {}
    refs = txt.count("$ref")
    chk = [bool(resp.get("200") or resp.get("201")), "401" in resp, refs >= 1, bool((d.get("components") or {}).get("schemas"))]
    s = 0.5 * _frac(res) + 0.5 * (sum(chk) / 4)
    return round(s, 2), "complete" if all(chk) and _frac(res) == 1 else f"checks (200, 401, $ref, schemas): {chk}", True


def adr_check(reply):
    t = doc_of(reply)
    res = ck.check_adr(t)
    both = "sqlite" in t.lower() and "postgres" in t.lower()
    s = 0.7 * _frac(res) + 0.2 * (1 if both else 0) + 0.1 * (1 if len(t) > 600 else 0)
    bad = [r["name"] for r in res if not r["ok"]]
    return round(s, 2), "complete" if not bad and both else f"missing: {'; '.join(bad)[:120]}{'' if both else ' both options named'}", True


def arch_doc_check(reply):
    t = doc_of(reply)
    res = ck.check_mermaid(t)
    secs = ck._sections(t)
    want = [ck._has(secs, "overview", "context"), ck._has(secs, "component"), ck._has(secs, "failure"), ck._has(secs, "test")]
    s = 0.5 * _frac(res) + 0.5 * (sum(want) / 4)
    return round(s, 2), "complete" if all(want) and _frac(res) == 1 else f"sections(overview, components, failure modes, testing)={want}; diagram ok={_frac(res) == 1}", True


def refactor_task(title, prompt, tests, min_funcs=None, dup_line=None):
    def check(reply):
        code = mh.code_of(reply)
        try:
            import ast
            tree = ast.parse(code)
        except SyntaxError as e:
            return 0.0, f"syntax error: {e.msg}", False
        ok, out = mh.run_python_guarded(code + "\n\n" + tests)
        if not ok:
            return 0.0, "behaviour changed: " + (out.strip().splitlines()[-1] if out.strip() else "tests failed"), True
        n = sum(isinstance(x, ast.FunctionDef) for x in ast.walk(tree))
        if min_funcs and n < min_funcs:
            return 0.6, f"behaviour kept, but only {n} function(s) (expected extraction)", True
        if dup_line and code.count(dup_line) > 1:
            return 0.6, "behaviour kept, duplication remains", True
        return 1.0, "behaviour kept and structure improved", True
    return {"title": title, "prompt": prompt + "\nReply with ONE ```python code block containing the complete refactored code and nothing else.", "check": check}


SQL_NOTE = "\nReply with ONE ```sql code block and nothing else."
mh.TASKS.update({
    "sql": [
        {"title": "library schema", "prompt": "Design a SQLite schema for a small library: books, members and loans (a member may borrow many books; a book can be on loan to only one member at a time; record the due date). Use primary keys, NOT NULL, foreign keys and indexes where they matter." + SQL_NOTE, "check": sql_schema_check},
        {"title": "aggregate query", "prompt": "Tables: customers(id, name) and orders(id, customer_id, amount). Write ONE query returning each customer's name and total order amount (columns: name, total), only customers whose total is above 100, highest total first." + SQL_NOTE, "check": sql_query_check},
        {"title": "safe migration", "prompt": "Table users(id integer primary key, email text not null) already holds rows. Write SQL that adds a NOT NULL column status with default 'active' (existing rows must become 'active') and creates an index on email." + SQL_NOTE, "check": sql_migration_check},
    ],
    "api_design": [
        {"title": "todo REST API", "prompt": "Design an OpenAPI 3.0 document (JSON) for a todo API: list and create todos; get, update and delete one todo by id. A todo has id, title and done. Include operationIds, request bodies and response schemas via components.\nReply with ONE ```json code block and nothing else.", "check": api_todo_check},
        {"title": "login with errors", "prompt": "Design an OpenAPI 3.0 document (JSON) with ONE operation: POST /login taking email and password, returning a token on success (200) and an error object on failure (401). Define reusable schemas in components and use $ref.\nReply with ONE ```json code block and nothing else.", "check": api_login_check},
    ],
    "design_doc": [
        {"title": "decision record", "prompt": "Write an Architecture Decision Record choosing between SQLite and PostgreSQL for an internal tool used by about 20 people. Use the headings: Status, Context, Decision, Alternatives considered, Consequences. Be concrete about trade-offs.\nReply with the Markdown document only.", "check": adr_check},
        {"title": "architecture doc", "prompt": "Write a short architecture document for a URL shortener service. Headings: Overview, Components (table), Diagram (a ```mermaid flowchart with at least 4 nodes and labelled edges), Failure modes, Testing strategy.\nReply with the Markdown document only (no outer code fence).", "check": arch_doc_check},
    ],
    "refactor": [
        refactor_task("remove duplication",
                      "Refactor this code to remove the duplicated discount logic WITHOUT changing behaviour (keep the function names and signatures).\n```python\ndef member_price(amount):\n    discounted = amount - amount * 10 / 100\n    return round(discounted, 2)\n\ndef vip_price(amount):\n    discounted = amount - amount * 20 / 100\n    return round(discounted, 2)\n```",
                      "assert member_price(100) == 90.0 and vip_price(100) == 80.0 and member_price(19.99) == 17.99 and vip_price(0) == 0\nprint('ok')", min_funcs=3),
        refactor_task("extract steps",
                      "Split process(order) into small functions (validate, total, format) WITHOUT changing behaviour; keep process(order) with the same signature.\n```python\ndef process(order):\n    if not order.get('items'):\n        raise ValueError('empty')\n    t = 0\n    for i in order['items']:\n        t += i['price'] * i['qty']\n    if order.get('coupon') == 'TEN':\n        t = t * 0.9\n    return 'Total: %.2f' % t\n```",
                      "assert process({'items':[{'price':2,'qty':3}]}) == 'Total: 6.00'\nassert process({'items':[{'price':10,'qty':1}],'coupon':'TEN'}) == 'Total: 9.00'\ntry:\n    process({'items':[]}); raise SystemExit('no error')\nexcept ValueError:\n    pass\nprint('ok')", min_funcs=4),
    ],
    "protocol": [
        mh.py_task("length-prefixed framing",
                   "Write a tiny network protocol: `encode_frame(payload: bytes) -> bytes` prepends a 4-byte big-endian length prefix to payload. `decode_frames(buf: bytes) -> tuple` returns (frames, leftover): every COMPLETE frame currently at the start of buf, as a list of payloads, and any trailing bytes that are not yet a complete frame (a real TCP stream can hand you zero, one, or several frames in one read, and can split a frame across two reads).",
                   "a = encode_frame(b'one'); frames, left = decode_frames(a)\n"
                   "assert frames == [b'one'] and left == b''\n"
                   "buf = encode_frame(b'one') + encode_frame(b'two'); frames, left = decode_frames(buf)\n"
                   "assert frames == [b'one', b'two'] and left == b''\n"
                   "whole = encode_frame(b'partial'); cut = len(whole) - 2\n"
                   "frames, left = decode_frames(whole[:cut])\n"
                   "assert frames == [] and left == whole[:cut]\n"
                   "frames, left = decode_frames(left + whole[cut:])\n"
                   "assert frames == [b'partial'] and left == b''\n"
                   "print('ok')"),
        mh.py_task("checksummed frame validation",
                   "Write `wrap(payload: bytes) -> bytes` that appends a 2-byte big-endian checksum (sum of the payload's bytes, mod 65536) after payload. Write `unwrap(frame: bytes) -> bytes` that recomputes the checksum over everything except the last 2 bytes, compares it to those last 2 bytes, returns the payload if it matches, and raises ValueError if it does not.",
                   "f = wrap(b'trust me')\n"
                   "assert unwrap(f) == b'trust me'\n"
                   "bad = bytearray(f); bad[-1] ^= 0xFF\n"
                   "try:\n    unwrap(bytes(bad)); raise SystemExit('expected ValueError')\n"
                   "except ValueError:\n    pass\n"
                   "print('ok')"),
    ],
    "iot": [
        mh.py_task("OTA update gate",
                   "Write `apply_ota_update(current_version: str, candidate: dict) -> dict`. candidate has keys 'version' (a dotted string like '1.2.3'), 'payload' (bytes) and 'sha256' (a lowercase hex string). Accept the update ONLY if candidate['version'] is strictly greater than current_version, compared as tuples of ints (not as strings), AND hashlib.sha256(candidate['payload']).hexdigest() equals candidate['sha256']. Return {'applied': bool, 'version': str, 'reason': str} and never raise for a malformed version string -- return applied False with a reason instead.",
                   "import hashlib\n"
                   "payload = b'firmware bytes'; good_hash = hashlib.sha256(payload).hexdigest()\n"
                   "r = apply_ota_update('1.2.0', {'version': '1.3.0', 'payload': payload, 'sha256': good_hash})\n"
                   "assert r['applied'] is True and r['version'] == '1.3.0'\n"
                   "r = apply_ota_update('2.0.0', {'version': '1.9.9', 'payload': payload, 'sha256': good_hash})\n"
                   "assert r['applied'] is False\n"
                   "r = apply_ota_update('1.0.0', {'version': '9.9.9', 'payload': payload, 'sha256': '0' * 64})\n"
                   "assert r['applied'] is False\n"
                   "print('ok')"),
        mh.py_task("freshest valid sensor reading",
                   "Write `read_sensor(samples: list, now: float, max_age_s: float, lo: float, hi: float) -> dict`. Each sample is {'t': float, 'v': float}. Among samples that are BOTH within max_age_s of now (0 <= now - t <= max_age_s) AND within [lo, hi], return the one with the largest t as {'ok': True, 'value': v, 'age_s': now - t}. If none qualify, return {'ok': False, 'reason': <a string>}.",
                   "r = read_sensor([{'t': 95.0, 'v': 999.0}, {'t': 90.0, 'v': 21.0}], now=100.0, max_age_s=20, lo=-20, hi=60)\n"
                   "assert r == {'ok': True, 'value': 21.0, 'age_s': 10.0}\n"
                   "r = read_sensor([{'t': 0.0, 'v': 21.0}], now=1000.0, max_age_s=10, lo=-20, hi=60)\n"
                   "assert r['ok'] is False\n"
                   "print('ok')"),
    ],
})

# ---------------------------------------------------------------------------------------------
# Actions the hub can launch
# ---------------------------------------------------------------------------------------------
mh.ACTIONS.clear()
mh.ACTIONS.update({
    "selftest": ["architect.py", "selftest"], "flows": ["architect.py", "flows"], "show_config": ["architect.py", "show-config"],
    "portfolio": ["architect.py", "portfolio"], "export_stats": ["arch_training.py", "--stats"], "export": ["arch_training.py"],
    "train_check": ["train_lora.py", "--check"],
})


def _validated_overrides(raw, valid_ids, existing):
    """raw: {id: {dotted_field: value, ...}}  (new, flat shape) -- validated field by field with arch_params,
    merged onto `existing` so unspecified fields are kept. Still accepts the old shape {id: {think, sampling:{...}}}
    for anything already saved, by treating its top-level keys as already-structured overrides (not re-validated)."""
    out = {}
    for rid, patch in (raw or {}).items():
        if rid not in valid_ids or not isinstance(patch, dict):
            continue
        base = dict(existing.get(rid) or {})
        flat_fields = {"think", "sampling.temperature", "sampling.top_p", "sampling.top_k", "sampling.repeat_penalty", "num_ctx", "num_predict", "seed", "retries", "system_extra"}
        if patch and all(k in flat_fields for k in patch):
            for field, value in patch.items():
                base = params.apply_override(base, field, value)
        elif patch:                                          # legacy nested shape, e.g. {"sampling": {"temperature": 0.3}}
            base = patch
        if base:
            out[rid] = base
    return out


def load_portfolio():
    p = core.load_json(PORTFOLIO_FILE)
    apps = p.get("apps") if isinstance(p.get("apps"), list) else []
    out = []
    for a in apps:
        if isinstance(a, dict) and a.get("name") and a.get("path"):
            a = dict(a)
            a["exists"] = os.path.isdir(a["path"])
            out.append(a)
    return out


def save_portfolio(apps):
    clean = []
    for a in apps or []:
        if not isinstance(a, dict) or not str(a.get("name", "")).strip() or not str(a.get("path", "")).strip():
            continue
        clean.append({k: (a.get(k) if k in ("interfaces", "depends_on") else str(a.get(k) or "").strip())
                      for k in ("name", "path", "stack", "test_command", "interfaces", "depends_on", "notes")})
        for k in ("interfaces", "depends_on"):
            v = clean[-1][k]
            clean[-1][k] = [x.strip() for x in (v.split(",") if isinstance(v, str) else (v or [])) if str(x).strip()]
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump({"apps": clean}, f, indent=2)
    return clean


def _flow_meta():
    return [{"id": f.id, "title": f.title, "blurb": f.blurb, "steps": f.steps, "produces": f.produces, "domains": f.domains,
             "roles": sorted({s["role"].rstrip("*") for s in f.steps if s["role"] != "-"} | set(getattr(f, "uses_roles", []) or ({"generator_ui", "generator_sql"} if f.id in ("build", "refactor") else set())))} for f in fl.FLOWS.values()]


def build_state(q=None):
    st = _base_state(q)
    cfg = core.deep_merge(core.DEFAULTS, mh.read_config())
    models = st.get("models", [])
    try:
        import arch_index
        st["embed_model"] = arch_index.status(None, cfg).get("model")        # "sqlite-fts5" when function-level retrieval is on (the old key name, kept for the page)
    except Exception:
        st["embed_model"] = None
    ranked, plan = roles.recommend(models)
    role_rows = []
    for rid, r in roles.ROLES.items():
        rec = ranked.get(rid, [])
        role_rows.append({"id": rid, "title": r["title"], "icon": r["icon"], "blurb": r["blurb"], "needs": r["needs"], "latency": r["latency"], "uses": r["uses"],
                          "assigned": cfg["roles"].get(rid, ""), "override": cfg["role_overrides"].get(rid, {}), "default_think": r["think"],
                          "recs": rec[:4], "suggested": plan["assignment"].get(rid), "group": r.get("group", "Core")})
    best_for = {}
    for rid, rec in ranked.items():
        if rec and rec[0]["score"] > 0:
            top = rec[0]["score"]
            for x in rec:
                if x["score"] > 0 and x["score"] >= top - 3:
                    best_for.setdefault(x["model"], []).append(rid)
    for m in models:
        m["best_for"] = best_for.get(m["name"], [])
    gaps = []
    for rr in role_rows:
        top = rr["recs"][0] if rr["recs"] else None
        if not top:
            gaps.append({"role": rr["id"], "why": "no usable model installed"})
        elif top["basis"] == "guess":
            gaps.append({"role": rr["id"], "why": "no measurements yet: run the Scoreboard tests to replace this guess"})
        elif top["score"] < 55:
            gaps.append({"role": rr["id"], "why": f"best installed model scores {top['score']}: consider a model stronger in " + ", ".join(sorted(rr["needs"], key=lambda c: -rr["needs"][c])[:2])})
    st.update(roles=role_rows, plan=plan, gaps=gaps, flows=_flow_meta(), portfolio=load_portfolio(), domains=fl.DOMAINS, templates=templates.listing(),
              addons=[{"id": k, "title": v[0], "files": sorted(v[1])} for k, v in templates.ADDONS.items()],
              arch_config=cfg, weights=mh.load_results().get("_weights") or dict(mh.DEFAULT_WEIGHTS),
              settings_schema=params.SETTINGS, run_options_schema=params.RUN_OPTIONS, override_schema=params.OVERRIDE_FIELDS,
              model_overrides=cfg.get("model_overrides", {}), presets=params.list_presets(), self_root=HERE, projects_dir=os.path.join(os.path.expanduser("~"), "ArchitectProjects"),
              task_templates=taskkit.all_templates(), task_groups=taskkit.GROUPS)
    try:
        import arch_cloud
        st["cloud"] = arch_cloud.status(core.load_config())
    except Exception:
        st["cloud"] = None
    return st


_base_state = mh.build_state
mh.build_state = build_state


def run_listing(root):
    d = os.path.join(root, ".arch", "runs")
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), reverse=True)[:25]:
            rp = os.path.join(d, n, "report.md")
            if not os.path.isfile(rp):
                continue
            try:
                rep = open(rp, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            calls = 0
            tp = os.path.join(d, n, "trace.jsonl")
            if os.path.isfile(tp):
                calls = sum(1 for l in open(tp, encoding="utf-8", errors="replace") if '"type": "call"' in l)
            out.append({"name": n, "flow": (re.search(r"- Flow: `(\w+)`", rep) or [None, ""])[1], "goal": (re.search(r"- Goal: (.*)", rep) or [None, ""])[1][:140],
                        "calls": calls, "changed": len(re.findall(r"`[^`]+` (?:applied)", rep)),
                        "state": "complete" if re.search(r"complete|fixed|written|passed checks", rep, re.I) and not re.search(r"NOT |FAILED|incomplete|Stopped", rep) else "open",
                        "report": rep[:9000]})
    return out


def trace_listing(root, run_name):
    """The REAL per-call record of one run, straight from the trace.jsonl the Engine itself wrote while the
    run happened (arch_core.Engine._trace) -- every line here is an actual Ollama call that actually
    completed, with the actual model, the actual measured tok/s and duration. Nothing here is reconstructed
    or guessed after the fact; this is proof a call happened, not a claim that one did."""
    p = os.path.join(root, ".arch", "runs", run_name, "trace.jsonl")
    if not os.path.isfile(p):
        return []
    out = []
    for line in open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "call":
            out.append({"id": rec.get("id"), "t": rec.get("t"), "role": rec.get("role"), "model": rec.get("model"), "label": rec.get("label"),
                        "secs": rec.get("secs"), "tok_s": rec.get("tok_s"), "gen_tokens": rec.get("gen_tokens"), "done_reason": rec.get("done_reason"),
                        "prompt_tokens": rec.get("prompt_tokens"), "cached": rec.get("prefix_reused_est"), "exact": rec.get("cached_exact"),
                        "tier": rec.get("tier"), "transport": rec.get("transport"), "usd": rec.get("usd")})
    return out


def trace_economics(root, run_name):
    """Everything the run timeline needs besides the calls: what the checks said about each call (marks), the interface-wiring help
    (assists), the step checks, escalations and the verdict. Never prompt or reply text. Capped."""
    p = os.path.join(root, ".arch", "runs", run_name, "trace.jsonl")
    out = {"marks": {}, "assists": {}, "checks": [], "escalations": [], "verdict": None}
    if not os.path.isfile(p):
        return out
    for line in open(p, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        t = rec.get("type")
        if t == "mark" and rec.get("id") is not None:
            out["marks"][str(rec["id"])] = {"accepted": rec.get("accepted"), "reason": str(rec.get("reason") or "")[:300]}
        elif t == "assist" and rec.get("id") is not None:
            out["assists"][str(rec["id"])] = [str(f.get("why") or f.get("file") or "")[:200] for f in (rec.get("fixes") or [])][:6]
        elif t == "check" and len(out["checks"]) < 400:
            out["checks"].append({"step": rec.get("step"), "gate": rec.get("gate"), "ok": rec.get("ok"), "text": str(rec.get("text") or "")[:200]})
        elif t == "escalation" and len(out["escalations"]) < 50:
            out["escalations"].append({"step": rec.get("step"), "role": rec.get("role"), "to": rec.get("to") or rec.get("model")})
        elif t == "verdict":
            out["verdict"] = {"verdict": rec.get("verdict"), "why": str(rec.get("why") or rec.get("reason") or "")[:300]}
        elif t == "broker" and len(out.setdefault("broker", [])) < 20:
            out["broker"].append({k: rec.get(k) for k in ("t", "flow", "family", "suggest", "current", "why", "sure", "C", "target")})
    out.setdefault("broker", [])
    return out


def safe_root(p, create_ok=False):
    p = os.path.abspath(os.path.expanduser(str(p or "")))
    if not os.path.isdir(p):
        if create_ok and os.path.isdir(os.path.dirname(p)):
            return p                                   # a new project: the Architect creates this folder
        raise ValueError(f"Folder not found: {p}" + (f" (and its parent {os.path.dirname(p)} does not exist either)" if create_ok else ""))
    return p


TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


def tune_info():
    """Everything the Health > Tune page shows: tune status and profile, the last Get-ready result, and models worth adding (never downloads)."""
    import arch_autotune as at
    import arch_hw
    import arch_modelopt
    import arch_ready
    at._wire()
    hw = arch_hw.detect()
    st = at.status(hw)
    cfg = mh.read_config()
    try:
        rows = mh.model_list(hw, "f16", 192.0, 50.0)
        cat = at.suggest(hw, rows, cfg, st.get("profile"))
        own = at.owned_models(rows, cfg, st.get("profile"))
        opt = arch_modelopt.plan(hw, rows, cfg, st.get("profile"))
        try:  # passport family line + explain for the Models table; absent passports are simply skipped
            import arch_passport
            for o in own:
                pp = arch_passport.load(o.get("name", "")) if isinstance(o, dict) else None
                if isinstance(pp, dict):
                    o["passport"] = {k: pp.get(k) for k in ("identity", "family_line", "explain") if pp.get(k) is not None}
        except Exception:
            pass
    except mh.OllamaError as e:
        own, cat, opt = [], {"as_of": None, "machine": {}, "suggestions": [], "error": e.msg}, None
    fit, index = None, None
    try:
        import arch_chain
        import arch_index
        import arch_tune
        rows_fit = rows if opt is not None else []
        if rows_fit:
            fit = arch_chain.fit_sheet(cfg, hw, rows_fit, arch_tune.load_stats(), {})
        index = arch_index.status(None, cfg, [r["name"] for r in rows_fit])
    except Exception as e:                                         # advisory cards: never take the page down
        fit, index = None, {"state": f"unavailable ({e})", "suggest": None}
    return {"status": {k: st[k] for k in ("tuned", "needs_tune", "reason")}, "profile": st.get("profile"), "ready": arch_ready.load_ready(), "fit": fit, "index": index,
            "catalog": cat, "installed": [o["name"] for o in own], "models": own,
            "can_undo": bool((st.get("profile") or {}).get("undo")), "opt": opt,
            "opt_can_undo": bool(arch_modelopt._load().get("undo")), "machine_card": _read_text(os.path.join(core.data_dir(), "machine.md")),
            "machines": machines_info(cfg), "history": __import__("arch_ledger").roles_overview(), "livetest": core.load_json(os.path.join(core.data_dir(), "livetest.json")) or None}


def machines_info(cfg=None):
    """Other machines you added (probed with a short timeout), role routing, power state, and the plan. Read-only."""
    import arch_hosts as H
    import arch_power
    cfg = cfg or mh.read_config()
    probes = {n: H.probe(u, get=lambda url, timeout=2: H._get(url, timeout)) for n, u in H.hosts_of(cfg).items()}
    return {"hosts": [dict(name=n, url=u, **probes[n]) for n, u in H.hosts_of(cfg).items()], "role_hosts": cfg.get("role_hosts") or {},
            "plan": H.plan(cfg, [], probes), "power": arch_power.battery()}


# ---- First visit: the short guided path a person sees the first time they open the hub ----------------------------------------
# Everything here is read from what the app already knows (Doctor, Get ready, the tier picker). The one thing it adds is a practice
# project in Architect's OWN data folder, so the first task can never touch a folder of the person's.
TIER_WORDS = {
    "worker": "Writes and fixes the code. This is the one you will notice: it lives on the graphics card when it fits.",
    "router": "Reads your request and picks the workflow. A one-word answer, so a small model on the processor is enough.",
    "heavy": "Called only when the worker has failed a step. Bigger and slower, used sparingly.",
    "draft": "Optional. Guesses the worker's next words so it answers faster; must share the worker's vocabulary.",
}
PRACTICE_FILES = {
    "textkit.py": (
        '"""A tiny practice module. Architect fixes one small bug in it on your first visit."""\n'
        "import re\n\n\n"
        "def slugify(text):\n"
        "    \"\"\"'Hello World!' -> 'hello-world'\"\"\"\n"
        "    text = text.strip().lower()\n"
        "    text = re.sub(r\"[^a-z0-9\\s-]\", \"\", text)\n"
        "    return text.replace(\" \", \"-\")\n"),
    "test_textkit.py": (
        "from textkit import slugify\n\n\n"
        "def test_simple():\n"
        "    assert slugify(\"Hello World\") == \"hello-world\"\n\n\n"
        "def test_punctuation():\n"
        "    assert slugify(\"Hello, World!\") == \"hello-world\"\n\n\n"
        "def test_repeated_spaces():\n"
        "    assert slugify(\"Hello   World\") == \"hello-world\"\n"),
}
PRACTICE_GOAL = ("slugify('Hello   World') in textkit.py returns 'hello---world' instead of 'hello-world': "
                 "repeated spaces must become a single dash. Fix slugify so every test in test_textkit.py passes.")


def practice_root():
    return os.path.join(core.data_dir(), "practice", "textkit")


def practice_prepare(root=None):
    """(Re)write the two practice files, so every practice run starts from the same small bug. Only this folder is written."""
    root = root or practice_root()
    os.makedirs(root, exist_ok=True)
    for name, body in PRACTICE_FILES.items():
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
    return root


def first_visit_info(root=None, hub_port=8766, doctor_run=None, team_plan=None):
    """What the first-visit path shows in one reply: the machine checks (Doctor), whether Get ready has run, and which model would do
    which job, in plain words. Read-only. Each part fails on its own, with the reason, so one missing piece never blanks the path."""
    out = {"machine": None, "ready": None, "team": None, "practice": {"root": practice_root(), "goal": PRACTICE_GOAL,
                                                                      "files": sorted(PRACTICE_FILES)}}
    try:
        if doctor_run is None:
            import arch_doctor
            doctor_run = arch_doctor.run
        d = doctor_run(root or HERE, hub_port)
        checks = d.get("checks") or []
        out["machine"] = {"ok": bool(d.get("ok")), "checks": [{"name": c.get("name"), "ok": bool(c.get("ok")), "advisory": bool(c.get("advisory")),
                                                               "detail": str(c.get("detail") or "")[:240]} for c in checks],
                          "passed": sum(1 for c in checks if c.get("ok")), "total": len(checks),
                          "blocking": [c.get("name") for c in checks if not c.get("ok") and not c.get("advisory")]}
    except Exception as ex:
        out["machine"] = {"error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    try:
        import arch_ready
        r = arch_ready.load_ready() or {}
        steps = r.get("steps") or []
        out["ready"] = {"ran": bool(steps), "ok": bool(r.get("ok")) if steps else None,
                        "passed": sum(1 for s in steps if s.get("ok")), "total": len(steps)}
    except Exception as ex:
        out["ready"] = {"ran": False, "ok": None, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    try:
        if team_plan is None:
            import arch_port
            team_plan = arch_port.pick_tiers(say=lambda m: None)
        cfg_ = core.load_config()
        current = {t: (cfg_.get("tiers") or {}).get(t, "") for t in TIER_WORDS}
        tiers = []
        for t, words in TIER_WORDS.items():
            rec = (team_plan.get("tiers") or {}).get(t) or {}
            tiers.append({"tier": t, "what": words, "recommended": rec.get("model") or "", "current": current.get(t) or "",
                          "why": str(rec.get("why") or "")[:300], "on_card": bool(rec.get("full_gpu")), "cpu": bool(rec.get("cpu"))})
        out["team"] = {"tiers": tiers, "first_run": not any(current.values()),
                       "has_worker": bool(current.get("worker") or ((team_plan.get("tiers") or {}).get("worker") or {}).get("model")),
                       "matches": all((x["current"] or "") == (x["recommended"] or "") for x in tiers),
                       "notes": [str(n)[:300] for n in (team_plan.get("notes") or [])][:4]}
    except Exception as ex:
        out["team"] = {"error": f"{type(ex).__name__}: {str(ex)[:200]}", "tiers": [], "has_worker": False}
    return out


def _read_text(path, limit=20000):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit)
    except OSError:
        return None


def code_health(root):
    """Fake-code findings and install integrity for the Health page (read-only)."""
    import arch_fakecode as fc
    import arch_scan
    import arch_selfimprove as si
    files = [rel for rel, _ in arch_scan.walk(root) if rel.endswith(".py")][:3000]
    found = fc.scan(root, files)
    shown = [x for x in found if x["gate"] or x["severity"] != "low"][:60]
    prof = core.load_json(os.path.join(root, ".arch", "profile.json"))
    import arch_ci
    import arch_resume
    cires = arch_ci.check_all(root)
    rz = arch_resume.resumable(root)
    return {"root": root, "fake": fc.summarize(found), "findings": shown, "profile": prof or None,
            "ci": {"summary": arch_ci.summarize(cires), "findings": cires["findings"][:12], "parser": cires["parser"]},
            "resumable": {"run_id": rz["run_id"], "flow": rz["flow"], "goal": rz.get("goal", ""), "status": rz["status"]} if rz else None,
            "self": {"verify": si.verify(), "history": si.history()[-8:]}}


# ---------------------------------------------------------------------------------------------
# Blueprints: a workflow drawn on the hub, saved as flows/<id>.yaml and judged by the SAME validator
# `architect.py flows --validate` uses. A file the validator refuses is removed again; nothing half-valid stays on disk.
# ---------------------------------------------------------------------------------------------
BLUEPRINT_KINDS = ("generate", "code", "ask_json", "judge", "doc", "sample_n", "cascade", "tool_loop")
BLUEPRINT_NEEDS_PROMPT = {"generate", "ask_json", "doc", "sample_n", "cascade", "tool_loop"}   # these read step["prompt"] when they run
BLUEPRINT_TIERS = ("router", "worker", "heavy")
BLUEPRINT_ID = re.compile(r"[a-z][a-z0-9_]{2,30}")
_BP_STEP_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
_BP_PROMPT = re.compile(r"[a-z0-9_-]{1,40}(/[a-z0-9_-]{1,60})?")


def _bp_text(s, n=120):
    """One line of text that both PyYAML and the built-in YAML reader read back unchanged inside double quotes."""
    s = re.sub(r"[\x00-\x1f\x7f\\]", " ", str(s or "")).replace('"', "'")
    return re.sub(r"\s+", " ", s).strip()[:n]


def _bp_builtin_ids(flows_dir):
    """Every workflow id that is not a blueprint someone drew here: the Python flows, plus YAML files without `blueprint: true`."""
    import arch_dataflow
    ids, drawn = set(fl.FLOWS), set()
    for d in {arch_dataflow.FLOWS_DIR, flows_dir}:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".yaml"):
                (drawn if _bp_is_blueprint(os.path.join(d, f)) else ids).add(f[:-5])
    # a blueprint saved earlier is registered in fl.FLOWS like any YAML flow once the app restarts; it stays the person's own,
    # unless a Python flow class has the same id (that one is built in, whatever a YAML file claims)
    py_ids = {k for k, v in fl.FLOWS.items() if type(v).__module__ != "arch_dataflow"}
    return ids - (drawn - py_ids)


def _bp_is_blueprint(path):
    import arch_dataflow
    try:
        doc = arch_dataflow.load_yaml_file(path)
    except Exception:
        return False
    return isinstance(doc, dict) and doc.get("blueprint") is True


def blueprint_kit(flow=None):
    """What the Blueprints page may offer: step kinds, tiers, roles, the registered gates (with their first doc line), the prompt files
    on disk, and which ids are taken. With `flow`, also each step's prompt as `flow/name`, so a loaded workflow keeps its prompts."""
    import arch_dataflow
    import arch_gates
    gates = []
    for name in sorted(arch_gates.GATES):
        doc = (getattr(arch_gates.GATES[name], "__doc__", None) or "").strip().split("\n")[0][:160]
        gates.append({"name": name, "doc": doc})
    prompts = []
    if os.path.isdir(arch_dataflow.PROMPTS_DIR):
        for d in sorted(os.listdir(arch_dataflow.PROMPTS_DIR)):
            sub = os.path.join(arch_dataflow.PROMPTS_DIR, d)
            if os.path.isdir(sub):
                prompts += [f"{d}/{f[:-3]}" for f in sorted(os.listdir(sub)) if f.endswith(".md")]
    out = {"kinds": list(BLUEPRINT_KINDS), "needs_prompt": sorted(BLUEPRINT_NEEDS_PROMPT), "tiers": list(BLUEPRINT_TIERS),
           "roles": sorted(roles.ROLES), "gates": gates, "prompts": prompts, "taken": sorted(_bp_builtin_ids(arch_dataflow.FLOWS_DIR)),
           "id_pattern": BLUEPRINT_ID.pattern}
    if flow and re.fullmatch(r"[a-z0-9_]{1,40}", flow):
        path = os.path.join(arch_dataflow.FLOWS_DIR, f"{flow}.yaml")
        try:
            doc = arch_dataflow.load_yaml_file(path) if os.path.isfile(path) else {}
        except Exception:
            doc = {}
        pof = {}
        for st in (doc.get("steps") or []) if isinstance(doc, dict) else []:
            if isinstance(st, dict) and st.get("id") and isinstance(st.get("prompt"), str):
                p = st["prompt"][:-3] if st["prompt"].endswith(".md") else st["prompt"]
                pof[st["id"]] = p if "/" in p else f"{flow}/{p}"
        out["prompts_of"] = pof
        out["blueprint"] = bool(isinstance(doc, dict) and doc.get("blueprint") is True)
    return out


def _bp_steps(raw):
    """The steps as sent by the page -> clean dicts. Anything structurally wrong is refused here (ValueError); whether the workflow makes
    sense is the validator's call, not this function's."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("A blueprint needs at least one step.")
    if len(raw) > 30:
        raise ValueError("At most 30 steps in one blueprint.")
    out, seen = [], set()
    for n, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise ValueError(f"step {n} is not an object")
        kind = str(s.get("kind") or "generate")
        if kind not in BLUEPRINT_KINDS:
            raise ValueError(f"step {n}: '{kind}' is not a step kind a blueprint can hold ({', '.join(BLUEPRINT_KINDS)})")
        sid = str(s.get("id") or "").strip() or f"step{n}"
        if not _BP_STEP_ID.fullmatch(sid):
            raise ValueError(f"step {n}: the id '{sid[:40]}' must be lower-case letters, digits and _ (starting with a letter)")
        if sid in seen:                                     # left to the validator would also work; a clear message here is kinder
            raise ValueError(f"step {n}: the id '{sid}' is used twice")
        seen.add(sid)
        tier = str(s.get("tier") or "")
        if tier and tier not in BLUEPRINT_TIERS:
            raise ValueError(f"step {n}: tier must be one of {', '.join(BLUEPRINT_TIERS)}")
        role = str(s.get("role") or "")
        if role and role not in roles.ROLES:
            raise ValueError(f"step {n}: '{role[:40]}' is not a role")
        prompt = str(s.get("prompt") or "").strip()
        if prompt.endswith(".md"):
            prompt = prompt[:-3]
        if prompt and not _BP_PROMPT.fullmatch(prompt):
            raise ValueError(f"step {n}: a prompt is named like review/review (folder/file under prompts/)")
        check = str(s.get("check") or "").strip()
        if check and not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", check):
            raise ValueError(f"step {n}: '{check[:40]}' is not a check name")
        try:
            retries = max(0, min(5, int(s.get("retries") or 0)))
        except (TypeError, ValueError):
            raise ValueError(f"step {n}: retries must be a whole number from 0 to 5")
        out.append({"id": sid, "kind": kind, "title": _bp_text(s.get("title")) or sid, "tier": tier, "role": role, "prompt": prompt,
                    "check": check, "retries": retries, "when": _bp_text(s.get("when"), 300)})
    return out


def blueprint_yaml(fid, title, steps, blurb="", budget=None):
    """The flow document as text, in the subset of YAML the built-in reader understands (so it also loads without PyYAML)."""
    b = budget or {}
    lines = [f"id: {fid}", f'title: "{_bp_text(title) or fid}"', "kind: direct",
             f'blurb: "{_bp_text(blurb, 300) or "Drawn on the Blueprints page of the hub."}"', "blueprint: true",
             "budget: {calls: %d, tokens: %d, seconds: %d}" % (b.get("calls", 40), b.get("tokens", 200000), b.get("seconds", 1800)), "steps:"]
    for s in steps:
        lines += ["", f"  - id: {s['id']}", f"    kind: {s['kind']}", f'    title: "{s["title"]}"']
        if s["kind"] != "code":
            if s["tier"]:
                lines.append(f"    tier: {s['tier']}")
            if s["role"]:
                lines.append(f"    role: {s['role']}")
            if s["prompt"]:
                lines.append(f'    prompt: "{s["prompt"]}"')
        if s["when"]:
            lines.append(f'    when: "{s["when"]}"')
        if s["kind"] == "code":
            lines.append(f"    gate: {s['check'] or 'none'}")
        elif s["kind"] != "judge" and s["check"]:
            lines.append("    verify: {run_tests: true}" if s["check"] == "tests" else f"    verify: {{gate: {s['check']}}}")
        if s["retries"] and s["kind"] not in ("code", "judge"):
            lines.append(f"    retries: {s['retries']}")
    lines += ["", "verdict:", '  pass_if: "' + " and ".join(f"{s['id']}.ok" for s in steps) + '"', ""]
    return "\n".join(lines)


def save_blueprint(b, flows_dir=None):
    """POST /api/arch/flow_save. {id, title, steps, blurb?, budget?, overwrite?, dry?} -> {ok, id, path, problems, validator, yaml}.

    Refused outright (ValueError, HTTP 400): an id outside [a-z][a-z0-9_]{2,30}, the id of a built-in workflow, or a blueprint that
    already exists unless `overwrite` is true. Otherwise the file is written, read back and validated by arch_dataflow.validate; when
    that finds problems the file is deleted (an overwritten blueprint is put back as it was) and the problems are returned with ok False.
    `dry` validates the same text without writing anything."""
    import arch_dataflow
    flows_dir = flows_dir or arch_dataflow.FLOWS_DIR
    fid = str(b.get("id") or "").strip()
    if not BLUEPRINT_ID.fullmatch(fid):
        raise ValueError("A blueprint id is 3 to 31 characters: a lower-case letter first, then lower-case letters, digits or _.")
    if fid in _bp_builtin_ids(flows_dir):
        raise ValueError(f"'{fid}' is a built-in workflow. Pick a new id; the built-in ones are never overwritten from the hub.")
    steps = _bp_steps(b.get("steps"))
    budget = {}
    for k, lo, hi in (("calls", 1, 400), ("tokens", 1000, 5_000_000), ("seconds", 30, 86_400)):
        v = (b.get("budget") or {}).get(k) if isinstance(b.get("budget"), dict) else None
        if v not in (None, ""):
            try:
                budget[k] = max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                raise ValueError(f"budget.{k} must be a whole number")
    text = blueprint_yaml(fid, b.get("title") or fid, steps, b.get("blurb") or "", budget)
    path = os.path.join(flows_dir, f"{fid}.yaml")
    rel = f"flows/{fid}.yaml"

    def judge(doc):
        probs = list(arch_dataflow.validate(doc))
        own = []
        for s in arch_dataflow.validate_steps(doc if isinstance(doc, dict) else {}):
            if s.get("kind") in BLUEPRINT_NEEDS_PROMPT and not s.get("prompt"):
                own.append(f"step {s.get('id')}: a {s.get('kind')} step talks to a model, so it needs a prompt file (prompts/<folder>/<name>.md)")
        return probs, own

    if b.get("dry"):
        import arch_ci
        try:
            doc = arch_ci.load_yaml(text)
        except Exception as ex:
            return {"ok": False, "id": fid, "path": rel, "dry": True, "problems": [f"the file would not read back: {ex}"], "validator": [], "yaml": text}
        probs, own = judge(doc)
        return {"ok": not (probs or own), "id": fid, "path": rel, "dry": True, "problems": probs + own, "validator": probs, "yaml": text}
    old = None
    if os.path.exists(path):
        if b.get("overwrite") is not True:
            raise ValueError(f"You already have a blueprint called '{fid}'. Tick 'Replace my blueprint' to save over it, or pick a new id.")
        with open(path, encoding="utf-8") as f:
            old = f.read()
    os.makedirs(flows_dir, exist_ok=True)
    with open(path, "w" if old is not None else "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        probs, own = judge(arch_dataflow.load_yaml_file(path))
    except Exception as ex:
        probs, own = [f"the file would not read back: {type(ex).__name__}: {ex}"], []
    if probs or own:
        if old is not None:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(old)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
        return {"ok": False, "id": fid, "path": rel, "problems": probs + own, "validator": probs, "kept": old is not None, "yaml": text}
    live = False
    if os.path.normcase(os.path.abspath(flows_dir)) == os.path.normcase(os.path.abspath(arch_dataflow.FLOWS_DIR)):
        try:                                                 # runnable from this hub now, the same way load_all registers it at start
            if ((core.load_config().get("flows") or {}).get("engine") or "legacy") != "off":
                fl.FLOWS[fid] = arch_dataflow.DataFlow(arch_dataflow.load_yaml_file(path), path)
                live = True
        except Exception:
            live = False
    return {"ok": True, "id": fid, "path": rel, "problems": [], "validator": [], "replaced": old is not None, "live": live, "yaml": text}


# ---------------------------------------------------------------------------------------------
# Crew: the week's cost and the agent schedule. Both come from modules that may not be in this install yet, so every call is guarded:
# the hub starts and the page says what is missing instead of failing.
# ---------------------------------------------------------------------------------------------
def costs_summary(days=7):
    try:
        import arch_costs
        out = arch_costs.summary(days=days)
    except Exception as ex:                                  # missing module, missing function, or its own error
        return {"available": False, "days": days, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    if not isinstance(out, dict):
        return {"available": False, "days": days, "error": "arch_costs.summary did not return a dict"}
    return dict(out, available=True)


def crew_schedule_list():
    try:
        import arch_agents
        items = arch_agents.schedule_list()
    except Exception as ex:
        return {"available": False, "schedules": [], "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    if isinstance(items, dict):
        items = items.get("schedules") or items.get("items") or []
    return {"available": True, "schedules": list(items or [])}


def crew_schedule_change(b):
    """POST /api/arch/crew_schedule: {add: {agent_id, message, at, repeat, environment_id?, repo_url?}} or {remove: sid}."""
    try:
        import arch_agents
    except Exception as ex:
        raise ValueError(f"Scheduling is not available in this install ({type(ex).__name__}).")
    if b.get("remove") is not None:
        sid = str(b["remove"])
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", sid):
            raise ValueError("that is not a schedule id")
        if not hasattr(arch_agents, "schedule_remove"):
            raise ValueError("Scheduling is not available in this install yet (arch_agents.schedule_remove is missing).")
        res = arch_agents.schedule_remove(sid)
        return dict(crew_schedule_list(), ok=res is not False, removed=sid)
    a = b.get("add")
    if not isinstance(a, dict):
        raise ValueError("say add or remove")
    aid = str(a.get("agent_id") or "")
    if not re.fullmatch(r"agent_[A-Za-z0-9]{6,64}", aid):
        raise ValueError("Pick one of your agents.")
    msg = str(a.get("message") or "").strip()[:20000]
    if not msg:
        raise ValueError("Say what the agent should do.")
    at = str(a.get("at") or "")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("The time is HH:MM, for example 07:30.")
    repeat = str(a.get("repeat") or "daily")
    if repeat not in ("daily", "once"):
        raise ValueError("repeat is daily or once")
    env = str(a.get("environment_id") or "").strip() or None
    if env and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", env):
        raise ValueError("that is not an environment id")
    repo = str(a.get("repo_url") or "").strip() or None
    if repo and not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+(\.git)?", repo):
        raise ValueError("That is not a GitHub repository address.")
    if not hasattr(arch_agents, "schedule_add"):
        raise ValueError("Scheduling is not available in this install yet (arch_agents.schedule_add is missing).")
    made = arch_agents.schedule_add(aid, msg, at=at, repeat=repeat, environment_id=env, repo_url=repo)
    return dict(crew_schedule_list(), ok=True, added=made)


# ---------------------------------------------------------------------------------------------
# The agents' workspace: Fleet (several agents at once), Map (how the pieces of one run connect), Logbook (what every run taught us),
# Library (briefs that worked), Monitor (every agent on this machine, live) and the price of a Claude message before it is sent.
# Each backend module may be missing in an install, so every call goes through _guarded: the hub starts, the page says what is missing.
# ---------------------------------------------------------------------------------------------
def _plain(x):
    """Anything a module hands back -> plain JSON (sets, paths and odd objects become strings), so _send never fails on it."""
    return json.loads(json.dumps(x, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o)))


def _guarded(fn, *a, **kw):
    """-> (result, None) or (None, "Name: why") when the module or the call failed."""
    try:
        return fn(*a, **kw), None
    except Exception as ex:
        return None, f"{type(ex).__name__}: {str(ex)[:300]}"


def _mod(name):
    import importlib
    try:
        return importlib.import_module(name)
    except ImportError as ex:
        raise ValueError(f"{name}.py is not in this install ({type(ex).__name__})") from None


FLEET_CAP_MAX = 500.0


def fleet_plan_from(rows):
    """The Fleet page's table rows -> the plan arch_fleet.launch takes. Files may be one comma/newline separated string; a local agent's
    brief is its goal. Nothing is judged here: plan_check does that, so its plain words reach the page unchanged."""
    plan = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            plan.append(r)
            continue
        kind = str(r.get("kind") or "claude").strip()
        files = r.get("files")
        if isinstance(files, str):
            files = [f.strip() for f in re.split(r"[,\n]", files) if f.strip()]
        a = {"name": str(r.get("name") or "").strip()[:80], "kind": kind, "files": list(files or [])}
        brief = str(r.get("brief") or "").strip()[:20000]
        if kind == "local":
            a["goal"] = brief
        else:
            a["brief"] = brief
            a["mode"] = str(r.get("mode") or "acceptEdits")
        b = r.get("budget_usd", r.get("budget"))
        a["budget_usd"] = None if b in (None, "") else b
        for k in ("model", "after", "flow"):
            if r.get(k):
                a[k] = r[k]
        plan.append(a)
    return plan


def _fleet_cap(b):
    try:
        cap = float(b.get("cap", 20.0))
    except (TypeError, ValueError):
        raise ValueError("the spending cap must be a number")
    if not 0 < cap <= FLEET_CAP_MAX:
        raise ValueError(f"the spending cap must be above $0 and at most ${FLEET_CAP_MAX:.0f}")
    return cap


def _fleet_tests(b):
    t = b.get("tests")
    if isinstance(t, str):
        t = [x.strip() for x in t.splitlines() if x.strip()]
    return [str(x).strip() for x in (t or []) if str(x).strip()][:10]


def fleet_check(b):
    """POST /api/arch/fleet {check: rows, cap, tests} -> {problems, total, cap, agents}. Spends nothing."""
    fleet = _mod("arch_fleet")
    plan = fleet_plan_from(b.get("check") if "check" in b else b.get("launch"))
    cap = _fleet_cap(b)
    problems = list(fleet.plan_check(plan, cap))
    for c in _fleet_tests(b):
        p = fleet.command_problem(c)
        if p:
            problems.append(p)
    try:                                                  # a template's {slot} left in a brief would reach the agent as written
        import arch_agentlib
        for a in plan:
            if isinstance(a, dict):
                left = arch_agentlib.leftovers(" ".join([str(a.get("brief") or a.get("goal") or "")] + [str(f) for f in a.get("files") or []]))
                if left:
                    problems.append(f"{a.get('name') or 'an agent'}: fill in {', '.join(left)} first (left from the template)")
    except Exception:
        pass
    total = 0.0
    for a in plan:
        try:
            total += float((a or {}).get("budget_usd") or 0)
        except (TypeError, ValueError, AttributeError):
            pass
    return {"ok": not problems, "problems": problems, "total": round(total, 2), "cap": cap, "agents": len(plan)}


def fleet_info(fid=None):
    """GET /api/arch/fleet[?id=] -> the fleets so far, or one fleet's live status."""
    fleet, err = _guarded(_mod, "arch_fleet")
    if err:
        return {"available": False, "error": err, "fleets": []}
    if fid:
        st, err = _guarded(fleet.status, fid)
        return _plain(st) if not err else {"error": err}
    fleets, err = _guarded(fleet.list_fleets, 30)
    return _plain({"available": True, "fleets": fleets or [], "error": err, "kinds": list(getattr(fleet, "KINDS", ("claude", "local"))),
                   "max_agents": getattr(fleet, "MAX_AGENTS", 16), "allowed": list(getattr(fleet, "ALLOWED_PREFIXES", ())),
                   "default_tests": list(getattr(fleet, "DEFAULT_TESTS", ())), "cap_max": FLEET_CAP_MAX})


def fleet_change(b):
    fleet = _mod("arch_fleet")
    if b.get("stop"):
        return {"ok": bool(fleet.stop(str(b["stop"]))), "stopped": str(b["stop"])}
    if "check" in b:
        return fleet_check(b)
    if "launch" in b:
        chk = fleet_check(b)
        if chk["problems"]:
            raise ValueError("The plan was not launched: " + "; ".join(chk["problems"]))
        root = safe_root(b.get("root") or HERE)
        fid = fleet.launch(fleet_plan_from(b["launch"]), root, total_cap_usd=chk["cap"], tests=_fleet_tests(b))
        return {"ok": True, "id": fid}
    raise ValueError("say check, launch or stop")


# ---- the Map: one run, fleet, flow or Claude Code run as nodes and edges ----------------------------------------------------------
MAP_KINDS = ("run", "fleet", "flow", "cc")
MAP_MAX_NODES = 120



def ollama_unload_request(b):
    """POST /api/arch/ollama/unload {"model"}: unload one model of ours from Ollama (keep_alive 0). Another app's model only with an
    explicit {"all": true}, which then unloads every loaded model. -> {"ok", "unloaded": [names], "refused": [names]}"""
    import arch_ollama
    view = arch_ollama.loaded_view(core.load_config())
    loaded = {m["name"]: m for m in view["models"]}
    every = b.get("all") is True
    model = str(b.get("model") or "").strip()
    if not every and not model:
        raise ValueError("Name a model to unload (or send all: true).")
    targets = list(loaded) if every and not model else [model]
    gone, refused = [], []
    for name in targets:
        m = loaded.get(name)
        if not m:
            continue
        if not m["ours"] and not every:
            refused.append(name)
            continue
        if arch_ollama.unload(name):
            gone.append(name)
    if refused and not gone:
        raise ValueError(f"{', '.join(refused)} is not a model this app uses; send all: true to unload it anyway.")
    return {"ok": True, "unloaded": gone, "refused": refused}

class _Graph:
    def __init__(self, kind, ident, title):
        self.out = {"kind": kind, "id": ident, "title": title, "nodes": [], "edges": []}
        self._ids, self._edges = {}, set()

    def node(self, key, typ, label, sub="", **detail):
        if key in self._ids:
            n = self._ids[key]
            for k, v in detail.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(n["detail"].get(k), (int, float)) \
                        and not isinstance(n["detail"].get(k), bool) and k not in ("secs", "tries"):
                    n["detail"][k] = round(n["detail"][k] + v, 6)
                elif v not in (None, "", [], {}) or k == "ok":       # ok None = could not run: kept, never dropped
                    n["detail"][k] = v
            return key
        if len(self.out["nodes"]) >= MAP_MAX_NODES:
            return None
        n = {"id": key, "type": typ, "label": str(label or key)[:60], "sub": str(sub or "")[:80],
             "detail": {k: v for k, v in detail.items() if v not in (None, "", [], {}) or k == "ok"}}
        self._ids[key] = n
        self.out["nodes"].append(n)
        return key

    def edge(self, a, b, label=""):
        if a and b and a != b and (a, b) not in self._edges:
            self._edges.add((a, b))
            self.out["edges"].append({"from": a, "to": b, "label": str(label or "")[:30]})

    def result(self):
        self.out["truncated"] = len(self.out["nodes"]) >= MAP_MAX_NODES
        return _plain(self.out)


def _last_user(msgs):
    for m in reversed(msgs or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            return (c if isinstance(c, str) else json.dumps(c, default=str))[:1500]
    return ""


def _tier_label(tier, model):
    t = str(tier or "").lower()
    if str(model or "").startswith("claude") or t == "claude":
        return "claude", "Claude"
    return (t or "model"), {"router": "Router", "worker": "Worker", "heavy": "Heavy", "draft": "Draft"}.get(t, t.title() or "Model")


def map_run(root, run_name):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run_name or ""):
        raise ValueError("that is not a run id")
    rd = os.path.join(root, ".arch", "runs", run_name)
    if not os.path.isdir(rd):
        raise ValueError("no such run")
    rep = _read_text(os.path.join(rd, "report.md")) or ""
    goal = (re.search(r"- Goal: (.*)", rep) or [None, ""])[1].strip()
    flow = (re.search(r"- Flow: `(\w+)`", rep) or [None, ""])[1]
    g = _Graph("run", run_name, goal[:80] or run_name)
    req = g.node("req", "request", "Request", flow and f"flow: {flow}", prompt=goal or "(the goal is not in the report)", run=run_name)
    steps, cur, verdict = [], None, None

    def step_node(name):
        nonlocal cur
        key = "step:" + str(name)
        if key not in g._ids:
            if g.node(key, "step", name, "step") and key not in steps:
                steps.append(key)
        cur = key
        return key
    try:
        lines = open(os.path.join(rd, "trace.jsonl"), encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        t = r.get("type")
        if t == "step":
            s = step_node(r.get("step") or "step")
            if "ok" in r:
                g.node(s, "step", "", ok=r.get("ok"), unsure=r.get("unsure") or None, tries=r.get("tries"), secs=r.get("secs"))
                for f in r.get("changed") or []:
                    g.edge(s, g.node("file:" + str(f), "file", os.path.basename(str(f)), str(f), path=str(f)), "edited")
        elif t == "call":
            s = cur or step_node(r.get("label") or r.get("role") or "model call")
            tk, tl = _tier_label(r.get("tier"), r.get("model"))
            tn = g.node("tier:" + tk, "claude" if tk == "claude" else "tier", tl, r.get("model") or "", model=r.get("model"), calls=1,
                        tokens_in=int(r.get("prompt_tokens") or 0), tokens_out=int(r.get("gen_tokens") or 0), cost=float(r.get("usd") or 0))
            g.node(s, "step", "", role=r.get("role"), calls=1, tokens_in=int(r.get("prompt_tokens") or 0),
                   tokens_out=int(r.get("gen_tokens") or 0), cost=float(r.get("usd") or 0),
                   prompt=_last_user(r.get("messages")), reply=str(r.get("reply") or "")[:1500])
            g.edge(s, tn, r.get("role") or "")
        elif t == "tool":
            s = cur or step_node(r.get("step") or "tools")
            tn = g.node("tool:" + str(r.get("tool")), "tool", r.get("tool") or "tool", "tool", uses=1,
                        last_args=json.dumps(r.get("args") or {}, default=str)[:600], last_result=str(r.get("result") or "")[:800])
            g.edge(s, tn)
            args = r.get("args") or {}
            path = args.get("path") or args.get("file") if isinstance(args, dict) else None
            if path and r.get("tool") in ("apply", "edit_lines"):
                g.edge(s, g.node("file:" + str(path), "file", os.path.basename(str(path)), str(path), path=str(path)), "edited")
        elif t == "check":
            s = cur or step_node(r.get("step") or "checks")
            ck_ = g.node(f"check:{s}:{r.get('gate')}", "check", r.get("gate") or "check", "check", ok=r.get("ok"),
                         result=str(r.get("text") or "")[:1500], secs=r.get("secs"))
            g.edge(s, ck_)
        elif t == "verdict":
            verdict = r
    prev = req
    for s in steps:
        g.edge(prev, s, "then" if prev != req else "")
        prev = s
    if verdict or not steps:
        v = (verdict or {}).get("verdict") or ("pass" if re.search(r"passed checks|complete", rep, re.I) else "unknown")
        vn = g.node("verdict", "verdict", "Verdict: " + v, (verdict or {}).get("why") or "", result=(verdict or {}).get("why") or "",
                    verdict=v, meter=(verdict or {}).get("meter"))
        g.edge(prev, vn)
    return g.result()


def map_fleet(fid):
    fleet = _mod("arch_fleet")
    st = fleet.status(fid)
    if st.get("error") and not st.get("agents"):
        raise ValueError(st["error"])
    g = _Graph("fleet", fid, f"Fleet {fid}")
    req = g.node("req", "request", "Fleet", f"{len(st.get('agents') or [])} agents, cap ${st.get('cap_usd') or 0}",
                 root=st.get("root"), phase=st.get("phase"), cost=st.get("total_cost"))
    names = {}
    for a in st.get("agents") or []:
        an = g.node("agent:" + a["name"], "agent", a["name"], f"{a.get('kind') or 'claude'} · {a.get('status')}",
                    status=a.get("status"), cost=a.get("cost"), budget=a.get("budget_usd"), turns=a.get("turns"), error=a.get("error"),
                    last_event=(a.get("last_event") or {}).get("text") if isinstance(a.get("last_event"), dict) else a.get("last_event"),
                    run=a.get("run"), files=a.get("files"))
        names[a["name"]] = an
        after = a.get("after") or []
        for x in ([after] if isinstance(after, str) else after):
            g.edge(names.get(x), an, "after")
        if not after:
            g.edge(req, an)
        if (a.get("kind") or "claude") == "local":
            tn = g.node("tier:" + (a.get("lane") or "worker"), "tier", "Worker" if a.get("lane") != "cpu" else "Router", a.get("lane") or "")
        else:
            tn = g.node("tier:claude", "claude", "Claude", a.get("model") or "Claude Code", cost=float(a.get("cost") or 0))
        g.edge(an, tn)
        for f in a.get("files") or []:
            g.edge(an, g.node("file:" + str(f), "file", os.path.basename(str(f).rstrip("/")) or str(f), str(f), path=str(f)), "owns")
    tests = st.get("tests") or [{"cmd": c, "ok": None, "planned": True} for c in st.get("tests_planned") or []]
    vn = g.node("verdict", "verdict", "Verdict: " + str(st.get("verdict") or st.get("phase") or "running"), "",
                verdict=st.get("verdict"), diffstat=st.get("diffstat"), strays=st.get("strays"))
    for i, t in enumerate(tests):
        cmd = t.get("cmd") or t.get("command") or str(t)
        cn = g.node(f"check:{i}", "check", cmd[:40], "test", ok=t.get("ok"), result=str(t.get("output") or "")[-1500:], command=cmd)
        for an in names.values():
            g.edge(an, cn)
        g.edge(cn, vn)
    if not tests:
        for an in names.values():
            g.edge(an, vn)
    return g.result()


def map_flow(fid):
    if not re.fullmatch(r"[a-z0-9_]{1,40}", fid or ""):
        raise ValueError("flow must be a workflow id like bugfix")
    fm = _mod("arch_dataflow").flow_map(fid)
    if fm.get("error"):
        raise ValueError(fm["error"])
    g = _Graph("flow", fid, fm.get("title") or fid)
    prev = g.node("req", "request", "Request", fm.get("title") or fid, prompt=fm.get("blurb"), budget=fm.get("budget"))
    for s in fm.get("steps") or []:
        sn = g.node("step:" + str(s.get("id")), "step", s.get("title") or s.get("id"), s.get("kind") or "",
                    role=s.get("role"), budget=s.get("budget"), retries=s.get("retries"), when=s.get("when"))
        g.edge(prev, sn, "then" if prev != "req" else "")
        if s.get("tier") or s.get("model"):
            tk, tl = _tier_label(s.get("tier"), s.get("model"))
            g.edge(sn, g.node("tier:" + tk, "claude" if tk == "claude" else "tier", tl, s.get("model") or "", model=s.get("model")), s.get("role") or "")
        if s.get("check"):
            g.edge(sn, g.node(f"check:{s.get('id')}", "check", s["check"], "check", gate=s["check"]))
        prev = sn
    g.edge(prev, g.node("verdict", "verdict", "Verdict", "pass only when every check passed"))
    return g.result()


def _cc_file(ev):
    try:
        d = json.loads(ev.get("text") or "{}")
    except ValueError:
        return None
    return (d.get("file_path") or d.get("notebook_path") or d.get("path")) if isinstance(d, dict) else None


def map_cc(rid):
    d = _mod("arch_claudecode").load_run(rid)
    if d.get("error") and not d.get("events"):
        raise ValueError(d["error"])
    g = _Graph("cc", rid, (d.get("prompt") or rid)[:80])
    req = g.node("req", "request", "Request", d.get("mode") or "", prompt=str(d.get("prompt") or "")[:3000], root=d.get("root"))
    ag = g.node("agent", "agent", "Claude Code", str(d.get("status") or ""), status=d.get("status"), cost=d.get("cost"),
                turns=d.get("turns"), error=d.get("error"), session=d.get("session"))
    g.edge(req, ag)
    tn = g.node("tier:claude", "claude", "Claude", d.get("model") or "default model")
    tin = tout = 0
    for ev in d.get("events") or []:
        if ev.get("type") == "assistant" and isinstance(ev.get("usage"), dict):
            tin += int(ev["usage"].get("input_tokens") or 0)
            tout += int(ev["usage"].get("output_tokens") or 0)
        if ev.get("type") == "tool":
            name = "Agent" if ev.get("name") == "Task" else (ev.get("name") or "tool")
            t = g.node("tool:" + name, "tool", name, "tool", uses=1, last_args=str(ev.get("arg") or "")[:600])
            g.edge(ag, t)
            f = _cc_file(ev)
            if f and name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                g.edge(t, g.node("file:" + f, "file", os.path.basename(f), f, path=f), "edited")
    g.node("tier:claude", "claude", "", tokens_in=tin, tokens_out=tout, cost=float(d.get("cost") or 0))
    g.edge(ag, tn)
    final = next((e for e in reversed(d.get("events") or []) if e.get("type") == "result"), None)
    g.edge(ag, g.node("verdict", "verdict", "Result: " + str(d.get("status") or "?"), "", result=str((final or {}).get("text") or d.get("error") or "")[:2000]))
    return g.result()


def map_graph(kind, ident, root=None):
    """GET /api/arch/map -> {kind, id, title, nodes:[{id, type, label, sub, detail}], edges:[{from, to, label}]} or {error}.
    Node types: request, step, agent, tier, claude, tool, file, check, verdict. Read-only."""
    if kind not in MAP_KINDS:
        return {"error": "kind is run, fleet, flow or cc"}
    try:
        if kind == "run":
            return map_run(safe_root(root or HERE), ident)
        if kind == "fleet":
            return map_fleet(ident)
        if kind == "flow":
            return map_flow(ident)
        return map_cc(ident)
    except Exception as ex:
        return {"error": f"{str(ex)[:300]}" if isinstance(ex, ValueError) else f"{type(ex).__name__}: {str(ex)[:300]}"}


def map_sources(root=None):
    """What the Map can draw: the project's runs, the fleets, the workflows, the Claude Code runs."""
    out = {"runs": [], "fleets": [], "flows": [], "cc": []}
    try:
        r = safe_root(root or HERE)
        out["runs"] = [{"id": x["name"], "label": (x.get("goal") or x["name"])[:80], "flow": x.get("flow")} for x in run_listing(r)]
    except Exception:
        pass
    fls, _ = _guarded(lambda: _mod("arch_fleet").list_fleets(30))
    out["fleets"] = [{"id": f["id"], "label": f"{f['id']} · {f.get('agents')} agents · {f.get('phase')}"} for f in fls or []]
    try:
        out["flows"] = sorted(n[:-5] for n in os.listdir(os.path.join(HERE, "flows")) if n.endswith(".yaml"))
    except OSError:
        pass
    ccs, _ = _guarded(lambda: _mod("arch_claudecode").list_runs(30))
    out["cc"] = [{"id": c.get("id"), "label": str(c.get("prompt") or c.get("id"))[:80]} for c in ccs or []]
    return _plain(out)


# ---- the Logbook ------------------------------------------------------------------------------------------------------------------
def logbook_info(q=None):
    lb, err = _guarded(_mod, "arch_learn")
    if err:
        return {"available": False, "error": err}
    if q is not None:
        res, err = _guarded(lb.similar, str(q)[:500], 8)
        return _plain({"available": True, "similar": res or [], "error": err})
    st, err = _guarded(lb.stats)
    text = ""
    try:
        text = _read_text(lb.playbooks_path(), 60000) or ""
    except Exception:
        pass
    return _plain({"available": True, "stats": st or {}, "error": err, "playbooks": text})


def logbook_ingest():
    lb = _mod("arch_learn")
    res = lb.ingest()
    pb, err = _guarded(lb.playbooks)
    return _plain(dict(logbook_info(), ok=True, ingested=res, playbooks_error=err))


# ---- the Library ------------------------------------------------------------------------------------------------------------------
def library_info(name=None):
    lib, err = _guarded(_mod, "arch_agentlib")
    if err:
        return {"available": False, "error": err, "templates": []}
    if name:
        t, err = _guarded(lib.load, name)
        if err:
            return {"error": err.split(": ", 1)[-1]}
        ex, _ = _guarded(lib.explain, name)
        t = {k: v for k, v in t.items() if k != "path"}
        return _plain({"template": t, "explain": ex or ""})
    items, err = _guarded(lib.list_templates)
    method = _read_text(os.path.join(HERE, "docs", "agent_method.md"), 80000)
    return _plain({"available": True, "templates": items or [], "error": err, "method": method or None})


def library_fill(b):
    lib = _mod("arch_agentlib")
    name = str(b.get("fill") or "")
    vals = {str(k): str(v) for k, v in (b.get("values") or {}).items() if str(v).strip()}
    p = lib.fill_parts(name, **vals)
    if p.get("kind") != "agent":
        raise ValueError(f"{name} is a footer: it is added to other briefs, not run as an agent")
    tests = []
    try:
        import arch_fleet
        tests = [c for c in p.get("tests") or [] if arch_fleet.command_problem(c) is None]
    except Exception:
        pass
    return _plain({"ok": True, "row": {"name": p["name"].replace("_", "-"), "kind": "claude", "brief": p["brief"], "files": ", ".join(p["files"]),
                                       "budget": p["budget_usd"], "mode": p["mode"]}, "tests": tests})


# ---- the Monitor ------------------------------------------------------------------------------------------------------------------
MON_AGENT_KEYS = ("id", "name", "status", "cost", "turns", "tools_total", "edits", "errors", "refusals", "live", "quiet_s", "fleet",
                  "strays", "mode", "started", "ended", "error")


def _timeline_tail(path, n=150):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 96000))
            raw = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in raw[-n:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append({k: r.get(k) for k in ("t", "agent", "kind", "detail")})
    return out[::-1]


def monitor_info():
    ov, err = _guarded(_mod, "arch_overseer")
    if err:
        return {"available": False, "error": err}
    s, err = _guarded(ov.scan)
    if err or not isinstance(s, dict):
        s2, err2 = _guarded(ov.summary)
        s, err = (s2, err) if isinstance(s2, dict) else ({}, err or err2)
    agents = []
    for a in s.get("agents") or []:
        row = {k: a.get(k) for k in MON_AGENT_KEYS}
        row["files_edited"] = sorted((a.get("files_edited") or {}).keys()) if isinstance(a.get("files_edited"), dict) else list(a.get("files_edited") or [])
        row["tools"] = sorted((a.get("tool_calls") or {}).items(), key=lambda kv: -kv[1])[:6] if isinstance(a.get("tool_calls"), dict) else []
        row["stalls"] = len(a.get("stalls") or [])
        agents.append(row)
    les, lerr = _guarded(ov.lessons, None, False)
    tl = []
    try:
        tl = _timeline_tail(os.path.join(ov.overseer().out, "timeline.jsonl"))
    except Exception:
        pass
    return _plain({"available": True, "error": err, "agents": agents, "totals": s.get("totals") or {}, "conflicts": s.get("conflicts") or [],
                   "overlaps": s.get("overlaps") or [], "stalls": (s.get("stalls") or [])[:20], "top_tools": s.get("top_tools") or [],
                   "timeline": tl, "lessons": les or [], "lessons_error": lerr, "t": time.time()})


# ---- what a Claude message will cost, before it is sent --------------------------------------------------------------------------
CTX_TOKENS = {"small": 1000, "normal": 3500, "large": 15000, "max": 50000}      # the chat's context sizes (the hub's own labels)


def token_preview(b):
    """POST /api/arch/token_preview {model, message, ctx, council} -> {available, usd, line, ...}. Nothing is sent to a model."""
    model = str(b.get("model") or "").strip()
    if model.startswith("claude:"):
        model = model[len("claude:"):]
    if model and not re.fullmatch(r"claude-[a-z0-9.-]{3,40}", model):
        raise ValueError("unknown model")
    msg = str(b.get("message") or "")[:200000]
    tok, err = _guarded(_mod, "arch_tokens")
    if err:
        return {"available": False, "error": err}
    cfg_, _ = _guarded(core.load_config)                  # online off (the default) -> the offline chars/3.5 estimate, no request
    est, err = _guarded(tok.estimate_cost, model or None, "", [{"role": "user", "content": msg or " "}], cfg=cfg_ or {})
    if err:
        return {"available": False, "error": err}
    ctx_n = CTX_TOKENS.get(str(b.get("ctx") or "normal"), CTX_TOKENS["normal"])
    extra, _ = _guarded(lambda: _mod("arch_cloud").cost(model or est.get("model"), ctx_n, 0))
    usd = float(est.get("usd") or 0) + float(extra or 0)
    line = (f"about ${usd:.3f} ({int(est.get('input_tokens') or 0) + ctx_n:,} tokens in with ~{ctx_n:,} of project context, "
            f"~{int(est.get('expected_out') or 0):,} out; {'counted' if est.get('source') == 'counted' else 'estimate'})")
    if b.get("council"):
        line += " · the Council runs several agents: several times this"
    return _plain({"available": True, "usd": round(usd, 6), "line": line, "source": est.get("source"), "model": est.get("model"),
                   "input_tokens": est.get("input_tokens"), "context_tokens": ctx_n, "expected_out": est.get("expected_out")})


# ---- Crew builder and By-job model selection (arch_squad / arch_select / arch_jobs are optional: a missing module or function
# is reported as {"available": False, "why": ...}, never a crash)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.[A-Za-z0-9_.]{1,64}=.{0,400}$", re.S)


def _optional(modname, fn):
    """-> (callable, None) or (None, why) when the module or the function is not there yet."""
    import importlib
    try:
        m = importlib.import_module(modname)
    except Exception as ex:
        return None, f"{modname}.py is not available yet ({type(ex).__name__})"
    f = getattr(m, fn, None)
    return (f, None) if callable(f) else (None, f"{modname}.{fn}() is not available yet")


def squad_agents():
    """GET /api/arch/squad/agents -> {available, agents:[{name, description, model, role, effort, tools, budget, output, context}]}."""
    f, why = _optional("arch_squad", "list_agents")
    if not f:
        return {"available": False, "why": why, "agents": []}
    rows, err = _guarded(f)
    if err:
        return {"available": False, "why": err, "agents": []}
    keep = ("name", "description", "model", "role", "effort", "tools", "budget", "output", "context")
    items = rows.items() if isinstance(rows, dict) else ((None, r) for r in (rows or []))   # arch_squad returns {name: def}
    out = []
    for key, r in items:
        if not isinstance(r, dict):
            continue
        a = {k: r.get(k) for k in keep}
        a["name"] = a["name"] or key
        if r.get("problems"):
            a["problems"] = [str(x) for x in r["problems"]][:10]
        out.append(a)
    return {"available": True, "agents": out}


def _workflow_path(name):
    import arch_squad as sq
    d = getattr(sq, "WORKFLOWS_DIR", os.path.join(HERE, "workflows"))
    for ext in (".yaml", ".md"):             # workflows may live as .yaml or .md
        if os.path.isfile(os.path.join(d, name + ext)):
            return os.path.join(d, name + ext)
    return os.path.join(d, name + ".yaml")


def squad_workflows():
    """GET /api/arch/squad/workflows -> {available, workflows:[{name, description, phases}]}."""
    f, why = _optional("arch_squad", "list_workflows")
    if not f:
        return {"available": False, "why": why, "workflows": []}
    rows, err = _guarded(f)
    if err:
        return {"available": False, "why": err, "workflows": []}
    items = rows.items() if isinstance(rows, dict) else ((None, r) for r in (rows or []))   # {name: {name,title,description,phases,problems?}}
    out = []
    for key, r in items:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or key or "")
        if not _NAME_RE.match(name):
            continue
        w = {"name": name, "title": r.get("title") or name, "description": r.get("description") or "", "phases": list(r.get("phases") or [])}
        if r.get("problems"):                # shown as invalid in the Crew builder, not runnable
            w["problems"] = [str(x) for x in r["problems"]][:10]
        out.append(w)
    return {"available": True, "workflows": out}


def squad_save_agent(b):
    """POST /api/arch/squad/agent {name, spec} -> {ok} or 400 with the validation error."""
    name = str(b.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("An agent name is letters, digits, '-', '_' or '.', up to 64 characters.")
    spec = b.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object of agent fields.")
    f, why = _optional("arch_squad", "save_agent")
    if not f:
        raise ValueError(why)
    res = f(name, spec)                     # ValueError (invalid spec) reaches the page as a 400 with its message
    return {"ok": True, "name": name, "result": res if isinstance(res, (dict, list, str, int, float, bool)) or res is None else str(res)}


def squad_run(b):
    """POST /api/arch/squad/run {workflow, goal, owns, slots, fake, sets} -> starts `architect.py crew` as the hub job."""
    wf = str(b.get("workflow") or "").strip()
    if not _NAME_RE.match(wf):
        raise ValueError("Pick a workflow.")
    goal = str(b.get("goal") or "").strip()
    if not goal:
        raise ValueError("Type a goal first.")
    if len(goal) > 4000:
        raise ValueError("The goal is too long (4000 characters at most).")
    try:
        path = _workflow_path(wf)
    except ImportError:
        raise ValueError("arch_squad.py is not available yet")
    if not os.path.isfile(path):
        raise ValueError(f"No workflow named '{wf}'.")
    argv = ["architect.py", "crew", path, "--goal", goal]
    owns = b.get("owns") or []
    if isinstance(owns, str):
        owns = [x for x in re.split(r"[,\n]", owns)]
    owns = [str(x).strip() for x in owns if str(x).strip()]
    for o in owns:
        if len(o) > 260 or ".." in o.replace("\\", "/").split("/") or "," in o:
            raise ValueError(f"'{o[:60]}' is not a file path inside the project.")
    if owns:
        argv += ["--owns", ",".join(owns)]
    if b.get("slots") not in (None, ""):
        try:
            slots = int(b["slots"])
        except (TypeError, ValueError):
            raise ValueError("slots must be a whole number.")
        if not 1 <= slots <= 8:
            raise ValueError("slots must be between 1 and 8.")
        argv += ["--slots", str(slots)]
    if b.get("fake"):
        argv.append("--fake")
    sets = [str(s).strip() for s in (b.get("sets") or []) if str(s).strip()]
    for s in sets:
        if not _SET_RE.match(s):
            raise ValueError(f"'{s[:60]}' is not agent.field=value.")
    ps, _ = _optional("arch_squad", "parse_sets")
    if ps and sets:
        ps(sets)                            # its ValueError names the bad override
    for s in sets:
        argv += ["--set", s]
    mh.JOB.start("Crew: " + wf + ": " + goal[:50], argv)
    return {"ok": True, "argv": argv[1:]}


# ---- Review screen, editable plan and session panel. All of it is read from the run's own files (.arch/runs/<id>/trace.jsonl, run.json);
# a record or module that is missing leaves its part empty, never an error.
_RUN_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
_TESTLIKE = re.compile(r"test|pytest|unittest|spec|contract|smoke|syntax|compile|lint|build", re.I)


def _run_dir_of(root, run):
    base = os.path.join(root, ".arch", "runs")
    run = str(run or "").strip()
    if not run:                                              # newest run that has a trace
        names = sorted(os.listdir(base), reverse=True) if os.path.isdir(base) else []
        run = next((n for n in names if os.path.isfile(os.path.join(base, n, "trace.jsonl"))), names[0] if names else "")
    if not run:
        return "", ""
    if not _RUN_RE.fullmatch(run):
        raise ValueError("that is not a run id")
    return run, os.path.join(base, run)


def _trace_records(d, cap=20000):
    out = []
    try:
        with open(os.path.join(d, "trace.jsonl"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(out) >= cap:
                    break
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if isinstance(j, dict):
                    out.append(j)
    except OSError:
        pass
    return out


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def _git_diff(root, files, cap=20000):
    """Read-only `git diff` for these files; {} when git or a repository is missing."""
    import subprocess
    out = {}
    for rel in files[:40]:
        if ".." in rel.replace("\\", "/").split("/") or os.path.isabs(rel):
            continue
        try:
            r = subprocess.run(["git", "-C", root, "diff", "--no-color", "-U3", "--", rel], capture_output=True, text=True, timeout=8,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0 and r.stdout.strip():
                out[rel] = r.stdout[:cap]
        except (OSError, subprocess.SubprocessError):
            break
    return out


def _diff_counts(text):
    a = sum(1 for l in text.splitlines() if l.startswith("+") and not l.startswith("+++"))
    r = sum(1 for l in text.splitlines() if l.startswith("-") and not l.startswith("---"))
    return a, r


def review_view(root, run):
    """GET /api/arch/review?root=&run= -> files with diffs (trace `diff` records, else git diff), tests, verdict, cost, plan."""
    run, d = _run_dir_of(root, run)
    if not run or not os.path.isdir(d):
        return {"run": "", "empty": True}
    recs = _trace_records(d)
    st = _read_json(os.path.join(d, "run.json"))
    files, order = {}, []
    for r in recs:
        if r.get("type") == "diff" and r.get("file"):
            f = str(r["file"])
            if f not in files:
                order.append(f)
            files[f] = {"file": f, "added": int(r.get("added") or 0), "removed": int(r.get("removed") or 0), "new_file": bool(r.get("new_file")),
                        "diff": str(r.get("diff") or ""), "truncated": bool(r.get("truncated")), "source": "trace"}
    changed = []
    for r in recs:
        if r.get("type") == "step":
            for f in r.get("changed") or []:
                if isinstance(f, str) and f not in files and f not in changed:
                    changed.append(f)
    gd = _git_diff(root, changed) if changed else {}
    for f in changed:
        t = gd.get(f, "")
        a, rm = _diff_counts(t)
        files[f] = {"file": f, "added": a, "removed": rm, "new_file": False, "diff": t, "truncated": False, "source": "git" if t else "none"}
        order.append(f)
    checks = [r for r in recs if r.get("type") == "check" and r.get("ok") is not None and r.get("gate")]
    tests = [{"name": str(c["gate"])[:80], "ok": bool(c["ok"]), "text": str(c.get("text") or "")[:300], "step": c.get("step")} for c in checks]
    for t in tests:
        t["test"] = bool(_TESTLIKE.search(t["name"]))
    verdict = next(({"verdict": r.get("verdict"), "why": str(r.get("why") or "")[:400]} for r in reversed(recs) if r.get("type") == "verdict"), None)
    calls = [r for r in recs if r.get("type") == "call"]
    usd = sum(float(c.get("usd") or 0) for c in calls)
    todo = next((r.get("items") for r in reversed(recs) if r.get("type") == "todo"), None) or st.get("todo") or []
    accepted = _read_json(os.path.join(d, "accepted.json"))
    _attach_hunks(root, d, [files[f] for f in order][:60])
    return {"run": run, "goal": st.get("goal") or "", "flow": st.get("flow") or "", "status": st.get("status") or "", "verdict": verdict,
            "files": [files[f] for f in order][:60], "tests": tests[:80],
            "cost": {"usd": round(usd, 4), "calls": len(calls), "secs": round(sum(float(c.get("secs") or 0) for c in calls), 1),
                     "local": usd == 0, "models": sorted({str(c.get("model")) for c in calls if c.get("model")})[:6]},
            "plan": todo, "accepted": bool(accepted.get("t")), "root": root}


def _attach_hunks(root, d, files):
    """Adds the Keep / Drop hunk lists to review_view's file entries (only where a copy from before the run exists)."""
    for f in files:
        try:
            h = hunks_for(root, d, f["file"])
        except Exception as ex:
            h = {"splittable": False, "why": f"{type(ex).__name__}: {str(ex)[:120]}", "hunks": []}
        f["splittable"], f["split_why"] = h["splittable"], h["why"]
        if h["splittable"]:
            f["hunks"] = h["hunks"]


def review_decide(b):
    """POST /api/arch/review/accept {root, run}: marks the run accepted (a note beside the run; nothing else changes)."""
    root = safe_root(b.get("root") or HERE)
    run, d = _run_dir_of(root, b.get("run"))
    if not run or not os.path.isdir(d):
        raise ValueError("there is no run by that name")
    with open(os.path.join(d, "accepted.json"), "w", encoding="utf-8") as f:
        json.dump({"t": time.time()}, f)
    return {"ok": True, "run": run}


def _plan_hook():
    return hasattr(core.Engine, "plan_override")


def plan_view(root, run):
    """GET /api/arch/plan -> the run's plan as a list; editable only when the engine reads an override."""
    run, d = _run_dir_of(root, run)
    if not run:
        return {"run": "", "items": [], "editable": False}
    ok = _plan_hook()
    return {"run": run, "items": review_view(root, run).get("plan") or [], "editable": ok,
            "why": "" if ok else "Edit is not available yet: the engine does not read a plan override (needs Engine.plan_override reading <run>/plan_override.json)."}


def plan_save(b):
    """POST /api/arch/plan {root, run, items:[{id,title,status}]}: writes <run>/plan_override.json when the engine reads it."""
    root = safe_root(b.get("root") or HERE)
    run, d = _run_dir_of(root, b.get("run"))
    if not run or not os.path.isdir(d):
        raise ValueError("there is no run by that name")
    items = []
    for i, it in enumerate(b.get("items") or []):
        if isinstance(it, dict) and str(it.get("title") or "").strip():
            items.append({"id": str(it.get("id") or f"u{i}")[:40], "title": str(it["title"]).strip()[:120],
                          "status": "skipped" if it.get("status") == "skipped" else "pending"})
    if not items:
        raise ValueError("the plan needs at least one step")
    if not _plan_hook():
        return {"ok": False, "supported": False, "why": "Edit is not available yet: the engine does not read a plan override."}
    with open(os.path.join(d, "plan_override.json"), "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "items": items}, f)
    return {"ok": True, "supported": True, "run": run, "items": items}


# ---- Per-hunk Keep / Drop in Review. The hunks are always computed from the copy of the file BEFORE the run against the file as the run
# left it, so their ids stay the same however often the person changes their mind. Without a pre-run copy there is nothing to rebuild from.
def _rel_safe(root, rel):
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/") or rel.startswith(".arch/") or re.match(r"^[A-Za-z]:", rel):
        raise ValueError("that is not a file inside the project")
    full = os.path.realpath(os.path.join(root, rel))
    if not (full + os.sep).startswith(os.path.realpath(root) + os.sep):
        raise ValueError("that is not a file inside the project")
    return rel, full


def _prerun_bytes(root, d, rel):
    """-> (bytes | None, kind, why). kind: backup | new | checkpoint | none. `new` = the run created the file (pre-run content is empty)."""
    bk = os.path.join(d, "backup", *rel.split("/"))
    if os.path.isfile(bk):
        with open(bk, "rb") as f:
            return f.read(), "backup", ""
    try:
        with open(os.path.join(d, "created.txt"), encoding="utf-8") as f:
            if rel in {x.strip().replace("\\", "/") for x in f.read().splitlines()}:
                return b"", "new", ""
    except OSError:
        pass
    try:
        import arch_checkpoint as ck
        run_id = os.path.basename(d)
        cps = ck.list_checkpoints(root, run_id)
        if cps:
            repo = ck._repo(root)
            env = ck._env(repo, root)
            ref = cps[0]["ref"]
            if ck._run(["rev-parse", "--verify", "-q", ref + "^"], env, root, check=False).returncode == 0:
                p = ck._run(["cat-file", "blob", f"{ref}^:{rel}"], env, root, check=False)
                if p.returncode == 0:
                    return p.stdout, "checkpoint", ""
                return b"", "new", ""                     # the parent has no such file: this run made it
    except Exception:
        pass
    return None, "none", ("There is no copy of this file from before the run (no backup in the run folder and no checkpoint of the first try), "
                          "so the file cannot be rebuilt from parts. Accept or Undo the whole run instead.")


def _hunk_groups(pre, post):
    """-> [{i1,i2,j1,j2,header,lines,added,removed}] using the same grouping `diff -U3` uses."""
    import difflib
    a, b = pre.splitlines(True), post.splitlines(True)
    groups = difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(3)
    out = []
    for g in groups:
        i1, i2, j1, j2 = g[0][1], g[-1][2], g[0][3], g[-1][4]
        lines = []
        for tag, x1, x2, y1, y2 in g:
            if tag == "equal":
                lines += [" " + l for l in a[x1:x2]]
            else:
                if tag in ("replace", "delete"):
                    lines += ["-" + l for l in a[x1:x2]]
                if tag in ("replace", "insert"):
                    lines += ["+" + l for l in b[y1:y2]]
        out.append({"i1": i1, "i2": i2, "j1": j1, "j2": j2,
                    "header": f"-{i1 + 1},{i2 - i1} +{j1 + 1},{j2 - j1}",
                    "lines": [l.rstrip("\r\n") for l in lines],
                    "added": sum(1 for l in lines if l[0] == "+"), "removed": sum(1 for l in lines if l[0] == "-")})
    return out


def _hunk_state(d):
    return _read_json(os.path.join(d, "hunks.json"))


def _post_bytes(d, rel, full):
    """The file as the run left it: the copy kept the first time a hunk was dropped, else what is on disk now."""
    keep = os.path.join(d, "post", *rel.split("/"))
    src = keep if os.path.isfile(keep) else full
    try:
        with open(src, "rb") as f:
            return f.read()
    except OSError:
        return None


def hunks_for(root, d, rel):
    """-> {"splittable", "why", "hunks": [{id, header, added, removed, lines, kept}]} for one changed file."""
    try:
        rel, full = _rel_safe(root, rel)
    except ValueError as ex:
        return {"splittable": False, "why": str(ex), "hunks": []}
    pre, kind, why = _prerun_bytes(root, d, rel)
    if pre is None:
        return {"splittable": False, "why": why, "hunks": []}
    post = _post_bytes(d, rel, full)
    if post is None:
        return {"splittable": False, "why": "The file is not there any more, so there is nothing to split.", "hunks": []}
    try:
        a, b = pre.decode("utf-8"), post.decode("utf-8")
    except UnicodeDecodeError:
        return {"splittable": False, "why": "This file is not plain text, so it cannot be split into parts.", "hunks": []}
    groups = _hunk_groups(a, b)
    if len(groups) > 200:
        return {"splittable": False, "why": "This file changed in more than 200 places; Keep or Drop the whole run instead.", "hunks": []}
    st = (_hunk_state(d).get(rel) or {})
    kept = set(st["keep"]) if isinstance(st.get("keep"), list) else set(range(len(groups)))
    return {"splittable": True, "why": "", "kind": kind, "hunks": [
        {"id": i, "header": g["header"], "added": g["added"], "removed": g["removed"], "lines": g["lines"][:400], "kept": i in kept}
        for i, g in enumerate(groups)]}


def review_hunks(b):
    """POST /api/arch/review/hunks {root, run, file, keep:[hunk ids]}: the file becomes its pre-run content plus the kept hunks."""
    root = safe_root(b.get("root") or HERE)
    run, d = _run_dir_of(root, b.get("run"))
    if not run or not os.path.isdir(d):
        raise ValueError("there is no run by that name")
    rel, full = _rel_safe(root, b.get("file"))
    keep = b.get("keep")
    if not isinstance(keep, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in keep):
        raise ValueError("keep must be a list of hunk numbers")
    pre, kind, why = _prerun_bytes(root, d, rel)
    if pre is None:
        return {"ok": False, "supported": False, "why": why}
    post = _post_bytes(d, rel, full)
    if post is None:
        return {"ok": False, "supported": False, "why": "The file is not there any more."}
    try:
        a, bb = pre.decode("utf-8"), post.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "supported": False, "why": "This file is not plain text, so it cannot be split into parts."}
    groups = _hunk_groups(a, bb)
    if any(k < 0 or k >= len(groups) for k in keep):
        raise ValueError("one of those hunks does not exist")
    keepset = set(keep)
    al, bl = a.splitlines(True), bb.splitlines(True)
    out, pos = [], 0
    for i, g in enumerate(groups):
        out += al[pos:g["i1"]]
        out += bl[g["j1"]:g["j2"]] if i in keepset else al[g["i1"]:g["i2"]]
        pos = g["i2"]
    out += al[pos:]
    new = "".join(out).encode("utf-8")
    keep_copy = os.path.join(d, "post", *rel.split("/"))
    if not os.path.isfile(keep_copy):                     # remember the run's own result once, so a dropped hunk can come back
        os.makedirs(os.path.dirname(keep_copy), exist_ok=True)
        with open(keep_copy, "wb") as f:
            f.write(post)
    removed = False
    if kind == "new" and not keepset:                       # the run made this file and nothing of it is kept
        if os.path.isfile(full):
            os.remove(full)
        removed = True
    else:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".arch-tmp"
        with open(tmp, "wb") as f:
            f.write(new)
        os.replace(tmp, full)
    st = _hunk_state(d)
    st[rel] = {"keep": sorted(keepset), "t": time.time(), "removed": removed}
    with open(os.path.join(d, "hunks.json"), "w", encoding="utf-8") as f:
        json.dump(st, f)
    return {"ok": True, "supported": True, "run": run, "file": rel, "removed": removed, "kept": len(keepset), "of": len(groups),
            "hunks": hunks_for(root, d, rel)}


# ---- Background jobs: the hub's own job and the queue's running / waiting tasks, with a live tail. Stop = the same stops the app already has.
def _tail_lines(text, n=12):
    ls = [l.rstrip() for l in str(text or "").splitlines() if l.strip()]
    return [l[:240] for l in ls[-n:]]


def _trace_tail(run_dir, n=10):
    out = []
    for r in _trace_records(run_dir, cap=4000)[-60:]:
        t = r.get("type")
        txt = r.get("text") or r.get("label") or r.get("gate") or r.get("name") or r.get("step") or r.get("verdict") or ""
        if t and txt:
            out.append(f"{t}: {str(txt)[:160]}")
    return out[-n:]


def bgjobs_view():
    """GET /api/arch/bgjobs -> running and waiting background jobs: {id, kind, title, state, secs, tail[], can_stop, stop_word}."""
    now = time.time()
    jobs = []
    j = mh.JOB
    try:
        with j.lock:
            if j.running():
                jobs.append({"id": "hub", "kind": "hub", "title": j.title or "Job", "state": "running", "secs": round(now - (j.started or now)),
                             "tail": _tail_lines(j.buf), "can_stop": True, "stop_word": "Stop"})
    except Exception:
        pass
    try:
        import arch_supervisor as sv
        for q in sv.listing(states=["running", "pending"], limit=40):
            running = q.get("state") == "running"
            tail, secs = [], 0
            if running:
                started = float(q.get("started") or now)
                secs = round(now - started)
                try:
                    rd = sv._newest_run(q.get("root") or "", started)
                    tail = _trace_tail(rd) if rd else []
                except Exception:
                    tail = []
            jobs.append({"id": f"q{q['id']}", "kind": "queue", "title": str(q.get("goal") or "")[:100], "state": "running" if running else "waiting",
                         "secs": secs, "flow": q.get("flow") or "", "tail": tail, "can_stop": True,
                         "stop_word": "Stop after this step" if running else "Cancel"})
    except Exception:
        pass
    return {"jobs": jobs, "t": now}


def bgjobs_stop(b):
    """POST /api/arch/bgjobs/stop {id}: hub job -> stop the process; waiting queue task -> cancel; running queue task -> .arch/STOP in its project."""
    jid = str(b.get("id") or "")
    if jid == "hub":
        mh.JOB.stop()
        return {"ok": True, "how": "stopped"}
    m = re.fullmatch(r"q(\d+)", jid)
    if not m:
        raise ValueError("that is not a job")
    import arch_supervisor as sv
    q = sv.get(int(m.group(1)))
    if not q:
        raise ValueError("there is no such task")
    if q.get("state") == "pending":
        return {"ok": bool(sv.cancel(q["id"])), "how": "cancelled"}
    if q.get("state") == "running":
        root = q.get("root") or ""
        if not os.path.isdir(root):
            raise ValueError("the project folder is gone")
        os.makedirs(os.path.join(root, ".arch"), exist_ok=True)
        with open(os.path.join(root, ".arch", "STOP"), "w", encoding="utf-8") as f:
            f.write("stopped from the background jobs panel\n")
        return {"ok": True, "how": "stopping after the current step"}
    return {"ok": False, "how": "it was not running"}


# ---- Hooks: your own commands at fixed points. Machine hooks are yours; a project's hook file needs trust, and any edit revokes it.
def _hooks_doc(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"hooks": []}
    if isinstance(d, list):
        d = {"hooks": d}
    if not isinstance(d, dict) or not isinstance(d.get("hooks"), list):
        d = dict(d) if isinstance(d, dict) else {}
        d["hooks"] = []
    return d


def hooks_view(root):
    import arch_hooks as hk
    mf, pf = hk.machine_file(), hk.project_file(root)

    def rows(path):
        out = []
        for i, r in enumerate(_hooks_doc(path)["hooks"]):
            if isinstance(r, dict) and r.get("run") and r.get("on") in hk.POINTS:
                out.append({"index": i, "on": r["on"], "run": str(r["run"])[:400], "match": str(r.get("match") or ""), "when": str(r.get("when") or ""),
                            "blocking": bool(r.get("blocking")), "timeout": int(r.get("timeout") or hk.DEFAULT_TIMEOUT), "name": str(r.get("name") or "")[:60]})
        return out
    cfg_ = core.load_config()
    plugin_wait = [{"plugin": u.get("plugin"), "count": len(u["hooks"]), "commands": [x["run"][:200] for x in u["hooks"]]}
                   for u in hk.load(root, cfg_)["untrusted"] if u.get("plugin")]
    return {"points": list(hk.POINTS), "root": root,
            "machine": {"path": mf, "exists": os.path.isfile(mf), "hooks": rows(mf)},
            "project": {"path": pf, "exists": os.path.isfile(pf), "hooks": rows(pf), "trusted": bool(os.path.isfile(pf) and hk.trusted(pf)),
                        "trust_all": bool((cfg_.get("hooks") or {}).get("trust_project"))},
            "plugins_waiting": plugin_wait}


def hooks_change(b):
    """POST /api/arch/hooks {root, action: add|remove|trust|untrust, scope: machine|project, hook:{...}, index, confirm}."""
    import arch_hooks as hk
    root = safe_root(b.get("root") or HERE)
    action, scope = str(b.get("action") or ""), str(b.get("scope") or "project")
    if scope not in ("machine", "project"):
        raise ValueError("scope must be machine or project")
    path = hk.machine_file() if scope == "machine" else hk.project_file(root)
    if action in ("add", "trust") and b.get("confirm") is not True:
        raise ValueError("Hooks run commands on this PC. Say confirm: true after reading the command.")
    if action == "trust":
        if scope != "project" or not os.path.isfile(path):
            raise ValueError("there is no project hook file to trust")
        return {"ok": bool(hk.trust(path)), "view": hooks_view(root)}
    if action == "untrust":
        hk.trust(path, False)
        return {"ok": True, "view": hooks_view(root)}
    doc = _hooks_doc(path)
    if action == "add":
        h = b.get("hook") or {}
        run_ = str(h.get("run") or "").strip()
        if not run_ or len(run_) > 400 or "\n" in run_:
            raise ValueError("the command must be one line, up to 400 characters")
        if h.get("on") not in hk.POINTS:
            raise ValueError("pick a point: " + ", ".join(hk.POINTS))
        row = {"on": h["on"], "run": run_, "blocking": bool(h.get("blocking"))}
        if str(h.get("name") or "").strip():
            row["name"] = str(h["name"]).strip()[:60]
        if str(h.get("match") or "").strip():
            row["match"] = str(h["match"]).strip()[:80]
        if h.get("when") in ("pass", "fail", "not_sure"):
            row["when"] = h["when"]
        try:
            row["timeout"] = max(1, min(900, int(h.get("timeout") or hk.DEFAULT_TIMEOUT)))
        except (TypeError, ValueError):
            pass
        if len(doc["hooks"]) >= 50:
            raise ValueError("that is a lot of hooks (50 is the limit)")
        doc["hooks"].append(row)
    elif action == "remove":
        i = b.get("index")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(doc["hooks"]):
            raise ValueError("there is no hook with that number")
        del doc["hooks"][i]
    else:
        raise ValueError("action must be add, remove, trust or untrust")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    core._atomic_write(path, json.dumps(doc, indent=2))
    return {"ok": True, "view": hooks_view(root),
            "note": "Editing a project's hook file takes back your approval; trust it again to run it." if scope == "project" else ""}


# ---- After Accept: one commit on its own branch (arch_gitflow; never pushes) and a PR-style summary. Refusals come back in plain words.
def git_commit_run(b):
    """POST /api/arch/git/commit {root, run}: commits exactly the run's changed files on a branch architect/<run>, then writes the PR summary."""
    import arch_gitflow as gf
    root = safe_root(b.get("root") or HERE)
    rv = review_view(root, b.get("run"))
    if not rv.get("run"):
        raise ValueError("there is no run by that name")
    run = rv["run"]
    files = [f["file"] for f in rv.get("files") or []]
    if not files:
        return {"ok": False, "why": "This run did not change any files, so there is nothing to commit.", "todo": ""}
    ok, why = gf.available(root)
    if not ok:
        return {"ok": False, "why": why, "todo": "Git needs to be installed and this folder needs to be a git project (git init)."}
    st = gf.load_state(root, run)
    if not st:                                   # the run ran without a branch: make one now; the run's own files are not "your earlier edits"
        pend = sorted(gf.status(root))
        others = [p for p in pend if p not in set(files)]
        started = gf.start_run(root, run, allow_dirty=True)
        if not started.get("ok"):
            return {"ok": False, "why": started.get("why") or "could not make a branch", "todo": "Fix that and press the button again."}
        st = gf.load_state(root, run) or {}
        st["dirty_before"] = others
        gf._save_state(root, run, st)
    res = gf.commit_run(root, run, files, goal=rv.get("goal") or "")
    if not res.get("ok"):
        why = res.get("why") or "the commit was refused"
        todo = ("Commit or set aside your own edits to " + ", ".join((res.get("mixed") or [])[:3]) + ", then try again.") if res.get("mixed") else \
               ("Add those files to the run or commit them yourself first, then try again.") if res.get("unrelated") else \
               ("Switch back with: git switch " + str(st.get("branch"))) if "not on the run's branch" in why or "not on" in why else \
               "Fix that and press the button again."
        return {"ok": False, "why": why, "todo": todo}
    checks = [(t["name"], t["ok"]) for t in rv.get("tests") or []][:30]
    summary = gf.pr_summary(root, run, goal=rv.get("goal") or "", checks=checks, verdict=((rv.get("verdict") or {}).get("verdict") or ""))
    return {"ok": True, "already": bool(res.get("already")), "sha": res.get("sha"), "branch": res.get("branch"), "files": res.get("files"),
            "message": res.get("message") or "", "summary_path": os.path.join(root, ".arch", "gitflow", re.sub(r"[^\w.-]+", "_", run) + ".PR.md"),
            "summary": summary[:6000], "pushed": False}


# ---- Set up this project: arch_onboard reads the project (no model) and drafts ARCHITECT.md; the person accepts it.
def onboard_project(b):
    """POST /api/arch/onboard {root[, accept, force]}: look at the project and draft ARCHITECT.md, or accept the draft."""
    import arch_onboard as ob
    root = safe_root(b.get("root") or HERE)
    if b.get("accept"):
        return ob.accept_draft(root, force=bool(b.get("force")))
    rep = ob.onboard(root, build_index=False)
    if not rep.get("ok"):
        return rep
    keep = ("ok", "root", "files", "language", "languages", "frameworks", "test_command", "run_command", "entry_points", "cli_commands",
            "http_routes", "conventions", "gotchas", "git", "existing_architect_md", "draft_path")
    out = {k: rep.get(k) for k in keep}
    out["draft"] = str(rep.get("draft") or "")[:12000]
    return out


def _plugins_plain():
    f, _why = _optional("arch_plugins", "list")
    if not f:
        return []
    rows, err = _guarded(f)
    if err:
        return []
    return [{"name": str(r["name"]), "enabled": bool(r.get("enabled"))} for r in (rows.values() if isinstance(rows, dict) else rows or [])
            if isinstance(r, dict) and r.get("name")]


def session_view(root, run):
    """GET /api/arch/session: context use by section, plugins/connectors, outputs, changes, servers, background jobs."""
    run, d = _run_dir_of(root, run)
    recs = _trace_records(d) if run else []
    calls = [r for r in recs if r.get("type") == "call"]
    last = calls[-1] if calls else {}
    window = int(last.get("num_ctx") or 0)
    used = int(last.get("prompt_tokens") or 0) + int(last.get("gen_tokens") or 0)
    secs = last.get("sections") if isinstance(last.get("sections"), dict) else None
    keys = ["system prompt", "skills", "memory", "tools", "project files", "conversation"]
    parts = {k: 0 for k in keys}
    if secs:
        for k, v in secs.items():
            kk = str(k).replace("_", " ")
            if kk in parts:
                parts[kk] = int(v or 0)
    elif last:
        for m in last.get("messages") or []:
            if isinstance(m, dict):
                c = m.get("content")
                n = len(c if isinstance(c, str) else json.dumps(c)) // 4
                parts["system prompt" if m.get("role") == "system" else "conversation"] += n
        tot = sum(parts.values())
        if tot and last.get("prompt_tokens"):
            sc = int(last["prompt_tokens"]) / tot             # scale the estimate to the tokens the server counted
            parts = {k: int(v * sc) for k, v in parts.items()}
    diffs = {}
    for r in recs:
        if r.get("type") == "diff" and r.get("file"):
            diffs[str(r["file"])] = r
    out = {"run": run, "window": window, "used": min(used, window) if window else used, "estimated": secs is None,
           "sections": [{"name": k, "tokens": v} for k, v in parts.items()], "compaction": int(window * 0.85) if window else 0,
           "compaction_note": "assumed at 85% of the window", "calls": len(calls),
           "outputs": [{"file": f, "new": bool(r.get("new_file"))} for f, r in diffs.items()][:30],
           "changes": {"files": len(diffs), "added": sum(int(r.get("added") or 0) for r in diffs.values()),
                       "removed": sum(int(r.get("removed") or 0) for r in diffs.values())}}
    try:
        out["plugins"] = _plugins_plain()
    except Exception:
        out["plugins"] = []
    try:
        import arch_hosts
        out["connectors"] = [{"name": str(x), "state": "configured"} for x in list(arch_hosts.hosts_of(core.load_config()))[:10]]
    except Exception:
        out["connectors"] = []
    try:
        import arch_server
        s = arch_server.status()
        out["servers"] = [{"tier": p["tier"], "name": p.get("name"), "port": p.get("port"), "ctx": p.get("ctx"), "up": bool(p.get("up")),
                           "place": "memory (RAM)" if p.get("on_cpu") else "graphics card"} for p in s.get("processes") or []]
        out["server_mode"] = s.get("mode")
    except Exception:
        out["servers"] = []
    jobs = []
    try:
        with mh.JOB.lock:
            if mh.JOB.running():
                jobs.append({"title": mh.JOB.title, "secs": round(time.time() - (mh.JOB.started or time.time()))})
    except Exception:
        pass
    try:
        import arch_supervisor as sv
        for j in sv.listing(states=["running"], limit=10):
            jobs.append({"title": "Queue: " + str(j.get("goal"))[:60], "secs": round(time.time() - float(j.get("started") or time.time()))})
    except Exception:
        pass
    out["jobs"] = jobs
    return out


# ---- Plugins, Telemetry and Squad runs pages (arch_plugins / arch_telemetry / arch_squad are optional; a missing module or
# function answers {"available": False, "why": ...}). Expensive GETs are cached for a few seconds; a change clears the cache.
_TTL_CACHE = {}
_TTL_LOCK = threading.Lock()


def _ttl(key, seconds, fn):
    """-> fn() at most once per `seconds` per key. ARCHITECT_NO_CACHE=1 turns it off (tests)."""
    if os.environ.get("ARCHITECT_NO_CACHE") == "1":
        return fn()
    now = time.time()
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit and now - hit[0] < seconds:
            return hit[1]
    val = fn()
    with _TTL_LOCK:
        _TTL_CACHE[key] = (now, val)
        if len(_TTL_CACHE) > 64:
            for k in sorted(_TTL_CACHE, key=lambda k: _TTL_CACHE[k][0])[:16]:
                _TTL_CACHE.pop(k, None)
    return val


def _ttl_drop(prefix):
    with _TTL_LOCK:
        for k in [k for k in _TTL_CACHE if k.startswith(prefix)]:
            _TTL_CACHE.pop(k, None)


def commands_view(root=None):
    """Every enabled command for the Console's slash palette: shipped, project and enabled plugins' commands, with the plugin that brought
    it. A plugin command whose bare name is taken is listed under plugin:name (arch_commands.expand accepts both)."""
    import arch_commands
    out = []
    for key, c in sorted(arch_commands.load_all(root).items()):
        out.append({"name": key, "description": c.get("description", ""), "flow": c.get("flow", ""), "scope": c.get("scope", ""),
                    "needs_args": bool(c.get("needs_args")), "args_hint": c.get("args_hint", ""), "plugin": c.get("plugin", "")})
    return out


def _plugin_commands(folder):
    """The commands one plugin folder holds (enabled or not): [{name, description, args_hint, needs_args}]."""
    out = []
    try:
        import arch_commands
        d = os.path.join(folder, "commands")
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if fn.lower().endswith(".md"):
                with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                    rec = arch_commands.parse(f.read(), os.path.join(d, fn), "plugin")
                if rec:
                    out.append({"name": rec["name"], "description": rec["description"], "args_hint": rec["args_hint"], "needs_args": rec["needs_args"]})
    except Exception:
        pass
    return out[:50]


def rpresets_view(raw=None):
    """Settings > Presets: Fast / Balanced / Quality / Battery (data/presets, applied by `architect.py preset NAME`), each with exactly what
    it would change from the settings in force now, and the last applied one for Undo. -> {available, presets:[...], undo}."""
    try:
        import arch_roleprofile as rp
        raw = rp._mh().read_config() if raw is None else raw
        out = []
        for name, p in rp.presets().items():
            ch = []
            for path_, v in (p.get("settings") or {}).items():
                cur = rp._get(raw, path_)
                if cur == rp._MISSING:
                    cur = None
                    d = core.DEFAULTS
                    for part in path_.split("."):
                        d = d.get(part) if isinstance(d, dict) else None
                    cur = d
                if cur != v:
                    ch.append({"what": path_, "from": cur, "to": v})
            for role, vals in (p.get("role_profiles") or {}).items():
                mine = (raw.get("role_profiles") or {}).get(role) or {}
                for k, v in vals.items():
                    if mine.get(k) != v:
                        ch.append({"what": f"{role}: {k}", "from": mine.get(k), "to": v})
            out.append({"id": name, "label": p.get("label") or name.title(), "blurb": p.get("blurb", ""), "changes": ch, "active": not ch})
        und = rp._load_undo()
        return {"available": True, "presets": out, "undo": (und[-1].get("what") if und else "")}
    except Exception as ex:
        return {"available": False, "why": f"{type(ex).__name__}: {str(ex)[:160]}", "presets": [], "undo": ""}


def rpreset_change(b):
    """POST /api/arch/rpreset {name} applies a preset (with an undo record); {undo: true} puts back what the last write changed."""
    import arch_roleprofile as rp
    if b.get("undo"):
        rec = rp.undo()
        return {"ok": True, "undone": rec["what"] if rec else "", **rpresets_view()}
    try:
        rec = rp.apply_preset(str(b.get("name") or ""))
    except (KeyError, ValueError) as ex:
        raise ValueError(str(ex))
    return {"ok": True, "applied": rec["what"], **rpresets_view()}


_PROVIDES = ("agents", "workflows", "skills", "commands", "hooks")


def plugins_list():
    """GET /api/arch/plugins -> {available, plugins:[{name, version, description, enabled, trusted, provides, problems}]}."""
    def get():
        f, why = _optional("arch_plugins", "list")
        if not f:
            return {"available": False, "why": why, "plugins": []}
        rows, err = _guarded(f)
        if err:
            return {"available": False, "why": err, "plugins": []}
        out = []
        for r in (rows.values() if isinstance(rows, dict) else rows or []):
            if not isinstance(r, dict) or not r.get("name"):
                continue
            pv = r.get("provides") if isinstance(r.get("provides"), dict) else {}
            out.append({"name": str(r["name"]), "version": str(r.get("version") or ""), "description": str(r.get("description") or "")[:400],
                        "enabled": bool(r.get("enabled")), "trusted": bool(r.get("trusted")),
                        "provides": {k: [str(x) for x in (pv.get(k) or [])][:50] if isinstance(pv.get(k), (list, tuple))
                                     else (pv.get(k) or 0) for k in _PROVIDES},
                        "problems": [str(x) for x in (r.get("problems") or [])][:10], "source": str(r.get("source") or r.get("path") or ""),
                        "command_info": _plugin_commands(r["dir"]) if r.get("dir") else []})
        return {"available": True, "plugins": out}
    return _plain(_ttl("plugins", 5, get))


def plugins_change(action, b):
    """POST /api/arch/plugins/{enable,disable,trust,install,remove}. trust needs {confirm: true}; install takes {path}."""
    if action not in ("enable", "disable", "trust", "install", "remove"):
        raise ValueError("unknown plugin action")
    f, why = _optional("arch_plugins", action)
    if not f:
        raise ValueError(why)
    if action == "install":
        p = str(b.get("path") or "").strip().strip('"')
        if not p or len(p) > 1000 or not (os.path.isdir(p) or (os.path.isfile(p) and p.lower().endswith(".zip"))):
            raise ValueError("Give a plugin folder or a .zip file that exists on this PC.")
        name = f(p)
        _ttl_drop("plugins")
        return {"ok": True, "name": str(name), **plugins_list()}
    name = str(b.get("name") or "")
    if not _NAME_RE.match(name):
        raise ValueError("bad plugin name")
    if action == "trust" and b.get("confirm") is not True:
        raise ValueError("Trusting a plugin lets its hooks and commands run code on this PC. Say confirm: true.")
    f(name)
    _ttl_drop("plugins")
    return {"ok": True, "name": name, **plugins_list()}


def telemetry_summary(window):
    """GET /api/arch/telemetry/summary?window=seconds -> {available, window, calls, tokens_in, ...}."""
    def get():
        f, why = _optional("arch_telemetry", "summary")
        if not f:
            return {"available": False, "why": why, "window": window}
        s, err = _guarded(f, window)
        if err or not isinstance(s, dict):
            return {"available": False, "why": err or "summary() returned nothing", "window": window}
        return {"available": True, "window": window, **s}
    return _plain(_ttl(f"tele.sum.{window}", 5, get))


def telemetry_events(since=None, run=None, kinds=None, limit=300):
    """GET /api/arch/telemetry/events?since=&run=&kinds=a,b -> {available, events:[...]} (newest last, at most `limit`)."""
    f, why = _optional("arch_telemetry", "read")
    if not f:
        return {"available": False, "why": why, "events": []}
    rows, err = _guarded(f, since=since, run=run or None, kinds=kinds or None, limit=limit)
    if err:
        return {"available": False, "why": err, "events": []}
    return _plain({"available": True, "events": list(rows or [])[-limit:]})


def telemetry_timeline(run_id):
    """GET /api/arch/telemetry/timeline?run= -> {available, run, spans:[{kind, start, end, duration, children}]}."""
    if not _NAME_RE.match(run_id or ""):
        return {"available": False, "why": "give a run id", "spans": []}

    def get():
        f, why = _optional("arch_telemetry", "timeline")
        if not f:
            return {"available": False, "why": why, "spans": []}
        t, err = _guarded(f, run_id)
        if err:
            return {"available": False, "why": err, "spans": []}
        spans = t.get("spans") if isinstance(t, dict) else t
        return {"available": True, "run": run_id, "spans": list(spans or [])}
    return _plain(_ttl("tele.tl." + run_id, 3, get))


def squad_runs():
    """GET /api/arch/squad/runs -> {available, runs:[{run_id, workflow, started, status, agents, cached}]}."""
    def get():
        f, why = _optional("arch_squad", "list_runs")
        if not f:
            return {"available": False, "why": why, "runs": []}
        rows, err = _guarded(f)
        if err:
            return {"available": False, "why": err, "runs": []}
        keep = ("run_id", "workflow", "started", "status", "agents", "cached")
        return {"available": True, "runs": [{k: r.get(k) for k in keep} for r in (rows or []) if isinstance(r, dict)][:200]}
    return _plain(_ttl("squad.runs", 5, get))


def squad_resume(b):
    """POST /api/arch/squad/resume {run_id} -> starts `architect.py crew <workflow> --resume <run_id>` as the hub job."""
    rid = str(b.get("run_id") or "")
    if not _NAME_RE.match(rid):
        raise ValueError("bad run id")
    row = next((r for r in squad_runs().get("runs") or [] if r.get("run_id") == rid), None)
    if not row:
        raise ValueError("no such run")
    wf = str(row.get("workflow") or "")
    if not _NAME_RE.match(wf):
        raise ValueError("that run has no workflow name")
    argv = ["architect.py", "crew", wf, "--resume", rid]
    if b.get("fake"):
        argv.append("--fake")
    mh.JOB.start("Crew: resume " + rid[:40], argv)
    _ttl_drop("squad.")
    return {"ok": True, "run_id": rid, "argv": argv[1:]}


def select_why():
    """GET /api/arch/select/why -> {available, jobs:[{job, current, best, C, pass_rate, ci, tok_s, fits, n, reason, suggestion}]}."""
    f, why = _optional("arch_select", "why")
    if not f:
        return {"available": False, "why": why, "jobs": []}
    d, err = _guarded(f, core.load_config(HERE))
    if err:
        return {"available": False, "why": err, "jobs": []}
    keep = ("current", "best", "C", "pass_rate", "ci", "tok_s", "fits", "n", "reason", "suggestion")
    rows = [dict({"job": str(k)}, **{f_: (v or {}).get(f_) for f_ in keep}) for k, v in (d or {}).items() if isinstance(v, dict)]
    return _plain({"available": True, "jobs": rows})


_JOB_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$")


def select_apply(b):
    job, model = str(b.get("job") or ""), str(b.get("model") or "")
    if not _JOB_RE.match(job):
        raise ValueError("Unknown job.")
    if not _MODEL_RE.match(model):
        raise ValueError("That is not a model name.")
    try:
        import arch_jobs
        known = getattr(arch_jobs, "JOBS", None)
        if known and job not in known:
            raise ValueError(f"'{job}' is not one of the jobs.")
    except ImportError:
        pass
    mh.JOB.start(f"Use {model} for {job}", ["architect.py", "select", "--apply", job, model])
    return {"ok": True}


def select_screen(b):
    argv = ["architect.py", "select", "--screen"]
    if b.get("fake"):
        argv.append("--fake")
    for key, flag, rx in (("models", "--models", _MODEL_RE), ("jobs", "--jobs", _JOB_RE)):
        v = b.get(key) or []
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",")]
        v = [str(x).strip() for x in v if str(x).strip()]
        for x in v:
            if not rx.match(x):
                raise ValueError(f"'{x[:60]}' is not a valid {key[:-1]} name.")
        if v:
            argv += [flag, ",".join(v)]
    mh.JOB.start("Screen models" + (" (fake)" if b.get("fake") else ""), argv)
    return {"ok": True}



_TPL_KEYS = ("id", "name", "description", "long", "category", "stack", "types", "difficulty", "colour", "files", "next_steps", "contract")


def templates_catalog():
    """GET /api/arch/templates/catalog -> {available, templates:[{id, name, ..., thumb}]} ; available False when the module is missing."""
    try:
        import arch_templates_catalog as tc
        rows = tc.catalog()
    except Exception as e:                       # missing module or a broken catalog: the UI falls back to the plain list
        return {"available": False, "why": type(e).__name__ + ": " + str(e)[:200], "templates": []}
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(r.get("id", ""))):
            continue
        t = {k: r.get(k) for k in _TPL_KEYS}
        t["thumb"] = "/templates/%s.svg" % t["id"] if os.path.isfile(os.path.join(HERE, "static", "templates", t["id"] + ".svg")) else None
        out.append(t)
    return {"available": True, "templates": out}


def recipes_list():
    """GET /api/arch/recipes -> {available, recipes:[...]} from arch_recipes.list(); a missing module answers available False."""
    try:
        import arch_recipes as rc
        rows = rc.list()
    except Exception as e:
        return {"available": False, "why": type(e).__name__ + ": " + str(e)[:200], "recipes": []}
    return {"available": True, "recipes": [r for r in rows if isinstance(r, dict) and r.get("id")]}


def recipes_fill(b):
    """POST /api/arch/recipes/fill {recipe, root, params} -> arch_recipes.fill(...): the goal text, the flow and what the project lacks."""
    rid = str(b.get("recipe") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", rid):
        raise ValueError("Pick a recipe.")
    root = safe_root(b.get("root") or HERE)
    params = b.get("params") if isinstance(b.get("params"), dict) else {}
    params = {str(k): str(v)[:200] for k, v in params.items() if re.fullmatch(r"[a-z_]{1,30}", str(k))}
    import arch_recipes as rc
    try:
        return rc.fill(rid, root, **params)
    except KeyError:
        raise ValueError("There is no recipe by that name.")


# ---------------------------------------------------------------- page registry, lessons, overnight report, data night, train the worker
def ui_registry():
    """GET /api/arch/ui/registry: the sidebar places, the pages (id, icon, label, description), the old-id map and the page functions,
    parsed from hub_ui.html by arch_uireg (cached; re-read only when the file changes). Warmed at server start."""
    import arch_uireg
    return arch_uireg.load(os.path.join(HERE, "hub_ui.html"))


try:
    ui_registry()
except Exception:
    pass


def dataset_lesson_counts(out_dir=None):
    """Samples per curriculum lesson in the per-model tier SFT files (meta.lesson). -> {counts, total, files}."""
    out_dir = out_dir or os.path.join(core.data_dir(), "datasets")
    counts, total, files = {}, 0, 0
    try:
        names = sorted(os.listdir(out_dir))
    except OSError:
        names = []
    for fn in names:
        if not (fn.startswith("tier-") and fn.endswith(".sft.jsonl") and fn.count(".") >= 3):
            continue
        files += 1
        try:
            with open(os.path.join(out_dir, fn), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    total += 1
                    lsn = (r.get("meta") or {}).get("lesson") or "(none)"
                    counts[lsn] = counts.get(lsn, 0) + 1
        except OSError:
            continue
    return {"counts": counts, "total": total, "files": files}


def lessons_view():
    """GET /api/arch/lessons: arch_lessons.load() + check() as rows. Each target says whether it exists (green), samples so far come from
    the dataset's lesson counts (null when no dataset has lesson tags yet)."""
    try:
        import arch_lessons as al
        rows = al.load()
        problems = al.check(rows)
    except Exception as ex:
        return {"available": False, "why": f"{type(ex).__name__}: {str(ex)[:160]}", "lessons": [], "problems": []}
    ds = dataset_lesson_counts()
    tagged = ds["total"] and any(k != "(none)" for k in ds["counts"])
    out = []
    for L in rows:
        def tg(v):
            res = []
            for x in al._targets(v):
                ok, why = al.target_ok(x)
                res.append({"target": x, "ok": bool(ok), "why": "" if ok else why})
            return res
        n = None
        if tagged:
            n = ds["counts"].get(L.get("id"), 0)
            tv = L.get("trains_via")
            if tv and tv != L.get("id"):
                n += ds["counts"].get(tv, 0)
        out.append({"id": L.get("id"), "lesson": L.get("lesson"), "evidence": L.get("evidence"), "status": L.get("status"),
                    "enforced_by": tg(L.get("enforced_by")), "checked_by": tg(L.get("checked_by")), "trains_via": L.get("trains_via"),
                    "samples": n})
    return {"available": True, "lessons": out, "problems": problems, "dataset": {"total": ds["total"], "files": ds["files"]}}


def _wilson(k, n):
    try:
        import arch_select
        lo, hi = arch_select.wilson(k, n)
    except Exception:
        lo, hi = (0.0, 1.0) if n <= 0 else (max(0.0, k / n - 0.2), min(1.0, k / n + 0.2))
    return [round(lo * 100, 1), round(hi * 100, 1)]


def _eval_card(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if d.get("fake"):
        return None
    rs = d.get("results") or []
    k = sum(1 for r in rs if r.get("verdict") == "pass")
    fl = sum(1 for r in rs if r.get("verdict") == "fail")
    n = k + fl
    return {"label": d.get("label") or "", "ts": d.get("ts"), "passed": k, "n": n, "not_sure": len(rs) - n,
            "rate": round(100.0 * k / n, 1) if n else None, "ci": _wilson(k, n) if n else None, "file": os.path.basename(path)}


def _glob_evals():
    import glob as _glob
    return sorted(_glob.glob(os.path.join(core.data_dir(), "evals", "*.json")), key=os.path.getmtime)


def datanight_state():
    j = mh.JOB
    try:
        run_ = bool(j.running()) and str(j.title).startswith("Data night")
    except Exception:
        run_ = False
    stop = os.path.exists(os.path.join(core.data_dir(), "STOP"))
    out = {"running": run_, "stopping": bool(run_ and stop), "title": j.title if run_ else "", "started": j.started if run_ else None}
    if run_:
        out["line"] = job_eta().get("line", "")
    return out


def overnight_view():
    """GET /api/arch/overnight: the Home report card for the last data night / eval. {ran:false} when nothing ran yet.
    Pass rate = the newest two non-fake evals whose label says holdout, else the newest two non-fake evals (basis says which)."""
    d = core.data_dir()
    night = {}
    try:
        with open(os.path.join(d, "data_night.json"), encoding="utf-8") as f:
            night = (json.load(f) or {}).get("last") or {}
    except (OSError, ValueError):
        pass
    ds = dataset_lesson_counts()
    paths = _glob_evals()
    cards = [c for c in (_eval_card(p) for p in paths[-14:]) if c]
    hold = [c for c in cards if "holdout" in c["label"].lower()]
    pool, basis = (hold, "holdout") if hold else (cards, "latest runs (none is labelled holdout yet)")
    latest = pool[-1] if pool else None
    prev = pool[-2] if len(pool) > 1 else None
    fastest = None
    try:
        import arch_costmodel as cm
        rows = [r for r in cm.from_evals(paths) if r.get("passed") and (r.get("tasks") or 0) >= 3 and r.get("C")]
        if rows:
            r = min(rows, key=lambda x: x["C"])
            fastest = {"model": r.get("model"), "flow": r.get("flow"), "tasks": r.get("tasks"), "secs_per_task": round(r.get("t") or 0, 1),
                       "secs_per_pass": round(r["C"], 1), "pass_rate": round(100.0 * (r.get("p") or 0), 1)}
    except Exception:
        pass
    per = {k: v for k, v in ds["counts"].items() if k != "(none)"}
    ran = bool(night or ds["total"] or latest)
    return {"ran": ran, "night": night, "samples": {"total": ds["total"], "by_lesson": per, "untagged": ds["counts"].get("(none)", 0)},
            "basis": basis, "latest": latest, "previous": prev,
            "delta": (round(latest["rate"] - prev["rate"], 1) if latest and prev and latest["rate"] is not None and prev["rate"] is not None else None),
            "fastest": fastest, "running": datanight_state()}


def datanight_view(q=None):
    """GET /api/arch/datanight[?until=&max=&worker=]: running state, the plain defaults and an ETA / expected-passes preview."""
    q = q or {}
    cfg = core.load_config()
    tiers = cfg.get("tiers") or {}
    out = {"state": datanight_state(), "defaults": {"until": "07:00", "worker": tiers.get("worker") or "", "teacher": bool(tiers.get("heavy")),
                                                    "heavy": tiers.get("heavy") or "", "curriculum": True}}
    try:
        import arch_eta
        import importlib.util
        spec = importlib.util.spec_from_file_location("train_traces", os.path.join(HERE, "tools", "train_traces.py"))
        tt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tt)
        until = (q.get("until") or [""])[0]
        t_end = tt.until_epoch(until) if until else None
        mx = int((q.get("max") or ["0"])[0] or 0)
        worker = (q.get("worker") or [""])[0] or tiers.get("worker") or None
        est = arch_eta.estimate(0, mx or 105, family="distributed", model=worker, until=t_end)
        out["preview"] = est
        fits = est.get("fits") if est.get("fits") is not None else est.get("left")
        cards = [c for c in (_eval_card(p) for p in _glob_evals()[-6:]) if c and c["n"]]
        share = (sum(c["passed"] for c in cards) / max(1, sum(c["n"] for c in cards))) if cards else None
        out["expected"] = {"tasks": fits, "passing": (round(fits * share) if (share is not None and fits is not None) else None),
                           "pass_share": (round(share * 100, 1) if share is not None else None)}
    except ValueError as ex:
        out["error"] = str(ex)
    except Exception as ex:
        out["preview_error"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    return out


MIN_SAMPLES_DRY, MIN_SAMPLES_REAL = 10, 50


def trainworker_view():
    """GET /api/arch/trainworker: how many worker samples exist, and whether the button may run (and why not)."""
    d = os.path.join(core.data_dir(), "datasets")
    n = 0
    try:
        for fn in os.listdir(d):
            if fn.startswith("tier-worker") and fn.endswith(".sft.jsonl") and fn.count(".") >= 3:
                with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                    n += sum(1 for line in f if line.strip())
    except OSError:
        pass
    try:
        busy = bool(mh.JOB.running())
    except Exception:
        busy = False
    why_dry = ("A job is already running: stop it or wait." if busy else
               (f"Only {n} worker samples so far; a dry run needs {MIN_SAMPLES_DRY}. Run a data night first." if n < MIN_SAMPLES_DRY else ""))
    why_real = why_dry or (f"Only {n} worker samples so far; a real run needs {MIN_SAMPLES_REAL}." if n < MIN_SAMPLES_REAL else "")
    return {"samples": n, "min_dry": MIN_SAMPLES_DRY, "min_real": MIN_SAMPLES_REAL, "can_dry": not why_dry, "why_dry": why_dry,
            "can_real": not why_real, "why_real": why_real, "worker": (core.load_config().get("tiers") or {}).get("worker") or ""}


def datanight_start(b):
    argv = ["tools/train_traces.py"]
    until = str(b.get("until") or "").strip()
    if until:
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", until):
            raise ValueError("say the end time as HH:MM")
        argv += ["--until", until]
    if b.get("max"):
        argv += ["--max-tasks", str(max(1, min(2000, int(b["max"]))))]
    worker = str(b.get("worker") or "").strip()
    if worker:
        if not re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,160}", worker):
            raise ValueError("that is not a model name")
        argv += ["--worker", worker]
    if b.get("curriculum"):
        argv += ["--curriculum"]
    teacher = b.get("teacher")
    if teacher is True:
        heavy = str(b.get("heavy") or (core.load_config().get("tiers") or {}).get("heavy") or "").strip()
        if heavy and re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,160}", heavy):
            argv += ["--heavy", heavy]
    elif teacher is False:
        argv += ["--heavy-retries", "0"]
    try:
        os.remove(os.path.join(core.data_dir(), "STOP"))          # an old Stop must not end tonight's run before it starts
    except OSError:
        pass
    mh.JOB.start("Data night" + (f" until {until}" if until else ""), argv)
    _ttl_drop("overnight")
    return {"ok": True, "argv": argv[1:]}


def _cli_has(flag):
    """True when architect.py's source mentions the flag (so the hub never passes an option the CLI would refuse)."""
    try:
        with open(os.path.join(HERE, "architect.py"), encoding="utf-8", errors="replace") as f:
            return f'"{flag}"' in f.read()
    except OSError:
        return False


def datanight_stop():
    """Stop safely: write the STOP file; the night finishes the task it is in, then exports and ends."""
    p = os.path.join(core.data_dir(), "STOP")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("stop requested from the hub\n")
    return {"ok": True, "stopping": True, "state": datanight_state()}


def trainworker_start(b):
    v = trainworker_view()
    real = bool(b.get("real"))
    if not (v["can_real"] if real else v["can_dry"]):
        raise ValueError(v["why_real"] if real else v["why_dry"])
    argv = ["tools/train_worker.py"] + (["--real"] if real else []) + (["--model", v["worker"]] if v["worker"] else [])
    mh.JOB.start("Train the worker" + (" (real)" if real else " (dry run)"), argv)
    return {"ok": True, "argv": argv[1:], "real": real}


class AHandler(mh.Handler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/arch/runs":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                return self._send(200, run_listing(safe_root((parse_qs(u.query).get("root") or [HERE])[0])))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/arch/recipes":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            return self._send(200, recipes_list())
        if u.path in ("/api/arch/review", "/api/arch/plan", "/api/arch/session"):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            q = parse_qs(u.query)
            try:
                root = safe_root((q.get("root") or [HERE])[0])
                fn = {"/api/arch/review": review_view, "/api/arch/plan": plan_view, "/api/arch/session": session_view}[u.path]
                return self._send(200, fn(root, (q.get("run") or [""])[0]))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/arch/trace":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            q = parse_qs(u.query)
            run_name = (q.get("run") or [""])[0]
            if not run_name or "/" in run_name or ".." in run_name:
                return self._send(400, {"error": "a valid run name is required"})
            try:
                root = safe_root((q.get("root") or [HERE])[0])
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, dict({"calls": trace_listing(root, run_name)}, **trace_economics(root, run_name)))
        if u.path == "/api/arch/serve":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                root = safe_root((parse_qs(u.query).get("root") or [HERE])[0])
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            out = runner.health(root)
            out["default_cmd"] = runner.default_cmd(root)
            out["log_tail"] = runner.logs(root, 200)
            return self._send(200, out)
        if u.path == "/api/arch/ollama":                # what Ollama holds in memory right now, ours or not (read-only)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            import arch_ollama
            return self._send(200, arch_ollama.loaded_view(core.load_config()))
        if u.path == "/api/arch/doctor":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                root = safe_root((parse_qs(u.query).get("root") or [HERE])[0], create_ok=True)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            import arch_doctor as doctor
            try:
                hub_port = self.server.server_address[1]
            except Exception:
                hub_port = 8766
            return self._send(200, doctor.run(root, hub_port))
        if u.path == "/api/arch/assess":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            import arch_assess as assess
            st = mh.build_state()
            return self._send(200, assess.assess(st.get("models", []), st.get("hw"), core.deep_merge(core.DEFAULTS, mh.read_config())))
        if u.path == "/api/arch/advise":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                root = safe_root((parse_qs(u.query).get("root") or [HERE])[0], create_ok=True)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            import arch_advisor as advisor
            import arch_assess as assess
            import arch_doctor as doctor
            try:
                hub_port = self.server.server_address[1]
            except Exception:
                hub_port = 8766
            d_out = doctor.run(root, hub_port)
            st = mh.build_state()
            a_out = assess.assess(st.get("models", []), st.get("hw"), core.deep_merge(core.DEFAULTS, mh.read_config()))
            return self._send(200, advisor.advise(root, d_out, a_out))
        if u.path == "/api/arch/gallery_readme":
            name = (parse_qs(u.query).get("pack") or [""])[0]
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
                return self._send(400, {"error": "bad pack name"})
            pth = os.path.join(HERE, "harnesses", name, "README.md")
            return self._send(200, {"text": _read_text(pth) if os.path.isfile(pth) else "No README for this pack."})
        if u.path == "/api/arch/gallery":
            out = {"packs": [], "agents": []}
            try:
                import arch_harness
                out["packs"] = arch_harness.list_packs()
            except Exception as ex:
                out["packs_error"] = str(ex)[:200]
            try:
                import arch_agentlib
                out["agents"] = arch_agentlib.list_templates()
            except Exception as ex:
                out["agents_error"] = str(ex)[:200]
            return self._send(200, out)
        if u.path == "/api/arch/flow_detail":
            import arch_dataflow
            return self._send(200, arch_dataflow.flow_detail((parse_qs(u.query).get("flow") or [""])[0]))
        if u.path == "/api/arch/why":
            # a finished run in plain sentences (arch_why: built from the trace records, no model call)
            import arch_why
            q = parse_qs(u.query)
            root = safe_root((q.get("root") or [HERE])[0])
            rid = (q.get("run") or [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", rid or ""):
                last = core.latest_run(root)
                rid = os.path.basename(last) if last else ""
            d = os.path.join(root, ".arch", "runs", rid) if rid else ""
            try:
                return self._send(200, {"run": rid, "sentences": arch_why.explain_run(d) if d and os.path.isdir(d) else []})
            except Exception as ex:
                return self._send(200, {"run": rid, "sentences": [], "error": str(ex)[:200]})
        if u.path == "/api/arch/live":
            # how many agents are working right now, anywhere (hub-started Claude Code runs + everything the monitor sees)
            now = time.time()
            c = getattr(AHandler, "_live_cache", None)
            if not c or now - c[0] > 10:
                names = []
                try:
                    import arch_claudecode as _cc
                    def _alive(pid):                      # a run whose process is gone is not working, whatever its record says
                        try:
                            import arch_server as _sv
                            return bool(pid) and _sv._alive(int(pid))
                        except Exception:
                            return False
                    names += [r.get("id") or "claude" for r in (_cc.list_runs(limit=30) or [])
                              if r.get("status") == "running" and _alive(r.get("pid"))]
                except Exception:
                    pass
                try:
                    import arch_overseer as _ov
                    s = _ov.summary()
                    names += [a.get("name") or "agent" for a in (s.get("agents") or [])
                              if (a.get("live") or a.get("status") == "running") and float(a.get("quiet_s") or 0) < 600]
                except Exception:
                    pass
                c = AHandler._live_cache = (now, sorted(set(map(str, names)))[:20])
            return self._send(200, {"running": len(c[1]), "names": c[1]})
        if u.path == "/fonts/inter.woff2":              # the bundled interface font (Inter, SIL OFL 1.1: static/fonts/LICENSE-Inter.txt)
            try:
                with open(os.path.join(HERE, "static", "fonts", "inter.woff2"), "rb") as f:
                    return self._send(200, f.read(), "font/woff2")
            except OSError:
                return self._send(404, {"error": "font missing"})
        if u.path.startswith("/templates/") and u.path.endswith(".svg"):   # template thumbnails (static/templates/<id>.svg)
            tid = u.path[len("/templates/"):-4]
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", tid):
                return self._send(400, {"error": "bad template id"})
            try:
                with open(os.path.join(HERE, "static", "templates", tid + ".svg"), "rb") as f:
                    return self._send(200, f.read(), "image/svg+xml")
            except OSError:
                return self._send(404, {"error": "thumbnail missing"})
        if u.path == "/api/arch/templates/catalog":     # the project template catalog (arch_templates_catalog), read-only
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            return self._send(200, templates_catalog())
        if u.path == "/api/arch/morning":               # the newest overnight report (tools/morning_report.py), read-only
            import glob as _glob
            fs = sorted(_glob.glob(os.path.join(core.data_dir(), "morning_*.md")), key=os.path.getmtime)
            if not fs:
                return self._send(200, {"text": "", "t": None})
            try:
                with open(fs[-1], encoding="utf-8") as f:
                    return self._send(200, {"text": f.read()[:200000], "t": os.path.getmtime(fs[-1]), "file": os.path.basename(fs[-1])})
            except OSError as ex:
                return self._send(200, {"text": "", "t": None, "error": str(ex)[:200]})
        if u.path == "/api/arch/orchestration":         # who handed what to whom: agents, tokens, cache, training data (read-only; cached 60 s)
            import arch_orchestration
            now = time.time()
            c = getattr(AHandler, "_orch_cache", None)
            if not c or now - c[0] > 60 or "fresh" in u.query:
                try:
                    c = AHandler._orch_cache = (now, arch_orchestration.snapshot(limit=3))
                except Exception as ex:
                    return self._send(200, {"error": str(ex)[:200]})
            return self._send(200, c[1])
        if u.path == "/api/arch/caches":                # every cache layer, both ends of Claude, findings (read-only; cached 60 s)
            import arch_cachestats
            now = time.time()
            c = getattr(AHandler, "_caches_cache", None)
            if not c or now - c[0] > 60 or "fresh" in u.query:
                try:
                    c = AHandler._caches_cache = (now, arch_cachestats.ledger())
                except Exception as ex:
                    return self._send(200, {"layers": [], "findings": [], "error": str(ex)[:200]})
            return self._send(200, c[1])
        if u.path == "/api/arch/cost":                  # the report's C = t / p-bar from this machine's evals and traces (read-only)
            import glob as _glob
            import arch_costmodel as cm
            try:
                ev = cm.from_evals(sorted(_glob.glob(os.path.join(core.data_dir(), "evals", "*.json"))))
                return self._send(200, {"evals": ev, "calls": cm.measure(cm.default_roots()), "p_cheapest": cm.p_cheapest(ev),
                                        "formula": "C = t / p_bar (seconds per accepted change, Eq. 17)"})
            except Exception as ex:
                return self._send(200, {"evals": [], "calls": [], "error": str(ex)[:200]})
        if u.path == "/api/arch/recommend":
            import arch_recommend
            try:
                return self._send(200, arch_recommend.recommend())
            except Exception as ex:
                return self._send(200, {"_error": {"value": None, "why": str(ex)[:200], "basis": "error"}})
        if u.path == "/api/arch/claudecode":
            import arch_claudecode as cc
            q = parse_qs(u.query)
            if q.get("run"):
                return self._send(200, cc.snapshot(q["run"][0], int((q.get("since") or ["0"])[0])))
            return self._send(200, cc.status())
        if u.path == "/api/arch/subagents":
            import arch_claudecode as cc
            return self._send(200, {"subagents": cc.load_subagents(), "models": list(cc.SUB_MODELS[1:]), "starters": cc.STARTERS})
        if u.path == "/api/arch/agents":
            import arch_agents
            q = parse_qs(u.query)
            if q.get("session"):
                return self._send(200, arch_agents.snapshot(q["session"][0], int((q.get("since") or ["0"])[0])))
            try:
                return self._send(200, dict(arch_agents.listing(), sessions=arch_agents.sessions()))
            except Exception as ex:
                return self._send(200, {"error": f"{type(ex).__name__}: {str(ex)[:300]}", "agents": [], "environments": [], "sessions": arch_agents.sessions()})
        if u.path == "/api/arch/chat":
            import arch_chat
            return self._send(200, arch_chat.snapshot(safe_root((parse_qs(u.query).get("root") or [HERE])[0])))
        if u.path == "/api/arch/backlog":
            import arch_autopilot as ap
            return self._send(200, ap.load(safe_root((parse_qs(u.query).get("root") or [HERE])[0])))
        if u.path == "/api/arch/report":
            root = safe_root((parse_qs(u.query).get("root") or [HERE])[0])
            last = core.latest_run(root)
            txt = _read_text(os.path.join(last, "report.md")) if last else ""
            return self._send(200, {"run": os.path.basename(last) if last else None, "report": (txt or "")[-6000:]})
        if u.path == "/api/arch/tune_info":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            return self._send(200, tune_info())
        if u.path == "/api/arch/first_visit":
            # The first-visit path: machine checks, Get ready, which model does which job, and the practice task. Read-only.
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                root = safe_root((parse_qs(u.query).get("root") or [HERE])[0], create_ok=True)
            except ValueError:
                root = HERE
            try:
                hub_port = self.server.server_address[1]
            except Exception:
                hub_port = 8766
            return self._send(200, first_visit_info(root, hub_port))
        if u.path == "/api/arch/code_health":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                root = safe_root((parse_qs(u.query).get("root") or [HERE])[0])
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, code_health(root))
        if u.path == "/api/arch/receipt":
            # One run as a self-contained page. Served as HTML so the browser shows it; it loads nothing else.
            import arch_receipt
            q = parse_qs(u.query)
            try:
                root = safe_root((q.get("root") or [HERE])[0])
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            run = (q.get("run") or [""])[0]
            if run and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run):
                return self._send(400, {"error": "that is not a run id"})
            rd = os.path.join(root, ".arch", "runs", run) if run else arch_receipt.latest_run(root)
            if not rd or not os.path.isdir(rd):
                return self._send(404, {"error": "no such run"})
            body = arch_receipt.build(rd, root).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return None
        if u.path == "/api/arch/evals":
            # The benchmark history, from the reports `eval` saved: every run's pass rate and cost (live and scripted kept apart), and the
            # newest live run task by task. Read-only; nothing is measured here.
            rows = []
            try:
                with open(os.path.join(core.data_dir(), "evals.jsonl"), encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        rows.append({k: r.get(k) for k in ("ts", "label", "fake", "quick", "n", "pass_rate", "not_sure", "tokens", "secs", "per_flow", "path")})
            except OSError:
                pass
            live = next((r for r in reversed(rows) if not r.get("fake") and r.get("n")), None)
            detail = core.load_json(live["path"]) if live and live.get("path") else None
            tasks = [{k: t.get(k) for k in ("id", "flow", "verdict", "secs", "calls", "tokens_in", "tokens_out", "detail")}
                     for t in (detail or {}).get("results") or []]
            for r in rows:
                r.pop("path", None)
            return self._send(200, {"runs": rows[-60:], "live": dict(live or {}, path=None) if live else None, "tasks": tasks,
                                    "changes": (detail or {}).get("task_changes")})
        if u.path == "/api/arch/team":
            # Which installed model this machine would put in each tier, and why -- measured against this card, not looked up by name.
            import arch_port
            plan = arch_port.pick_tiers(say=lambda m: None)
            cfg_ = core.load_config()
            current = {t: (cfg_.get("tiers") or {}).get(t, "") for t in ("router", "worker", "heavy", "draft")}
            models = []
            try:
                models = [{"name": m["name"], "size_gb": m["size_gb"], "eff_b": m["eff_b"], "quant": m["quant"], "family": m["family"],
                           "score": arch_port.quality(m), "moe": m["moe"]} for m in arch_port.facts() if not m["embed"]]
            except Exception:
                models = []
            first_run = not any(current.values())
            return self._send(200, {"plan": plan, "current": current, "models": sorted(models, key=lambda x: -x["score"]),
                                    "first_run": first_run, "text": arch_port.render(plan)})
        if u.path == "/api/arch/machine":
            # The machine map: every part of this computer, what it is measured to do, and which model sits where. Read-only.
            import arch_hw
            import arch_server
            try:
                hw = arch_hw.detect()
                lv = arch_hw.live()
                cfg_ = core.load_config()
                pl = arch_server.plan(cfg_, hw)
                st = {p["tier"]: p for p in arch_server.status(cfg_).get("processes", [])}
                drs = arch_hw.drive_roles(arch_hw.drives())
            except Exception as ex:
                return self._send(200, {"error": f"{type(ex).__name__}: {ex}"})
            bw = arch_hw.load_bandwidth()
            return self._send(200, {
                "cpu": {"name": hw.get("cpu"), "cores": hw.get("cpu_cores"), "perf_cores": hw.get("cpu_perf_cores")},
                "gpu": {"name": hw.get("gpu"), "vram_gb": hw.get("vram_gb"), "used_gb": lv.get("vram_used_gb"), "busy_pct": lv.get("gpu_pct"),
                        "bw": bw.get("gpu_bw")},
                "ram": {"gb": hw.get("ram_gb"), "free_gb": lv.get("ram_free_gb") or hw.get("ram_free_gb"), "bw": bw.get("ram_bw"),
                        "bw_source": bw.get("source") or "estimate"},
                "drives": drs,
                "models": [{"tier": p["tier"], "name": p["name"], "gpu_gb": p.get("est_gpu_gb") or 0, "ram_gb": p.get("ram_gb") or 0,
                            "cpu_only": p.get("ngl") == 0, "resident": p.get("resident"), "why": p.get("why"),
                            "up": bool((st.get(p["tier"]) or {}).get("up")), "on_cpu": bool((st.get(p["tier"]) or {}).get("on_cpu"))}
                           for p in pl.get("processes") or []],
                "notes": pl.get("notes") or []})
        if u.path == "/api/arch/flowmap":
            # How a workflow runs here: its steps, the model each one uses on this machine, the check that decides it, its budget.
            import arch_dataflow
            fid = (parse_qs(u.query).get("flow") or [""])[0]
            if not re.fullmatch(r"[a-z0-9_]{1,40}", fid or ""):
                return self._send(400, {"error": "flow must be a workflow id like bugfix"})
            return self._send(200, arch_dataflow.flow_map(fid))
        if u.path == "/api/arch/drives":
            # Every drive, its bus, size, free space and what it is good for here (models, scratch, archive, or nothing if it looks
            # like a fake-capacity drive). Reads only; nothing is written or measured.
            import arch_hw
            try:
                drs = arch_hw.drive_roles(arch_hw.drives())
            except Exception as ex:
                return self._send(200, {"drives": [], "error": f"{type(ex).__name__}: {ex}"})
            return self._send(200, {"drives": drs, "models_dir": arch_hw.models_dir()})
        if u.path == "/api/arch/gpu":
            # Is the graphics card working, and is any running model spilling into RAM? Live numbers (nvidia-smi) plus, for each running
            # model server, how the plan split it between the card and RAM. The plan reads model files, so it is kept for a minute.
            import arch_hw
            import arch_server
            now = time.time()
            if now - _GPU_PLAN.get("t", 0) > 60:
                try:
                    cfg_ = core.load_config()
                    pl = arch_server.plan(cfg_)
                    _GPU_PLAN.update(t=now, notes=pl.get("notes") or [], tiers=[
                        {"tier": p["tier"], "name": p["name"], "gpu_gb": p.get("est_gpu_gb"), "ram_gb": p.get("ram_gb"),
                         "full_gpu": p.get("full_gpu"), "cpu_only": p.get("ngl") == 0, "moe_cpu_layers": p.get("moe_cpu_layers", 0)}
                        for p in pl.get("processes") or []])
                except Exception as ex:                       # the strip must never break on a machine without llama-server
                    _GPU_PLAN.update(t=now, notes=[f"server plan unavailable: {type(ex).__name__}"], tiers=[])
            try:                                              # which servers are up is read every time: only the plan is kept
                up = {p["tier"] for p in arch_server.status(core.load_config()).get("processes", []) if p.get("up")}
            except Exception:
                up = set()
            return self._send(200, dict(_live_sample(arch_hw), tiers=[dict(t, up=t["tier"] in up) for t in _GPU_PLAN.get("tiers", [])],
                                        notes=_GPU_PLAN.get("notes", [])))
        if u.path == "/api/arch/queue":
            import arch_supervisor as sv
            return self._send(200, {"jobs": sv.listing(limit=60), "counts": sv.counts(), "spent_today": sv.spent_today(),
                                    "stopped": sv.stopped(), "budget": sv.budget_left() or "", "eta": sv.eta()})
        if u.path == "/api/arch/rpresets":
            return self._send(200, rpresets_view())
        if u.path == "/api/arch/bgjobs":
            return self._send(200, bgjobs_view())
        if u.path == "/api/arch/hooks":
            try:
                return self._send(200, hooks_view(safe_root((parse_qs(u.query).get("root") or [HERE])[0])))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/arch/usage":
            import arch_usage
            w = (parse_qs(u.query).get("window") or ["day"])[0]
            return self._send(200, arch_usage.usage(w if w in ("day", "week") else "day"))
        if u.path == "/api/arch/ui/registry":
            return self._send(200, ui_registry())
        if u.path == "/api/arch/lessons":
            return self._send(200, _ttl("lessons", 20, lessons_view))
        if u.path == "/api/arch/overnight":
            return self._send(200, _ttl("overnight", 8, overnight_view))
        if u.path == "/api/arch/datanight":
            return self._send(200, datanight_view(parse_qs(u.query)))
        if u.path == "/api/arch/trainworker":
            return self._send(200, trainworker_view())
        if u.path == "/api/arch/eta":
            # How long is left: the queue, the running job's own progress line, or a preview for a planned data night (arch_eta).
            import arch_eta
            q = parse_qs(u.query)
            out = {"job": job_eta()}
            try:
                import arch_supervisor as sv
                out["queue"] = sv.eta()
            except Exception:
                out["queue"] = {}
            if q.get("preview"):
                try:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("train_traces", os.path.join(HERE, "tools", "train_traces.py"))
                    tt = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(tt)
                    until = (q.get("until") or [""])[0]
                    t_end = tt.until_epoch(until) if until else None
                    mx = int((q.get("max") or ["0"])[0] or 0)
                    total = mx or int((q.get("total") or ["105"])[0] or 105)
                    out["preview"] = arch_eta.estimate(0, total, family="distributed", model=(q.get("worker") or [""])[0] or None, until=t_end)
                except ValueError as ex:
                    return self._send(400, {"error": str(ex)})
            return self._send(200, out)
        if u.path == "/api/arch/effective":
            import arch_effective
            role = (parse_qs(u.query).get("role") or ["generator"])[0]
            if role not in roles.ROLES:
                return self._send(400, {"error": "no such role"})
            return self._send(200, dict(arch_effective.effective_settings(core.load_config(), role), roles=sorted(roles.ROLES)))
        if u.path == "/api/arch/commands":
            import arch_commands
            try:
                root = safe_root((parse_qs(u.query).get("root") or [HERE])[0])
            except ValueError:
                root = HERE
            return self._send(200, {"commands": commands_view(root)})
        if u.path == "/api/arch/events":
            # What the running job is doing, as structured records rather than console text: one plan step, one tool call, one check
            # result, one verdict per event. `since` is the cursor the previous reply handed back.
            try:
                since = int((parse_qs(u.query).get("since") or ["0"])[0])
            except ValueError:
                since = 0
            return self._send(200, mh.JOB.event_feed(since))
        if u.path == "/api/arch/events/stream":
            return self._event_stream(parse_qs(u.query))
        if u.path == "/api/arch/blueprint_kit":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                return self._send(200, blueprint_kit((parse_qs(u.query).get("flow") or [""])[0]))
            except Exception as ex:
                return self._send(200, {"error": f"{type(ex).__name__}: {str(ex)[:200]}", "kinds": list(BLUEPRINT_KINDS), "tiers": list(BLUEPRINT_TIERS),
                                        "gates": [], "prompts": [], "roles": [], "taken": []})
        if u.path == "/api/arch/costs":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            try:
                days = max(1, min(90, int((parse_qs(u.query).get("days") or ["7"])[0])))
            except ValueError:
                days = 7
            return self._send(200, costs_summary(days))
        if u.path in ("/api/arch/plugins", "/api/arch/telemetry/summary", "/api/arch/telemetry/events", "/api/arch/telemetry/timeline",
                      "/api/arch/squad/runs"):
            # Plugins, Telemetry and Squad runs pages: read-only, short-TTL cached, "available: false" when a module is missing
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            q = parse_qs(u.query)
            one = lambda k, d="": (q.get(k) or [d])[0]
            if u.path == "/api/arch/plugins":
                return self._send(200, plugins_list())
            if u.path == "/api/arch/squad/runs":
                return self._send(200, squad_runs())
            if u.path == "/api/arch/telemetry/timeline":
                return self._send(200, telemetry_timeline(one("run")))
            if u.path == "/api/arch/telemetry/summary":
                try:
                    w = max(60, min(30 * 86400, int(one("window", "3600"))))
                except ValueError:
                    w = 3600
                return self._send(200, telemetry_summary(w))
            try:
                since = float(one("since")) if one("since") else None
            except ValueError:
                since = None
            kinds = [k for k in one("kinds").split(",") if re.match(r"^[a-z0-9_.]{1,40}$", k)] or None
            run_id = one("run") if _NAME_RE.match(one("run")) else None
            return self._send(200, telemetry_events(since, run_id, kinds))
        if u.path in ("/api/arch/squad/agents", "/api/arch/squad/workflows", "/api/arch/select/why"):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            return self._send(200, {"/api/arch/squad/agents": squad_agents, "/api/arch/squad/workflows": squad_workflows,
                                    "/api/arch/select/why": select_why}[u.path]())
        if u.path in ("/api/arch/selfimprove/sessions", "/api/arch/selfimprove/session", "/api/arch/selfimprove/suggest"):
            # Improve itself (Machine > Improve itself): read-only views of the loop's sessions (arch_selfimprove.loop)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            import arch_selfimprove as si
            try:
                if u.path.endswith("/sessions"):
                    return self._send(200, {"sessions": si.sessions(), "history": si.history()[-20:]})
                if u.path.endswith("/suggest"):
                    return self._send(200, si.suggest())
                return self._send(200, si.load_session((parse_qs(u.query).get("id") or [""])[0]))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/arch/crew_schedule":
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            return self._send(200, crew_schedule_list())
        if u.path in ("/api/arch/fleet", "/api/arch/map", "/api/arch/map_sources", "/api/arch/logbook", "/api/arch/library",
                      "/api/arch/monitor"):
            # The agents' workspace pages. Read-only; each module is optional and every call is guarded.
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            q = parse_qs(u.query)
            one = lambda k: (q.get(k) or [""])[0]
            if u.path == "/api/arch/fleet":
                return self._send(200, fleet_info(one("id") or None))
            if u.path == "/api/arch/map":
                return self._send(200, map_graph(one("kind"), one("id"), one("root") or None))
            if u.path == "/api/arch/map_sources":
                return self._send(200, map_sources(one("root") or None))
            if u.path == "/api/arch/logbook":
                return self._send(200, logbook_info(one("q") if "q" in q else None))
            if u.path == "/api/arch/library":
                return self._send(200, library_info(one("name") or None))
            return self._send(200, monitor_info())
        return super().do_GET()

    def _event_stream(self, q):
        """The same events, pushed as they happen (Server-Sent Events), so the live panel does not have to poll.

        Written by hand rather than through _send, because that helper always sets a Content-Length and writes the body in one go --
        the opposite of a stream. The whole loop is inside its own try/except: once the headers are out, the base handler's error path
        would write a second response into a stream that has already started.
        """
        try:
            since = int((q.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
        except OSError:
            return
        import time as _t
        quiet, started = 0, _t.time()
        try:
            while _t.time() - started < 3600:                      # a browser reconnects on its own; this keeps one socket bounded
                feed = mh.JOB.event_feed(since)
                if feed["events"] or feed["next"] != since:
                    since = feed["next"]
                    self.wfile.write(b"data: " + json.dumps(feed).encode("utf-8") + b"\n\n")
                    quiet = 0
                else:
                    quiet += 1
                    if quiet >= 20:                               # about every 5 seconds: a comment frame keeps the connection open
                        self.wfile.write(b": still here\n\n")
                        quiet = 0
                _t.sleep(0.25)
        except (OSError, BrokenPipeError, ValueError):
            return                                                 # the page navigated away or was closed: nothing to report

    def route(self, path, b):
        if path == "/api/arch/ready":
            argv = ["architect.py", "ready", "--root", safe_root(b.get("root") or HERE, create_ok=True)]
            if b.get("yes"):
                argv.append("--yes")                         # agrees to downloading ONE first model when none is installed
            mh.JOB.start("Get ready", argv)
            return {"ok": True}
        if path == "/api/arch/tune":
            argv = ["architect.py", "tune"]
            if b.get("undo"):
                argv.append("--undo")
            elif b.get("preview"):
                argv.append("--no-apply")
            elif b.get("deep"):
                argv.append("--deep")
            mh.JOB.start("Undo the last tune" if b.get("undo") else "Preview tune (changes nothing)" if b.get("preview") else "Tune for this machine", argv)
            return {"ok": True}
        if path == "/api/arch/ci":
            argv = ["architect.py", "ci", "--root", safe_root(b.get("root") or HERE, create_ok=True), "--run-local"]
            if b.get("init"):
                argv.append("--init")
            mh.JOB.start("Verify CI/CD pipeline", argv)
            return {"ok": True}
        if path == "/api/arch/resume":
            mh.JOB.start("Resume the interrupted run", ["architect.py", "resume", "--root", safe_root(b.get("root") or HERE), "--review", "ask" if b.get("review") != "auto" else "auto"])
            return {"ok": True}
        if path == "/api/arch/optimize":
            import arch_autotune as at
            import arch_hw
            import arch_modelopt
            at._wire()
            try:
                if b.get("undo"):
                    arch_modelopt.undo(say=lambda m: None)
                else:
                    models = b.get("models")
                    if models is not None and not (isinstance(models, list) and all(isinstance(x, str) for x in models)):
                        raise ValueError("models must be a list of installed model names")
                    hw = arch_hw.detect()
                    arch_modelopt.apply(mh.model_list(hw, "f16", 192.0, 50.0), hw, models=models or None, say=lambda m: None)
            except (at.TuneError, mh.OllamaError) as e:
                raise ValueError(getattr(e, "msg", None) or str(e))
            return {"ok": True}
        if path == "/api/arch/livetest":
            mh.JOB.start("Test with my models", ["architect.py", "livetest"] + (["--quick"] if b.get("quick") else []))
            return {"ok": True}
        if path == "/api/arch/hosts":
            import arch_hosts as H
            cfg = mh.read_config()
            if b.get("add"):
                name, addr = str((b["add"] or {}).get("name") or ""), str((b["add"] or {}).get("address") or "")
                if not re.match(r"^[A-Za-z0-9_.-]{1,32}$", name):
                    raise ValueError("Give the machine a short name (letters, digits, dot, dash).")
                cfg.setdefault("hosts", {})[name] = H.normalize(addr)
            if b.get("remove"):
                name = str(b["remove"])
                cfg.get("hosts", {}).pop(name, None)
                for r_ in [r for r, h_ in (cfg.get("role_hosts") or {}).items() if h_ == name]:
                    H.unroute(cfg, r_)
            if b.get("route"):
                rt = b["route"]
                if str(rt.get("role")) not in roles.ROLES:
                    raise ValueError("That is not a role.")
                H.route(cfg, str(rt["role"]), str(rt.get("host")), (str(rt["model"]) if rt.get("model") else None))
            if b.get("unroute"):
                H.unroute(cfg, str(b["unroute"]))
            if b.get("bench"):                               # measure one machine you added: speed of a tiny prompt + health
                name = str(b["bench"])
                if name not in (cfg.get("hosts") or {}):
                    raise ValueError("Add the machine first.")
                try:
                    H.bench_host(name, cfg, say=lambda m: None)
                except Exception as ex:
                    return {"ok": False, "error": f"{name} did not answer: {str(ex)[:160]}", "machines": machines_info(cfg)}
            if b.get("apply_plan"):
                for s_ in machines_info(cfg)["plan"]:
                    H.route(cfg, s_["role"], s_["host"], s_["model"])
            mh.write_config(cfg)
            return {"ok": True, "machines": machines_info(cfg)}
        if path == "/api/arch/pull":
            tag = str(b.get("tag") or "")
            if not TAG_RE.match(tag):
                raise ValueError("That does not look like a model tag.")
            if b.get("confirm") is not True:
                raise ValueError("A download needs an explicit confirmation (it can be many GB).")
            mh.JOB.start("Download " + tag, ["architect.py", "pull", tag, "--yes"])
            return {"ok": True}
        if path == "/api/arch/ollama/unload":
            return ollama_unload_request(b)
        if path == "/api/arch/sharpen":
            # a rough request -> a precise one: target files (checked to exist), acceptance checks, a draft test, risks (local by default)
            import arch_augment
            goal = str(b.get("goal") or "").strip()[:4000]
            if not goal:
                raise ValueError("Type what you want first.")
            use = "claude" if b.get("use") == "claude" else "local"
            res = arch_augment.sharpen(safe_root(b.get("root") or HERE), goal, use=use)
            return {"result": res, "summary": arch_augment.summary(res)}
        if path == "/api/arch/claudecode":
            import arch_claudecode as cc
            if b.get("stop"):
                cc.stop(str(b["stop"]))
                return {"ok": True}
            msg = str(b.get("message") or "").strip()[:20000]
            if not msg:
                raise ValueError("Type a task for Claude Code first.")
            root = safe_root(b.get("root") or HERE)
            model = str(b.get("model") or "") or None
            if model and not re.fullmatch(r"claude-[a-z0-9.-]{3,40}|opus|sonnet|haiku|fable", model):
                raise ValueError("unknown model")
            sess = str(b.get("resume") or "") or None
            if sess and not re.fullmatch(r"[0-9a-f-]{20,60}", sess):
                raise ValueError("bad session id")
            return {"ok": True, "run": cc.start(root, msg, str(b.get("mode") or "plan"), model, sess)}
        if path == "/api/arch/subagents":
            import arch_claudecode as cc
            return {"ok": True, "subagents": cc.save_subagents(b.get("subagents"))}
        if path == "/api/arch/agents":
            import arch_agents
            msg = str(b.get("message") or "").strip()[:20000]
            if not msg:
                raise ValueError("Type a task for the agent first.")
            if b.get("session"):
                arch_agents.send(str(b["session"]), msg)
                return {"ok": True, "session": b["session"]}
            aid = str(b.get("agent") or "")
            if not re.fullmatch(r"agent_[A-Za-z0-9]{6,64}", aid):
                raise ValueError("Pick one of your agents.")
            env = str(b.get("environment") or "") or None
            repo = str(b.get("repo") or "").strip() or None
            if repo and not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+(\.git)?", repo):
                raise ValueError("That is not a GitHub repository address.")
            return {"ok": True, "session": arch_agents.start(aid, msg, env, repo)}
        if path == "/api/arch/chat":
            import arch_chat
            root = safe_root(b.get("root") or HERE)
            if b.get("reset"):
                arch_chat.reset(root)
                return {"ok": True}
            text = str(b.get("message") or "").strip()[:4000]
            if not text:
                raise ValueError("Type a message first.")
            use = str(b.get("use") or "")[:120]
            if use.startswith("local:"):                        # only a model this machine has (a tier or an installed tag)
                cfg_now = core.load_config()
                known = set((cfg_now.get("tiers") or {}).values()) | set(core.Engine(root, "chat", "", review="dry", say=lambda m: None, persist=False).installed())
                if use[6:] not in known:
                    raise ValueError("that model is not on this machine")
            elif use.startswith("claude:"):
                if not re.fullmatch(r"claude:claude-[a-z0-9.-]{3,40}", use):
                    raise ValueError("that is not a Claude model name")
            elif use not in ("", "local", "claude"):
                raise ValueError("unknown model choice")
            eff = b.get("effort") if b.get("effort") in ("low", "medium", "high", "xhigh", "max") else None
            csz = b.get("ctx") if b.get("ctx") in ("small", "normal", "large", "max") else None
            if not arch_chat.ask(root, text, use=None if use in ("", "local") else use, effort=eff, ctx=csz, ultra=bool(b.get("ultra")) and use.startswith("claude")):
                raise ValueError("Still answering the last message.")
            return {"ok": True}
        if path == "/api/arch/backlog":
            import arch_autopilot as ap
            root = safe_root(b.get("root") or HERE)
            if b.get("add"):
                it = ap.add(root, str(b["add"])[:300], b.get("flow") or "auto")
                return {"ok": True, "item": it, "backlog": ap.load(root)}
            if b.get("remove"):
                bl = ap.load(root)
                bl["items"] = [i for i in bl["items"] if i["id"] != b["remove"]]
                ap.save(root, bl)
                return {"ok": True, "backlog": bl}
            if b.get("propose"):
                mh.JOB.start("Propose backlog items", ["architect.py", "backlog", "--propose", "--root", root])
                return {"ok": True}
            argv = ["architect.py", "autopilot", "--root", root, "--hours", str(float(b.get("hours") or 1)), "--max", str(int(b.get("max") or 5)),
                    "--review", b.get("review") if b.get("review") in ("auto", "ask", "dry") else "auto"]
            if b.get("propose_more"):
                argv.append("--propose")
            mh.JOB.start("Autopilot", argv)
            return {"ok": True}
        if path == "/api/arch/index":
            argv = ["architect.py", "index", "--root", safe_root(b.get("root") or HERE)]
            if b.get("rebuild"):
                argv.append("--rebuild")
            if b.get("query"):
                argv += ["--query", str(b["query"])[:500]]
            mh.JOB.start("Retrieval index", argv)
            return {"ok": True}
        if path == "/api/arch/scan":
            argv = ["architect.py", "scan", "--root", safe_root(b.get("root") or HERE)]
            if b.get("run_tests"):
                argv.append("--run-tests")
            mh.JOB.start("Scan project", argv)
            return {"ok": True}
        if path == "/api/arch/self_improve":
            goal = str(b.get("goal") or "").strip()
            if not goal:
                raise ValueError("Say what to improve first.")
            argv = ["architect.py", "self-improve", goal, "--review", "auto"]
            if b.get("promote_if_gates_pass") is True:
                argv.append("--yes")
            mh.JOB.start("Self-improve: " + goal[:50], argv)
            return {"ok": True}
        if path in ("/api/arch/selfimprove/start", "/api/arch/selfimprove/again"):
            argv = ["architect.py", "self-improve", "--rounds", str(max(1, min(10, int(b.get("rounds") or (1 if path.endswith("again") else 3)))))]
            if path.endswith("again"):
                sid = str(b.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", sid):
                    raise ValueError("bad session id")
                argv += ["--resume", sid]
            else:
                goal = str(b.get("goal") or "").strip()[:2000]
                if goal:
                    argv += ["--goal", goal]
            if b.get("fake") is True:
                argv.append("--fake")
            mh.JOB.start("Improve itself" + (": " + str(b.get("goal"))[:50] if b.get("goal") else ""), argv)   # never --yes: promote is its own button
            return {"ok": True}
        if path == "/api/arch/selfimprove/promote":
            import arch_selfimprove as si
            if b.get("confirm") is not True:
                raise ValueError("Promotion needs your confirmation.")
            try:
                return {"ok": True, "session": si.promote_session(str(b.get("id") or ""), confirm=True, say=lambda m: None)}
            except PermissionError as e:
                raise ValueError(str(e))
        if path == "/api/arch/selfimprove/discard":
            import arch_selfimprove as si
            return {"ok": True, "session": si.discard_session(str(b.get("id") or ""))}
        if path == "/api/arch/selfimprove/rollback":
            import arch_selfimprove as si
            return {"ok": True, "result": si.rollback_session(say=lambda m: None)}
        if path == "/api/arch/self_rollback":
            mh.JOB.start("Roll back last self-improvement", ["architect.py", "self-rollback"])
            return {"ok": True}
        if path == "/api/arch/assign":
            cfg = mh.read_config()
            cfg.setdefault("roles", {})
            for r, m in (b.get("roles") or {}).items():
                if r in roles.ROLES:
                    cfg["roles"][r] = str(m or "")
            if b.get("overrides") is not None:
                cfg["role_overrides"] = _validated_overrides(b["overrides"], set(roles.ROLES), cfg.get("role_overrides") or {})
            mh.write_config(cfg)
            return {"ok": True}
        if path == "/api/arch/model_override":
            cfg = mh.read_config()
            cfg.setdefault("model_overrides", {})
            model, field, value = str(b.get("model") or "").strip(), str(b.get("field") or ""), b.get("value")
            if not model:
                raise ValueError("no model named")
            try:
                o = params.apply_override(dict(cfg["model_overrides"].get(model) or {}), field, value)
            except params.ValidationError as e:
                raise ValueError(str(e))
            if o:
                cfg["model_overrides"][model] = o
            else:
                cfg["model_overrides"].pop(model, None)
            mh.write_config(cfg)
            return {"ok": True, "model_overrides": cfg["model_overrides"]}
        if path == "/api/arch/settings":
            patch = {k: v for k, v in (b.get("settings") or {}).items() if v not in ("", None)}
            cleared = [k for k, v in (b.get("settings") or {}).items() if v in ("", None)]
            try:
                coerced = params.validate_settings_patch(patch)
                for k in cleared:                                  # clearing (blank/None) needs no coercion, just a known path
                    if not params.is_known_setting(k):
                        raise params.ValidationError(f"'{k}' is not a recognised setting")
            except params.ValidationError as e:
                raise ValueError(str(e))
            cfg = mh.read_config()
            for path_, v in coerced.items():
                d = cfg
                parts = path_.split(".")
                for part in parts[:-1]:
                    d = d.setdefault(part, {})
                d[parts[-1]] = v
            for path_ in cleared:
                d, parts = cfg, path_.split(".")
                for part in parts[:-1]:
                    d = d.get(part)
                    if not isinstance(d, dict):
                        d = None
                        break
                if isinstance(d, dict):
                    d.pop(parts[-1], None)
            mh.write_config(cfg)
            return {"ok": True, "config": core.deep_merge(core.DEFAULTS, cfg)}
        if path == "/api/arch/doctor_fix":
            try:
                root = safe_root(b.get("root") or HERE, create_ok=True)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            import arch_doctor as doctor
            try:
                hub_port = self.server.server_address[1]
            except Exception:
                hub_port = 8766
            return self._send(200, doctor.fix(root, hub_port))
        if path == "/api/arch/rpreset":
            return rpreset_change(b)
        if path == "/api/arch/preset":
            name = b.get("name")
            try:
                coerced = params.apply_preset(name)
            except (KeyError, params.ValidationError) as e:
                raise ValueError(str(e))
            cfg = mh.read_config()
            for path_, v in coerced.items():
                d = cfg
                parts = path_.split(".")
                for part in parts[:-1]:
                    d = d.setdefault(part, {})
                d[parts[-1]] = v
            mh.write_config(cfg)
            return {"ok": True, "applied": name, "settings": coerced, "config": core.deep_merge(core.DEFAULTS, cfg)}
        if path == "/api/arch/practice":
            # The first-visit practice task: a two-file project in Architect's own data folder with one small bug, fixed by the normal
            # flow with the normal checks. The files are rewritten first, so it always starts from the same bug; no folder of yours is used.
            if b.get("start") is False:
                # create-only: Home's "Use the practice project" just fills the folder field; no run starts.
                if mh.JOB.running():
                    return {"ok": True, "root": practice_root(), "goal": PRACTICE_GOAL, "started": False}
                return {"ok": True, "root": practice_prepare(), "goal": PRACTICE_GOAL, "started": False}
            if mh.JOB.running():
                raise RuntimeError("A job is already running. Let it finish (or stop it) before the practice task.")
            root = practice_prepare()
            argv = ["architect.py", "run", PRACTICE_GOAL, "--root", root, "--review", "auto"]
            if "bugfix" in fl.FLOWS:
                argv += ["--flow", "bugfix"]
            mh.JOB.start("Practice task: fix slugify", argv)
            return {"ok": True, "root": root, "goal": PRACTICE_GOAL, "epoch": mh.JOB.epoch}
        if path == "/api/arch/apply_recommended":
            ranked, plan = roles.recommend(mh.build_state().get("models", []))
            cfg = mh.read_config()
            cfg.setdefault("roles", {}).update(plan["assignment"])
            mh.write_config(cfg)
            return {"ok": True, "assignment": plan["assignment"]}
        if path == "/api/arch/portfolio":
            return {"ok": True, "apps": save_portfolio(b.get("apps"))}
        if path == "/api/arch/route":
            goal = str(b.get("goal") or "").strip()
            if not goal:
                raise ValueError("Type a goal first.")
            root = safe_root(b.get("root") or HERE, create_ok=True)
            if not os.path.isdir(root):
                import tempfile
                root = tempfile.mkdtemp(prefix="arch_route_")          # a folder that does not exist yet is an empty project
            e = core.Engine(root, "route", goal, core.load_config(root), persist=False, say=lambda m: None)
            flow, doms, why = fl.route(e, goal, None)
            return {"flow": flow, "domains": doms, "why": why}
        if path == "/api/arch/run":
            goal = str(b.get("goal") or "").strip()
            flow = b.get("flow") if b.get("flow") in fl.FLOWS else None
            if not goal and flow != "new":
                raise ValueError("Type a goal first.")
            argv = ["architect.py", "run", goal or "New project"]
            if flow:
                argv += ["--flow", flow]
            if b.get("app"):
                argv += ["--app", str(b["app"])]
            else:
                argv += ["--root", safe_root(b.get("root") or HERE, create_ok=True)]
            argv += ["--review", b.get("review") if b.get("review") in ("ask", "auto", "dry") else "ask"]
            doms = [d for d in (b.get("domains") or []) if d in fl.DOMAINS]
            if doms:
                argv += ["--domains", ",".join(doms)]
            if b.get("template") in templates.TEMPLATES:
                argv += ["--template", b["template"]]
            if str(b.get("name") or "").strip():
                argv += ["--name", str(b["name"]).strip()[:80]]
            ads = [a for a in (b.get("addons") or []) if a in templates.ADDONS]
            if ads:
                argv += ["--addons", ",".join(ads)]
            if b.get("then_build") is False:
                argv += ["--no-build"]
            if b.get("autofix"):
                argv += ["--autofix"]
            rid = str(b.get("recipe") or "")
            if rid:
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", rid):
                    raise ValueError("that is not a recipe id")
                if _cli_has("--recipe"):            # run_flow writes params.recipe into run.json; the flag arrives with the CLI patch
                    argv += ["--recipe", rid]
            opts = {k: v for k, v in (b.get("options") or {}).items() if v not in ("", None) and params.is_known_setting(k)}
            if opts:
                try:
                    coerced = params.validate_settings_patch(opts)
                except params.ValidationError as e:
                    raise ValueError(str(e))
                for k, v in coerced.items():
                    argv += ["--set", f"{k}={json.dumps(v)}"]
            mh.JOB.start("Architect: " + (goal or "new project")[:60], argv)
            return {"ok": True}
        if path == "/api/arch/serve":
            root = safe_root(b.get("root") or HERE, create_ok=True)
            action = b.get("action")
            if action == "start":
                out = runner.start(root, b.get("cmd") or None, network=b.get("network") or None)
            elif action == "stop":
                out = runner.stop(root)
            else:
                raise ValueError("action must be 'start' or 'stop'")
            out["default_cmd"] = runner.default_cmd(root)
            return out
        if path == "/api/arch/team":
            # Use the recommended team, or set one tier by hand. Either way the old choice is saved so it can be put back.
            import arch_port
            if b.get("apply"):
                plan = arch_port.pick_tiers(say=lambda m: None)
                return {"ok": True, **arch_port.apply_plan(plan)}
            if isinstance(b.get("set"), dict):
                import model_hub as mh_
                raw = mh_.read_config()
                before = dict(raw.get("tiers") or {})
                for t, m in b["set"].items():
                    if t not in ("router", "worker", "heavy", "draft"):
                        raise ValueError(f"'{t}' is not a tier")
                    raw.setdefault("tiers", {})[t] = str(m or "")
                mh_.write_config(raw)
                return {"ok": True, "before": before, "after": raw["tiers"]}
            raise ValueError("say apply, or set")
        if path == "/api/arch/queue":
            # The supervisor's queue: add a task, cancel one that has not started, clear finished ones, or the kill switch.
            import arch_supervisor as sv
            if b.get("add"):
                a = b["add"] if isinstance(b["add"], dict) else {"goal": b["add"]}
                cfg_ = core.load_config()
                svc = cfg_.get("supervisor") or {}
                jid = sv.add(str(a.get("goal") or ""), safe_root(a.get("root") or HERE), str(a.get("flow") or ""),
                             float(a.get("minutes") or svc.get("task_minutes", 15)), int(svc.get("max_attempts", 3)), int(a.get("priority") or 0))
                return {"ok": True, "id": jid}
            if b.get("cancel") is not None:
                return {"ok": sv.cancel(int(b["cancel"]))}
            if b.get("clear"):
                return {"ok": True, "removed": sv.clear()}
            if "stop" in b:
                return {"ok": True, "stopped": sv.stop(bool(b["stop"]))}
            raise ValueError("say add, cancel, clear or stop")
        if path == "/api/arch/datanight":
            # A data night: the training tasks on the LOCAL models until an end time, then the dataset export (tools/train_traces.py).
            return datanight_start(b)
        if path == "/api/arch/datanight/stop":
            return datanight_stop()
        if path == "/api/arch/trainworker":
            return trainworker_start(b)
        if path == "/api/arch/supervise":
            hours = max(0.05, min(24.0, float(b.get("hours") or 1)))
            mh.JOB.start(f"Supervisor ({hours:g} h)", ["architect.py", "supervise", "--hours", str(hours), "--max", str(int(b.get("max") or 20))])
            return {"ok": True}
        if path == "/api/arch/command":
            # A slash command expands to words and then takes exactly the same path as anything typed by hand: same flows, same budgets,
            # same checks. Nothing in a command file can run a program.
            import arch_commands
            root = safe_root(b.get("root") or HERE)
            ex = arch_commands.expand(str(b.get("text") or ""), root)
            if not ex["ok"]:
                return {"ok": False, "why": ex["why"], "suggestions": ex.get("suggestions") or []}
            argv = ["architect.py", "run", ex["goal"], "--root", root, "--review", "auto" if b.get("review") == "auto" else "ask"]
            if ex["flow"]:
                argv += ["--flow", ex["flow"]]
            mh.JOB.start(f"/{ex['name']}", argv)
            return {"ok": True, "name": ex["name"], "flow": ex["flow"], "goal": ex["goal"]}
        if path == "/api/arch/redirect":
            # Changing your mind mid-run, without losing what has been done. The message is appended to the run's own inbox and the run
            # is asked to stop between calls, so it finishes the request it is in the middle of and then reports why it stopped.
            root = safe_root(b.get("root") or HERE)
            run = str(b.get("run") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run):
                raise ValueError("that is not a run id")
            msg = str(b.get("message") or "").strip()[:2000]
            if not msg:
                raise ValueError("say what to do instead")
            d = os.path.join(root, ".arch", "runs", run)
            if not os.path.isdir(d):
                raise ValueError("there is no run by that name")
            with open(os.path.join(d, "inbox.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "message": msg}) + "\n")
            if b.get("stop", True):
                with open(os.path.join(root, ".arch", "STOP"), "w", encoding="utf-8") as f:
                    f.write(msg[:200])
            return {"ok": True, "run": run, "message": msg, "stopping": bool(b.get("stop", True))}
        if path == "/api/arch/recipes/fill":
            return recipes_fill(b)
        if path == "/api/arch/review/accept":
            return review_decide(b)
        if path == "/api/arch/review/hunks":
            return review_hunks(b)
        if path == "/api/arch/git/commit":
            return git_commit_run(b)
        if path == "/api/arch/onboard":
            return onboard_project(b)
        if path == "/api/arch/bgjobs/stop":
            return bgjobs_stop(b)
        if path == "/api/arch/hooks":
            return hooks_change(b)
        if path == "/api/arch/bundle":
            import arch_bundle
            import arch_receipt
            root = safe_root(b.get("root") or HERE)
            run = str(b.get("run") or "")
            if run and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run):
                raise ValueError("that is not a run id")
            rd = os.path.join(root, ".arch", "runs", run) if run else arch_receipt.latest_run(root)
            if not rd or not os.path.isdir(rd):
                raise ValueError("there is no run by that name")
            return {"path": arch_bundle.bundle(rd)}
        if path == "/api/arch/plan":
            return plan_save(b)
        if path == "/api/arch/undo":
            mh.JOB.start("Undo last run", ["architect.py", "undo", "--root", safe_root(b.get("root") or HERE)])
            return {"ok": True}
        if path == "/api/arch/flow_save":
            return save_blueprint(b)
        if path == "/api/arch/crew_schedule":
            return crew_schedule_change(b)
        if path == "/api/arch/fleet":
            return fleet_change(b)
        if path == "/api/arch/logbook":
            if not b.get("ingest"):
                raise ValueError("say ingest")
            return logbook_ingest()
        if path == "/api/arch/library":
            return library_fill(b)
        if path == "/api/arch/squad/agent":
            return squad_save_agent(b)
        if path == "/api/arch/squad/run":
            return squad_run(b)
        if path == "/api/arch/squad/resume":
            return squad_resume(b)
        if path.startswith("/api/arch/plugins/"):
            return plugins_change(path.rsplit("/", 1)[1], b)
        if path == "/api/arch/select/apply":
            return select_apply(b)
        if path == "/api/arch/select/undo":
            mh.JOB.start("Undo the last model choice", ["architect.py", "select", "--undo"])
            return {"ok": True}
        if path == "/api/arch/select/screen":
            return select_screen(b)
        if path == "/api/arch/token_preview":
            return token_preview(b)
        return super().route(path, b)


mh.Handler = AHandler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    if not a.no_browser:
        import arch_launch
        if arch_launch.open_existing(a.port):               # already open: bring it up instead of starting a second copy
            return 0
    if (core.load_config().get("behavior") or {}).get("record_live", "on") == "on":
        import arch_hw
        import arch_server

        def servers_up():
            return {"servers": sorted(t for t, r in (arch_server.load_state().get("processes") or {}).items()
                                      if r.get("pid") and arch_server._alive(r["pid"]))}
        _keep_recorder_samples(arch_hw)                      # /api/arch/gpu reuses these samples instead of measuring again
        arch_hw.start_recorder(os.path.join(core.data_dir(), "live.log"), extra=servers_up)
        try:                                               # Settings > Server > Free memory when idle (0 = keep models loaded)
            import arch_server as _asv
            _asv.start_idle_sweeper(core.load_config, say=print)
        except Exception:
            pass   # a freeze then leaves evidence
    start_crew_scheduler()
    start_notify_watch()
    try:                                                   # closing the hub closes what the app started (behavior.close_on_exit)
        import arch_cleanup
        arch_cleanup.install_exit_hook(core.load_config, say=print)
    except Exception:
        pass
    return mh.serve(a.port, not a.no_browser)


_JOB_ETA = re.compile(r"^\s*(\d+/\d+, (?:~.+ left.*|about \d+ more fit.*|finished))\s*$", re.M)


def job_eta():
    """The newest ETA line the running job printed (eval / data night / supervisor progress), or ''."""
    try:
        with mh.JOB.lock:
            buf, running = mh.JOB.buf[-20000:], mh.JOB.running()
        m = _JOB_ETA.findall(buf)
        if not m:
            m = re.findall(r"\[supervisor\] queue: (.+?)  \(", buf)
        return {"line": m[-1] if m else "", "running": running, "title": mh.JOB.title}
    except Exception:
        return {"line": "", "running": False, "title": ""}


# jobs whose own process already notifies at its end (they know more than "it exited")
_SELF_NOTIFYING = ("Supervisor", "Data night")


def start_notify_watch(poll=3.0):
    """A desktop notification when a hub job ends (a run, a self-improve round, ...): arch_notify, gated by behavior.notify."""
    def loop():
        seen = None
        while True:
            time.sleep(poll)
            try:
                ended, title, code = mh.JOB.ended, mh.JOB.title or "", mh.JOB.code
                if ended and ended != seen:
                    if seen is not None or time.time() - ended < 2 * poll:
                        if not title.startswith(_SELF_NOTIFYING):
                            import arch_notify
                            arch_notify.notify("Architect: " + (title or "job")[:60] + " finished",
                                               "it ended normally" if code == 0 else f"it ended with exit code {code}")
                    seen = ended
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name="notify-watch").start()


def start_crew_scheduler():
    """The Crew page's scheduled agent runs fire while the hub is up. Missing module or function: the hub starts anyway."""
    try:
        import arch_agents
        arch_agents.start_scheduler()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
