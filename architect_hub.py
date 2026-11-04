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
mh.check_deps = lambda: {"pytest": core.has_module("pytest")}


def _flatten(d, p=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and k not in ("roles", "role_overrides", "model_overrides", "tiers"):
            out.update(_flatten(v, p + k + "."))
        else:
            out[p + k] = v
    return out


mh.agent_defaults = lambda: {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in _flatten(core.DEFAULTS).items() if not isinstance(v, dict)}


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
                        "secs": rec.get("secs"), "tok_s": rec.get("tok_s"), "gen_tokens": rec.get("gen_tokens"), "done_reason": rec.get("done_reason")})
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
            return self._send(200, {"calls": trace_listing(root, run_name)})
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
            return self._send(200, dict(arch_hw.live(), tiers=[dict(t, up=t["tier"] in up) for t in _GPU_PLAN.get("tiers", [])],
                                        notes=_GPU_PLAN.get("notes", [])))
        if u.path == "/api/arch/queue":
            import arch_supervisor as sv
            return self._send(200, {"jobs": sv.listing(limit=60), "counts": sv.counts(), "spent_today": sv.spent_today(),
                                    "stopped": sv.stopped(), "budget": sv.budget_left() or ""})
        if u.path == "/api/arch/commands":
            import arch_commands
            try:
                root = safe_root((parse_qs(u.query).get("root") or [HERE])[0])
            except ValueError:
                root = HERE
            return self._send(200, {"commands": arch_commands.summary(root)})
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
            if not arch_chat.ask(root, text, use=None if use in ("", "local") else use):
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
        if path == "/api/arch/undo":
            mh.JOB.start("Undo last run", ["architect.py", "undo", "--root", safe_root(b.get("root") or HERE)])
            return {"ok": True}
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
        arch_hw.start_recorder(os.path.join(core.data_dir(), "live.log"), extra=servers_up)   # a freeze then leaves evidence
    return mh.serve(a.port, not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
