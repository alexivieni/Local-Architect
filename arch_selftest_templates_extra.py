"""Self-tests for arch_templates_extra.py: the eight local-AI / distributed-systems starters.

Each starter must be registered, render with every placeholder filled, compile, import only the standard library
(plus pytest in tests), carry no gate-level fake-code finding, be picked by its own wording without stealing the
base templates' wording, and -- the point -- pass its OWN tests when written to a fresh folder (a child pytest per
starter, below-normal work: a few seconds each). No model, no network beyond 127.0.0.1.
    python arch_selftest_templates_extra.py
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


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
        r = subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run():
    results = []          # True / False / None (could not run)

    def check(name, ok, extra=""):
        results.append(None if ok is None else bool(ok))
        label = "NOT RUN" if ok is None else ("PASS" if ok else "FAIL")
        print(f"  {label:7} {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    print("Extra project template checks")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST", "ARCHITECT_NO_CACHE")
    old_env = {k: os.environ.get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="arch_tpl_extra_")
    os.environ.update({"ARCHITECT_DATA_DIR": os.path.join(tmp, "data"), "ARCHITECT_SELFTEST": "1", "ARCHITECT_NO_CACHE": "1"})
    try:
        import arch_templates as T
        import arch_templates_extra as X
        import arch_fakecode

        ids = X.EXTRA_IDS
        check("all eight starters are registered through arch_templates", len(ids) == 8 and all(t in T.TEMPLATES for t in ids),
              [t for t in ids if t not in T.TEMPLATES])
        check("the earlier templates are still there (empty, worker, mcp_server, cli_tool, docs_site)",
              {"empty", "worker", "mcp_server", "cli_tool", "docs_site"} <= set(T.TEMPLATES))
        shape = [t for t in ids if not ({"README.md", "pyproject.toml", "__PKG__/__init__.py", "__PKG__/__main__.py", ".gitignore",
                                         "conftest.py"} <= set(T.TEMPLATES[t]["files"])
                                        and sum(p.startswith("tests/test_") for p in T.TEMPLATES[t]["files"]) >= 2)]
        check("each has a README, pyproject, package, __main__ and at least two test files", not shape, shape)
        listed = {x["id"]: x for x in T.listing()}
        check("each is in listing() with a title, blurb, stack and run command",
              all(listed[t]["title"] and len(listed[t]["blurb"]) > 60 and listed[t]["stack"] and listed[t]["run"] for t in ids))

        rendered = {t: T.render(t, "Night Shift") for t in ids}
        left = [(t, p) for t, files in rendered.items() for p, text in files.items() if "__NAME__" in text or "__PKG__" in text or "__PKG__" in p]
        check("rendering fills every placeholder in paths and contents", not left, left[:5])
        check("the package lands under the python name", all("night_shift/__init__.py" in rendered[t] for t in ids))
        check("each rendered run command is filled in", all("__PKG__" not in T.rendered_run(t, "Night Shift") for t in ids))

        bad_syntax = []
        for t, files in rendered.items():
            for p, text in files.items():
                if p.endswith(".py"):
                    try:
                        compile(text, f"{t}/{p}", "exec")
                    except SyntaxError as e:
                        bad_syntax.append(f"{t}/{p}:{e.lineno} {e.msg}")
        check("every Python file compiles", not bad_syntax, bad_syntax)

        if STDLIB:
            foreign = []
            for t, files in rendered.items():
                for p, text in files.items():
                    if p.endswith(".py") and not bad_syntax:
                        allowed = STDLIB | {"night_shift"} | ({"pytest"} if p.startswith("tests/") else set())
                        foreign += [f"{t}/{p}: {m}" for m in _imports(text) - allowed]
            check("standard library only (pytest only inside tests/)", not foreign, foreign)
        else:
            check("standard library only (needs Python 3.10+ to list the standard library)", None)

        fake = []
        for t, files in rendered.items():
            for p, text in files.items():
                if p.endswith(".py"):
                    fake += [f"{t}/{p}:{f['line']} {f['rule']}" for f in arch_fakecode.scan_text(p, text) if f["severity"] == "high"]
        check("no gate-level fake-code finding (stubs, assertion-free tests, pretend work)", not fake, fake)
        no_net = [f"{t}/{p}" for t, files in rendered.items() for p, text in files.items()
                  if p.endswith(".py") and ("0.0.0.0" in text or "https://" in text)]
        check("nothing binds every interface or reaches the internet", not no_net, no_net)
        readmes = [t for t in ids if "python -m pytest -q" not in rendered[t]["README.md"] or "python -m night_shift" not in rendered[t]["README.md"]]
        check("each README says how to run it and how to test it", not readmes, readmes)

        phrases = {"tool_server": "a local tool server my model can call", "agent_fleet": "run several agents in parallel on one repo",
                   "lease_queue": "a job queue with leases and retries", "api_gateway": "an API gateway with rate limiting",
                   "event_store": "an event-sourced ledger", "model_cli": "a CLI over my local model with caching",
                   "telemetry_collector": "a metrics and trace collector", "fts_rag": "a small RAG over my notes"}
        wrong = {t: T.suggest(g) for t, g in phrases.items() if T.suggest(g)[0] != t}
        check("each starter is picked by its own wording", not wrong, wrong)
        base = {"an MCP server for my notes": "mcp_server", "a local agent with tool calling": "agent_tool",
                "a full-stack web app": "fullstack", "a SQL reporting database": "data_sql", "a REST API": "rest_api",
                "a background worker queue": "worker", "a terminal ui": "tui_app", "hello": "empty"}
        stolen = {g: T.suggest(g)[0] for g, want in base.items() if T.suggest(g)[0] != want}
        check("the base templates keep their own wording", not stolen, stolen)

        # ---- the real proof: each starter's own tests pass in a fresh folder
        if not _has_pytest():
            check(f"the starters' own tests pass ({sys.executable} has no pytest)", None)
        else:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            env.pop("PYTEST_ADDOPTS", None)
            for t in ids:
                d = os.path.join(tmp, t)
                for p, text in rendered[t].items():
                    full = os.path.join(d, p)
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text)
                try:
                    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=d, env=env,
                                       capture_output=True, text=True, timeout=240)
                    tail = (r.stdout + r.stderr)[-1500:]
                    passed = r.returncode == 0 and " passed" in r.stdout
                    check(f"{t}: its own tests pass", passed, tail)
                    if passed and " skipped" in r.stdout:
                        print(f"          note: {t} skipped tests here: {r.stdout.strip().splitlines()[-1]}")
                except subprocess.TimeoutExpired:
                    check(f"{t}: its own tests pass (did not finish in 240 s)", None)
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
        print(f"\n  ALL {n_pass} TEMPLATES_EXTRA CHECKS PASSED")
    else:
        print(f"\n  {n_fail} of {len(results)} TEMPLATES_EXTRA CHECKS FAILED, {n_none} COULD NOT RUN (not a pass)")
    return n_fail == 0 and n_none == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
