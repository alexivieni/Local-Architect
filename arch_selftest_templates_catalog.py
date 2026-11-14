"""Self-tests for the template catalog (arch_templates_catalog.py) and the file-based starters
(static/templates/src/<id>/, registered by arch_templates_more._load_src).

Every catalog entry: unique id, valid category / colour token / difficulty, a name, summary, description, file tree
and next steps, JSON-able, and a thumbnail at static/templates/<id>.svg that parses as XML, has a <title> and uses no
hex colour or external reference. Then the proof: EVERY template renders into a fresh folder through
catalog.render() and its own tests pass offline. A run where tests skipped because a declared optional dependency
is missing counts the template's full suite as could-not-run (not_sure), never as a pass.
    python arch_selftest_templates_catalog.py
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
OPTIONAL_OK = {"numpy"}          # imported only inside try/except ImportError, with a pure-Python path


def _imports(text):
    out = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def _has_pytest():
    try:
        return subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(None if ok is None else bool(ok))
        label = "NOT RUN" if ok is None else ("PASS" if ok else "FAIL")
        print(f"  {label:7} {name}" + (f"   ({str(extra)[:600]})" if extra and not ok else ""))

    print("Template catalog checks")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST", "ARCHITECT_NO_CACHE")
    old_env = {k: os.environ.get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="arch_tpl_cat_")
    os.environ.update({"ARCHITECT_DATA_DIR": os.path.join(tmp, "data"), "ARCHITECT_SELFTEST": "1", "ARCHITECT_NO_CACHE": "1"})
    try:
        import arch_fakecode
        import arch_templates as T
        import arch_templates_catalog as C
        import arch_templates_more as M

        cat = C.catalog()
        ids = [e["id"] for e in cat]
        check(f"the catalog lists every template ({len(ids)}), at least 30", len(ids) >= 30 and sorted(ids) == sorted(T.TEMPLATES))
        check("ids are unique", len(ids) == len(set(ids)))
        check("every template has hand-written metadata (no fallback entry)", set(C.META) == set(T.TEMPLATES), set(C.META) ^ set(T.TEMPLATES))
        bad = [e["id"] for e in cat if e["category"] not in C.CATEGORIES or e["colour"] not in C.COLOURS or e["difficulty"] not in C.DIFFICULTY]
        check("categories, colour tokens and difficulty are from the allowed sets", not bad, bad)
        thin = [e["id"] for e in cat if not (e["name"] and 15 <= len(e["summary"]) <= 90 and len(e["description"]) > 60
                                             and e["languages"] and e["tags"] and e["tree"] and len(e["next_steps"]) >= 2 and e["run"])]
        check("each entry has a name, one-line summary, longer description, languages, tags, file tree and next steps", not thin, thin)
        try:
            json.loads(json.dumps(cat))
            check("catalog() is JSON-able", True)
        except (TypeError, ValueError) as e:
            check("catalog() is JSON-able", False, e)
        check("at least eight of the ten categories are used", len({e["category"] for e in cat}) >= 8, {e["category"] for e in cat})

        svg_bad = []
        for e in cat:
            path = os.path.join(C.HERE, *e["image"].split("/"))
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                root = ET.fromstring(text)
                if not root.tag.endswith("svg") or root.find("{http://www.w3.org/2000/svg}title") is None:
                    svg_bad.append(f"{e['id']}: no svg root or title")
                if re.search(r"#[0-9a-fA-F]{3,8}\b", text) or re.search(r"(?:href|url)\s*[=(]\s*['\"]?(?:https?:|//)", text):
                    svg_bad.append(f"{e['id']}: hex colour or external reference")
            except (OSError, ET.ParseError) as ex:
                svg_bad.append(f"{e['id']}: {ex}")
        check("every entry has a thumbnail SVG that parses as XML, has a title, themes by currentColor/vars", not svg_bad, svg_bad)

        # ---- the file-based starters: shape, imports, fake-code gate
        new = list(M.FILE_IDS)
        check(f"the file-based starters load from static/templates/src ({len(new)})", len(new) >= 12 and all(t in T.TEMPLATES for t in new))
        rendered = {t: T.render(t, "Night Shift") for t in T.TEMPLATES}
        left = [(t, p) for t, files in rendered.items() for p, text in files.items() if "__NAME__" in text or "__PKG__" in text or "__PKG__" in p]
        check("rendering fills every placeholder in paths and contents (all templates)", not left, left[:5])
        shape = [t for t in new if not ({"README.md", ".gitignore", "conftest.py"} <= set(rendered[t])
                                        and sum(p.startswith("tests/test_") for p in rendered[t]) >= 2)]
        check("each new starter has a README, .gitignore and at least two test files", not shape, shape)
        bad_syntax, foreign, fake = [], [], []
        for t in new:
            allowed = STDLIB | {"night_shift"} | set(T.TEMPLATES[t]["needs"]) | OPTIONAL_OK
            for p, text in rendered[t].items():
                if not p.endswith(".py"):
                    continue
                try:
                    compile(text, f"{t}/{p}", "exec")
                except SyntaxError as ex:
                    bad_syntax.append(f"{t}/{p}:{ex.lineno}")
                    continue
                foreign += [f"{t}/{p}: {m}" for m in _imports(text) - allowed - ({"pytest"} if p.startswith("tests/") else set())]
                fake += [f"{t}/{p}:{f['line']} {f['rule']}" for f in arch_fakecode.scan_text(p, text) if f["severity"] == "high"]
        check("every Python file in the new starters compiles", not bad_syntax, bad_syntax)
        check("standard library only, or a dependency the template declares", not foreign if STDLIB else None, foreign)
        check("no gate-level fake-code finding in the new starters", not fake, fake)
        net = [f"{t}/{p}" for t in new for p, text in rendered[t].items() if p.endswith(".py") and "0.0.0.0" in text]
        check("nothing binds every interface", not net, net)

        phrases = {"chrome_extension": "a chrome extension that saves notes", "vscode_extension": "a vs code extension",
                   "chat_bot": "a discord bot", "ml_train": "train a classifier on my csv", "log_analyzer": "parse my nginx logs",
                   "data_cleaner": "clean up my messy csv", "security_scanner": "scan my code for secrets",
                   "pdf_report": "generate a pdf report", "svg_dashboard": "a KPI dashboard", "auth_service": "an auth service with login",
                   "file_sync": "sync two folders", "home_automation": "home automation with mqtt", "notebook_report": "a jupyter style notebook",
                   "flask_app": "a flask app"}
        wrong = {t: T.suggest(g) for t, g in phrases.items() if T.suggest(g)[0] != t}
        check("each new starter is picked by its own wording", not wrong, wrong)
        base = {"an MCP server for my notes": "mcp_server", "a local agent with tool calling": "agent_tool",
                "a full-stack web app": "fullstack", "a SQL reporting database": "data_sql", "a REST API": "rest_api",
                "a background worker queue": "worker", "a terminal ui": "tui_app", "hello": "empty",
                "a small RAG over my notes": "fts_rag", "an API gateway with rate limiting": "api_gateway"}
        stolen = {g: T.suggest(g)[0] for g, want in base.items() if T.suggest(g)[0] != want}
        check("the earlier templates keep their own wording", not stolen, stolen)

        # ---- catalog.render writes the same files and refuses to overwrite
        d = os.path.join(tmp, "render_check")
        written = C.render("terminal_game", d, {"name": "Night Shift"})
        same = len(written) == len(rendered["terminal_game"]) + 1 and os.path.isfile(os.path.join(d, "CONTRACT.json")) and os.path.isfile(os.path.join(d, "night_shift", "engine.py"))
        try:
            C.render("terminal_game", d, {"name": "Night Shift"})
            refused = False
        except FileExistsError:
            refused = True
        check("catalog.render writes arch_templates.render's files and refuses to overwrite", same and refused)

        # ---- the real proof: every template's own tests, offline, in a fresh folder
        if not _has_pytest():
            check(f"every template's own tests pass ({sys.executable} has no pytest)", None)
        else:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            env.pop("PYTEST_ADDOPTS", None)
            for t in T.TEMPLATES:
                dest = os.path.join(tmp, "t", t)
                C.render(t, dest, {"name": "Night Shift"})
                try:
                    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=dest, env=env,
                                       capture_output=True, text=True, timeout=300)
                except subprocess.TimeoutExpired:
                    check(f"{t}: its own tests pass (did not finish in 300 s)", None)
                    continue
                tail = (r.stdout + r.stderr)[-1200:]
                last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
                passed = r.returncode == 0 and " passed" in last
                skipped = " skipped" in last
                if r.returncode in (0, 5) and skipped and T.TEMPLATES[t]["needs"]:
                    if passed:
                        check(f"{t}: its dependency-free tests pass ({last})", True)
                    check(f"{t}: full suite (needs {', '.join(T.TEMPLATES[t]['needs'])}, not installed here)", None)
                else:
                    why = " / ".join([l_.strip() for l_ in (r.stdout + r.stderr).splitlines() if l_.strip()][-3:])[:400]   # one line: CI keeps only that
                    check(f"{t}: its own tests pass" + (f" ({last})" if passed else f" (rc {r.returncode}: {why})"), passed, tail)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    n_pass = sum(r is True for r in results)
    n_fail = sum(r is False for r in results)
    n_none = sum(r is None for r in results)
    if n_fail == 0 and n_none == 0:
        print(f"\n  ALL {n_pass} TEMPLATE CATALOG CHECKS PASSED")
    else:
        print(f"\n  {n_fail} of {len(results)} TEMPLATE CATALOG CHECKS FAILED, {n_none} COULD NOT RUN (not a pass)")
    return n_fail == 0 and n_none == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
