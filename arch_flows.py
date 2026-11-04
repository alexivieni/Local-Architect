"""Flows: the workflows the Architect can run. Each is a fixed, inspectable sequence of steps (planner ->
contract -> generator -> code checks -> judge), not a free-running agent: fixed workflows are more
predictable, cheaper and easier to test, which is what the current guidance recommends until a free agent
demonstrably does better. Every step records which role/model did it, so runs can become training data.

Flows: design, build, bugfix, refactor.   Router: route().
"""
import abc
import json
import os
import re
import subprocess
import sys
import time

import arch_checks as ck
import arch_fakecode
import arch_core as core
import arch_progress
import arch_schemas
from arch_core import BudgetExceeded, LLMError, LoopDetected

DOMAINS = {"web": "Web front end", "api": "Backend / API", "data": "Data / SQL"}


# ------------------------------------------------------------------------------------------ helpers
def detect_domains(e, goal):
    g = goal.lower()
    d = set()
    if re.search(r"\b(web ?page|website|front.?end|ui|ux|html|css|dashboard|form|responsive|landing|react|component|page)\b", g):
        d.add("web")
    if re.search(r"\b(api|endpoint|rest|openapi|backend|service|server|flask|fastapi|django|express|route|auth|microservice|webhook)\b", g):
        d.add("api")
    if re.search(r"\b(sql|database|schema|table|query|queries|migration|postgres|sqlite|mysql|etl|pipeline|warehouse|analytics|index)\b", g):
        d.add("data")
    files = core.list_files(e.root, e.cfg)
    if not d:
        if any(f.endswith((".html", ".css", ".js")) for f in files):
            d.add("web")
        if any(f.endswith(".py") for f in files):
            d.add("api")
        if any(f.endswith(".sql") for f in files):
            d.add("data")
    return sorted(d) or ["api"]


def gen_role_for(files, domains=None):
    files = list(files or [])
    if files and all(f.lower().endswith((".html", ".htm", ".css", ".js", ".mjs", ".jsx", ".tsx", ".ts")) for f in files):
        return "generator_ui"
    if files and all(f.lower().endswith(".sql") for f in files):
        return "generator_sql"
    if not files and domains == ["web"]:
        return "generator_ui"
    if not files and domains == ["data"]:
        return "generator_sql"
    return "generator"


TESTS_FIRST = """Write the tests for this request BEFORE any code exists. They are the contract the code will be written against.

REQUEST:
{goal}

ACCEPTANCE CRITERIA (every one needs at least one test):
{criteria}

INTERFACE OF THE FINISHED CODE (import exactly these names; the code does not exist yet):
{interface}

Rules: pytest functions only; each expected value must follow from the request (no guessed behaviour, no misspellings); no network, no
files on disk; keep it small and clear. Reply with exactly ONE block:
FILE: {path}
```python
...
```"""


def tests_first(e, goal, sprints, iface_text, path):
    """The planner writes the test file from the acceptance criteria before any code. Seen live (2026-11-04): a 4B writing its own
    tests got the expected values wrong ('Müsic!' -> 'musik', a cut-off 'Python-3.') and spent its retries chasing them. The bigger
    model writes the contract once; the worker then codes against tests it may not edit. -> True when a parsing test file was written."""
    import ast
    criteria = "\n".join(f"- {a.get('text', '')}" for sp in sprints for a in (sp.get("acceptance") or []) if isinstance(a, dict)) or "- the goal works"
    e.say(f"[tests first] the planner writes {path} from the acceptance criteria before any code")
    try:
        r = e.call("planner", TESTS_FIRST.format(goal=goal, criteria=criteria, interface=iface_text, path=path), label="tests-first")
    except Exception as ex:
        e.say(f"    [tests first] skipped: {str(ex)[:120]}")
        return False
    res = e.apply(r["text"], "tests first")
    p = os.path.join(e.root, path)
    ok = any(x["status"] == "applied" and x["rel"] == path for x in res) and os.path.isfile(p)
    if ok:
        try:
            ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError:
            ok = False
    e.mark(r["id"], ok, "tests written first" if ok else "the planner's tests could not be used")
    e.report(f"- Tests first: {'the planner wrote ' + path + ' from the acceptance criteria' if ok else 'not used (fell back to the builder writing tests)'}")
    if not ok and os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass
    return ok


def best_of_n(e, role, prompt, label, k, changed):
    """A stalled step with nobody bigger (or fast enough) to hand it to: ask the same fast model for `k` different attempts, try each
    one on the real files, keep the one that passes the most checks, and put the files back after each try. -> the winning reply,
    which the caller then applies through its normal checks. Small models plus a verifier: repeated sampling with tests is how
    a 4B model gets past a step it keeps getting wrong one way (research: docs/research_levers_20261104.md)."""
    import arch_flows_pro as pro
    e.say(f"    [best of {k}] the step is stuck: asking for {k} different attempts and testing each")
    best, best_score = None, -1
    # the tests that exist now are the judge: they are locked during the rescue, so every attempt has to change the code
    tests_now = [f for f in core.list_files(e.root, e.cfg) if os.path.basename(f).startswith("test_") or f.startswith("tests/")]
    saved_deny = list(e.deny_prefixes)
    e.deny_prefixes = saved_deny + tests_now
    try:
        return _best_of_n_loop(e, role, prompt, label, k, changed, pro, best, best_score, tests_now)
    finally:
        e.deny_prefixes = saved_deny


def _best_of_n_loop(e, role, prompt, label, k, changed, pro, best, best_score, tests_now):
    lock = (" The test files are locked for this attempt (" + ", ".join(tests_now[:4]) + "): change the CODE so they pass.") if tests_now else ""
    code = [f for f in sorted(set(changed)) if f.endswith(".py") and f not in tests_now and os.path.isfile(os.path.join(e.root, f))]
    if code:
        # small models follow a concrete order far better than "change the code": name the file, show it, ask for exactly that block
        cur = core.read_text(os.path.join(e.root, code[0]), 6000)[0]
        lock += (f"\nReply with exactly ONE block, `FILE: {code[0]}`, containing the complete corrected file. Its current content is:\n"
                 f"```python\n{cur}\n```")
    # Temperature alone did not diversify a confident 4B (three byte-identical replies at 0.8, seen live): each attempt gets its own strategy.
    ways = ["Rewrite the failing function from scratch in the simplest way you can, ignoring the current version.",
            "First, in a short comment, trace the failing input through the code step by step to the wrong value; then fix exactly that.",
            "Change only the line or expression that produces the wrong value; keep everything else as it is.",
            "Use a different technique than the current code (for text: a regular expression instead of loops, or the reverse).",
            "Handle the failing case explicitly first, then make sure the earlier passing cases still pass."]
    # a second rescue round on the same step must not resend the first round's exact prompts (the engine's loop guard stops a third
    # identical question); each round starts further along the strategy list and names its round
    rnd = getattr(e, "_bon_rounds", 0)
    e._bon_rounds = rnd + 1
    for i in range(k):
        try:
            r = e.call(role, prompt + f"\n\n(Rescue round {rnd + 1}, attempt {i + 1} of {k}. Strategy: {ways[(rnd * k + i) % len(ways)]}{lock})",
                       label=f"{label}-sample{i + 1}", temperature=0.9)
        except core.LoopDetected:
            break                                                # nothing new to ask: keep the best attempt so far
        cap = pro.capture(e)
        try:
            res = e.apply(r["text"], f"{label} sample {i + 1}")
            applied = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
            v = verify(e, sorted(set(changed) | set(applied))) if applied else {"ok": False, "passed": 0}
            score = 1000 if v.get("ok") else int(v.get("passed") or 0)
        except Exception:
            score = -1
        finally:
            pro.restore(e, cap)
        e._trace({"type": "check", "t": time.time(), "gate": f"best of {k}: attempt {i + 1}", "ok": score >= 1000, "metrics": {"score": score}})
        if score > best_score:
            best, best_score = r, score
        if score >= 1000:
            e.say(f"    [best of {k}] attempt {i + 1} passes every check")
            break
    return best


def _tree_hash(e):
    """A fingerprint of every project file (path, size, modification time): equal means no file changed since it was taken."""
    import hashlib
    h = hashlib.sha1()
    for f in core.list_files(e.root, e.cfg):
        try:
            st = os.stat(os.path.join(e.root, f))
            h.update(f"{f}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            pass
    return h.hexdigest()


def verify(e, files, run_tests=True, targets=None):
    """Code checks on the given files plus the project's tests. -> dict(ok, text, results, tests).

    Every individual check is also written to the trace as its own record, so a watching interface can show "tests passed, linter passed,
    fake-code check failed" while the run is still going, instead of waiting for a report and parsing prose out of it.
    """
    t_v = time.time()
    results = ck.check_files(e.root, files, e.cfg, e.run_dir)
    mine = set(e.changed_files) | set(e.created)                      # legacy code the run did not touch is never failed for being a stub
    results += arch_fakecode.check(e.root, [f for f in files if f in mine], e.cfg["behavior"].get("fake_code", "fail"))
    import arch_deps
    miss = arch_deps.missing(e.root, [f for f in files if f in mine])
    if miss:
        results.append(ck.R("imports are installed", False, arch_deps.feedback(miss)))
    tests = ("none", "")
    broken = [r for r in results if not r["ok"] and (r["name"].endswith(" parses") or r["name"].endswith("is valid JSON"))]
    if broken and run_tests:
        # fast verification: a file that does not even parse fails the step now; running the test suite would only say the same, slowly
        s = ck.summarize(results)
        e._trace({"type": "check", "t": time.time(), "gate": "syntax first", "ok": False, "text": "; ".join(r["name"] for r in broken)[:300]})
        return {"ok": False, "text": s["failures"], "results": results, "tests": ("skipped", "a file does not parse; tests not run"),
                "passed": s["passed"], "total": s["total"]}
    if run_tests:
        fast = related_tests(e, files) if (targets is None and e.cfg["behavior"].get("fast_tests", "on") == "on") else None
        if fast:                                                          # the tests for the changed files first: a failure comes back in seconds
            quick = core.run_tests(e.root, e.cfg, fast)
            if quick[0] == "fail":
                results.append(ck.R("project tests pass", False, f"(related tests first: {', '.join(fast)})\n" + quick[1][-1400:]))
                s = ck.summarize(results)
                return {"ok": False, "text": s["failures"], "results": results, "tests": quick, "passed": s["passed"], "total": s["total"]}
        key = (_tree_hash(e), tuple(targets or ()))
        cache = getattr(e, "_test_cache", None)
        if cache is None:
            cache = e._test_cache = {}
        if key in cache:                                                 # nothing changed since this exact run: same result, no wait
            tests = cache[key]
            e._trace({"type": "check", "t": time.time(), "gate": "test result reused (no file changed)", "ok": tests[0] == "pass"})
        else:
            tests = core.run_tests(e.root, e.cfg, targets)
            if tests[0] in ("pass", "fail"):
                cache[key] = tests
        if tests[0] == "fail":
            results.append(ck.R("project tests pass", False, tests[1][-1400:]))
        elif tests[0] == "pass":
            results.append(ck.R("project tests pass", True))
    try:                                   # the project's own hooks run here, so a blocking one fails the step exactly like a failing test
        import arch_hooks
        hk = arch_hooks.fire(e, "after_apply", files=files, step=e.step_name())
        if hk["blocked"]:
            results.append(ck.R("the project's own hooks pass", False, hk["blocked"]))
    except Exception:
        pass
    s = ck.summarize(results)
    _trace_checks(e, results, t_v)
    return {"ok": s["ok"], "text": s["failures"], "results": results, "tests": tests, "passed": s["passed"], "total": s["total"]}


def _trace_checks(e, results, t0):
    """One record per check that ran, named the way a person would name it. Never raises: reporting must not break a run."""
    try:
        secs = round(time.time() - t0, 1)
        for r in results or []:
            name = getattr(r, "name", None) or (r.get("name") if isinstance(r, dict) else None) or "check"
            ok = getattr(r, "ok", None)
            if ok is None and isinstance(r, dict):
                ok = r.get("ok")
            text = getattr(r, "detail", None) or getattr(r, "text", None) or (r.get("detail") if isinstance(r, dict) else "") or ""
            e._trace({"type": "check", "t": time.time(), "flow": e.flow, "step": e.step_name(), "gate": str(name),
                      "ok": (None if ok is None else bool(ok)), "text": str(text)[:2000], "metrics": {}, "secs": secs})
    except Exception:
        pass


_NAMED = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:py|js|mjs|ts|tsx|jsx|html|css|sql|json|md|ya?ml|toml|sh))\b")


def named_files(goal):
    """Relative file paths a request names outright ("create cart.py ... tests in tests/test_cart.py"). A request that names a file
    means it; these are added to the plan when the planner forgets them, and checked for before a sprint counts as done."""
    out = []
    text = re.sub(r"`[^`]*\s[^`]*`", " ", goal or "")                   # `python -m tool IN.csv OUT.md` is a command example, not a file to create
    for m in _NAMED.finditer(text):
        p = m.group(1).strip("./")
        if os.path.basename(p) in ("__init__.py", "__main__.py") and "/" not in p:
            continue                                                    # "a folder with __init__.py" names no particular path
        if p and ".." not in p and not p.startswith("/") and p not in out and not re.match(r"^\d+(\.\d+)+$", p):
            out.append(p)
    return out


_SPAN = re.compile(r"`([^`\n]{3,200})`")
_FROM = re.compile(r"^from\s+([A-Za-z_][\w.]*)\s+import\s+([A-Za-z_][\w, ]*)$")
_CALL = re.compile(r"^(?:[A-Za-z_]\w*\s*=\s*)?([A-Za-z_]\w*)\(([^()]*)\)$")
_MAIN = re.compile(r"^python3?\s+-m\s+([A-Za-z_][\w.]*)\b")
_METH = re.compile(r"^(?:[A-Za-z_]\w*\s*=\s*)?([A-Za-z_]\w*)\.([A-Za-z_]\w*)\(([^()]*)\)$")
_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\(")


def interface_contract(goal):
    """The interface a request spells out in backticks: `from pkg import Name`, `Name(path)`, `python -m pkg ...`.
    -> {"imports": [(module, name)], "calls": [(name, n_positional, [keywords], text)], "mains": [module]}. Empty when it names none."""
    imports, calls, mains, methods, var_of = [], [], [], [], {}
    for span in _SPAN.findall(goal or ""):
        s = span.strip().rstrip(";")
        m = _FROM.match(s)
        if m:
            imports += [(m.group(1), n.strip()) for n in m.group(2).split(",") if n.strip()]
            continue
        m = _MAIN.match(s)
        if m:
            mains.append(m.group(1))
            continue
        m = _METH.match(s)
        if m:                                       # `s.shorten(url)`: a method on something the request built earlier
            args = [a.strip() for a in m.group(3).split(",") if a.strip()]
            methods.append((m.group(1), m.group(2), len([a for a in args if "=" not in a]),
                            [a.split("=", 1)[0].strip() for a in args if "=" in a], s))
            continue
        a = _ASSIGN.match(s)
        if a:
            var_of[a.group(1)] = a.group(2)
        m = _CALL.match(s)
        if m:
            args = [a.strip() for a in m.group(2).split(",") if a.strip()]
            pos = [a for a in args if "=" not in a]
            kws = [a.split("=", 1)[0].strip() for a in args if "=" in a]
            calls.append((m.group(1), len(pos), kws, s))
    meths = [(var_of[v], meth, n, k, t) for v, meth, n, k, t in methods if v in var_of]
    if not (imports or mains or meths):
        imports, calls, meths = _prose_contract(goal or "")
    return {"imports": imports, "calls": calls, "mains": mains, "methods": meths}


_CREATE = re.compile(r"\b(?:[Cc]reate|[Ww]rite|[Bb]uild|[Aa]dd)\s+`?([A-Za-z_]\w*)\.py`?\s+with\b")
_PCALL = re.compile(r"(?<![\w.`])([A-Za-z_]\w*)\(([^()]*)\)")
_PARG = re.compile(r"^[A-Za-z_]\w*(?:\s*=\s*[\w.'\"-]+)?$")
_PCLASS = re.compile(r"\b(?:an?|the)\s+([A-Z]\w*)\s+class\b|\bclass\s+([A-Z]\w*)\b")


def public_defs(root):
    """-> {rel: {top-level public function/class names}} for the project's non-test Python files. Files that do not parse are skipped."""
    import ast
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith((".", "_")) and d not in ("tests", "node_modules", "venv")]
        for f in fn:
            if not f.endswith(".py") or f.startswith("test_"):
                continue
            p = os.path.join(dp, f)
            try:
                tree = ast.parse(open(p, encoding="utf-8").read())
            except (OSError, SyntaxError, ValueError):
                continue
            out[os.path.relpath(p, root).replace(os.sep, "/")] = {x.name for x in tree.body
                                                                  if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not x.name.startswith("_")}
    return out


def code_shape(root, files):
    """-> {rel: ast.dump} for the Python files: what the code IS, with spacing, comments and blank lines ignored. None for a file that
    does not parse (the syntax check reports that)."""
    import ast
    out = {}
    for f in files:
        if f.endswith(".py") and os.path.isfile(os.path.join(root, f)):
            try:
                out[f] = ast.dump(ast.parse(core.read_text(os.path.join(root, f))[0]))
            except (SyntaxError, ValueError, OSError):
                out[f] = None
    return out


def duplicate_bodies(root, files):
    """Top-level functions whose bodies are the same code under different names. -> [(rel, [names])]"""
    import ast
    out = []
    for f in files:
        if not f.endswith(".py") or not os.path.isfile(os.path.join(root, f)):
            continue
        try:
            tree = ast.parse(core.read_text(os.path.join(root, f))[0])
        except (SyntaxError, ValueError, OSError):
            continue
        groups = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(n.body) > 0:
                groups.setdefault(ast.dump(ast.Module(body=n.body, type_ignores=[])) + ast.dump(n.args), []).append(n.name)
        out += [(f, names) for names in groups.values() if len(names) > 1]
    return out


DEDUP_WORDS = re.compile(r"duplicat|identical|copy-?pasted|the same code|become one|merge|combine|dedup", re.I)


def lost_defs(before, after, said="", keep=()):
    """Public names a file had before this sprint and no longer has anywhere in the project. -> [(rel, name)]

    A name the request or the sprint's goal mentions (`said`) is theirs to change -- "remove legacy_dump", "rename load() to read_config()"
    -- unless the finished interface (`keep`, the interface_lines list) still needs it: then losing it is a loss, whatever the words say.
    A file that no longer parses is left to the syntax check."""
    now = set().union(*after.values()) if after else set()
    needed = set()
    for line in keep or ():
        m = _ISIG.match(line) if isinstance(line, str) else None
        if m:
            needed.add(m.group(2).split(".")[-1])
        elif isinstance(line, str) and line.startswith("from "):
            needed.add(line.split(" import ", 1)[-1].split()[0] if " import " in line else "")
    return [(rel, n) for rel, names in before.items() if rel in after for n in sorted(names)
            if n not in now and (n in needed or not re.search(r"\b" + re.escape(n) + r"\b", said or ""))]


def _prose_contract(goal):
    """The same interface when the request writes it as plain words: "Create dates.py with is_leap(year), days_in_month(year, month)".

    Deliberately narrow, because a wrong guess here fails a correct build: exactly one module the request says to create, only call shapes
    whose arguments are bare names (or name=default), never a builtin, and only outside backticks. With one named class ("a Cart class:
    add(sku, price, qty=1), total()") the calls are that class's methods. Anything less certain names nothing, as before.
    A live build lost days_in_month(year, month) to a rewrite and shipped a one-argument version, because this check read only backticks."""
    import builtins
    hits = [m for m in _CREATE.finditer(goal) if not m.group(1).startswith("test")]
    if len({m.group(1) for m in hits}) != 1:
        return [], [], []
    mod = hits[0].group(1)
    # Only the sentence that says what to create: "Do not call monthrange(y, m)" or "for example f(x)" later on is not the interface.
    rest = goal[hits[0].end():]
    stop = re.search(r"[.;!?](?=\s|$)", rest)
    text = _SPAN.sub(" ", rest[:stop.start()] if stop else rest)
    neg = re.compile(r"\b(?:not|never|without|instead of|avoid|don't|do not|except)\b", re.I)
    classes = {a or b for a, b in _PCLASS.findall(goal[:hits[0].end()] + " " + text)}
    found, seen = [], set()
    for mt in _PCALL.finditer(text):
        name, argtext = mt.group(1), mt.group(2)
        clause = re.split(r",|\bbut\b|\band\b", text[:mt.start()])[-1]
        args = [a.strip() for a in argtext.split(",") if a.strip()]
        if hasattr(builtins, name) or not all(_PARG.match(a) for a in args) or neg.search(clause) or name in seen:
            continue
        seen.add(name)
        pos = [a for a in args if "=" not in a]
        kws = [a.split("=", 1)[0].strip() for a in args if "=" in a]
        found.append((name, len(pos), kws, f"{name}({argtext})"))
    if not found:
        return [], [], []
    if len(classes) == 1:
        cls = next(iter(classes))
        return [(mod, cls)], [], [(cls, n, k, kw, t) for n, k, kw, t in found if n != cls]
    if classes:
        return [], [], []
    return [(mod, n) for n, _, _, _ in found], found, []


def contract_check(e, goal):
    """Does the code expose the interface the request names? Imports each name, and checks each named call accepts those arguments
    (by signature, without running it). -> (ok, text). (True, "") when the request names no interface.

    Small models routinely build something that works but is shaped differently from what was asked -- a constructor without the path
    argument, a function left out of the package's __init__. Their own tests pass, so nothing catches it until a user does."""
    c = interface_contract(goal)
    if not (c["imports"] or c["mains"]):
        return True, ""
    lines = ["import importlib, inspect, sys", "bad = []", "names = {}"]
    for mod, name in c["imports"]:
        lines.append(f"try:\n    names[{name!r}] = getattr(importlib.import_module({mod!r}), {name!r})\n"
                     f"except Exception as ex:\n    bad.append('`from {mod} import {name}` fails: %s: %s' % (type(ex).__name__, ex))")
    for name, npos, kws, text in c["calls"]:
        args = ", ".join(["None"] * npos + [f"{k}=None" for k in kws])
        lines.append(f"if {name!r} in names:\n    try:\n        inspect.signature(names[{name!r}]).bind({args})\n"
                     f"    except TypeError as ex:\n        bad.append('`{text}` does not fit the signature: %s' % ex)\n"
                     f"    except ValueError:\n        pass")
    for cls, meth, npos, kws, text in c.get("methods") or []:
        args = ", ".join(["None"] * npos + [f"{k}=None" for k in kws])
        lines.append(f"if {cls!r} in names and isinstance(names[{cls!r}], type):\n"
                     f"    _f = getattr(names[{cls!r}], {meth!r}, None)\n"
                     f"    if _f is None:\n        bad.append('`{text}`: {cls} has no method {meth}')\n"
                     f"    else:\n        _ok = False\n"
                     f"        for _self in ((None,), ()):\n"
                     f"            try:\n                inspect.signature(_f).bind(*_self{', ' + args if args else ''})\n"
                     f"                _ok = True\n                break\n"
                     f"            except (TypeError, ValueError):\n                pass\n"
                     f"        if not _ok:\n            bad.append('`{text}` does not fit the signature of {cls}.{meth}')")
    for mod in c["mains"]:
        # A package runs under -m only through its __main__.py; a plain module runs as itself.
        lines.append(f"try:\n    _s = importlib.util.find_spec({mod!r})\n"
                     f"    if _s is None:\n        bad.append('`python -m {mod}`: there is no {mod} to run')\n"
                     f"    elif _s.submodule_search_locations is not None and importlib.util.find_spec({mod!r} + '.__main__') is None:\n"
                     f"        bad.append('`python -m {mod}`: the package has no __main__.py, so -m has nothing to run')\n"
                     f"except Exception as ex:\n    bad.append('`python -m {mod}` cannot be found: %s' % ex)")
    lines.append("import importlib.util")
    lines.insert(1, "import importlib.util")
    lines.append("print('\\n'.join(bad)); sys.exit(1 if bad else 0)")
    try:
        env = dict(core.guard_env(True), PYTHONPATH=e.root, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-B", "-c", "\n".join(lines)], cwd=e.root, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as ex:
        return True, f"(the interface check could not run: {ex})"
    if p.returncode == 0:
        return True, ""
    out = (p.stdout.strip() or p.stderr.strip()[-600:])
    where = []
    for mod, name in c["imports"]:
        hits = []
        for f in core.list_files(e.root, getattr(e, "cfg", None) or core.load_config(e.root)):
            if f.endswith(".py") and not f.startswith("tests/"):
                try:
                    src = core.read_text(os.path.join(e.root, f), 200000)[0]
                except OSError:
                    continue
                if re.search(r"^(?:class|def)\s+" + re.escape(name) + r"\b", src, re.M):
                    hits.append(f)
        if len(hits) > 1:
            where.append(f"{name} is defined in more than one place ({', '.join(hits)}); `from {mod} import {name}` gets only one of them. "
                         f"Keep one definition and re-export it from {mod.replace('.', '/')}/__init__.py.")
        elif hits and name in out:
            where.append(f"{name} is defined in {hits[0]}; make sure {mod.replace('.', '/')}/__init__.py imports it (from .x import {name}).")
    if where:
        out += "\n" + "\n".join(where)
    return False, ("The request names this interface exactly, and the code does not match it yet:\n" + out +
                   "\nChange the code (not the request) so these work as written.")


def related_tests(e, files):
    """Test files that belong to the changed files: the changed tests themselves, plus tests/test_<module>.py for each changed module.
    -> list of relative paths that exist, or [] (then the full suite runs as before)."""
    out = []
    for f in files or []:
        if f.startswith("tests/") and f.endswith(".py"):
            out.append(f)
        elif f.endswith(".py"):
            stem = os.path.splitext(os.path.basename(f))[0]
            for cand in (f"tests/test_{stem}.py", f"tests/{stem}_test.py", f"test_{stem}.py"):
                if os.path.isfile(os.path.join(e.root, cand)):
                    out.append(cand)
    return sorted(set(out))


def log_checks(e, title, v):
    e.say(f"[check] {title}: {v['passed']}/{v['total']} passed" + ("" if v["ok"] else "\n" + v["text"]))
    e.report(f"  - {title}: {v['passed']}/{v['total']} checks" + ("" if v["ok"] else " (failing: " + "; ".join(r['name'] for r in v['results'] if not r['ok'])[:300] + ")"))


def parse_docs(text):
    """DOC: path ... content (until the next DOC: line). A wrapping code fence is removed. -> {path: content}"""
    docs, cur, buf = {}, None, []
    for line in core.strip_think(text).replace("\r\n", "\n").split("\n"):
        m = re.match(r"^\s*(?:#+\s*)?(?:\*\*)?DOC\s*:\s*`?([^\s`*]+)`?(?:\*\*)?\s*$", line)
        if m:
            if cur:
                docs[cur] = "\n".join(buf)
            cur, buf = m.group(1), []
        elif cur is not None:
            buf.append(line)
    if cur:
        docs[cur] = "\n".join(buf)
    out = {}
    for k, v in docs.items():
        lines = v.strip("\n").split("\n")
        if len(lines) > 2 and re.match(r"^```(?!mermaid)[\w+-]*\s*$", lines[0].strip()) and lines[-1].strip() == "```":
            lines = lines[1:-1]               # the model wrapped the whole file in one code fence
        out[k] = "\n".join(lines).strip("\n") + "\n"
    return out


def handoff_path(e):
    return os.path.join(e.root, ".arch", "handoff.json")


def read_handoff(e):
    return core.load_json(handoff_path(e))


def write_handoff(e, **fields):
    h = read_handoff(e)
    h.update(fields)
    h["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(handoff_path(e)), exist_ok=True)
    with open(handoff_path(e), "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2)


def handoff_text(e):
    h = read_handoff(e)
    return ("HANDOFF FROM EARLIER WORK (read this first):\n" + json.dumps(h, indent=1)[:4000] + "\n\n") if h else ""


def judge(e, what, evidence, criteria):
    """LLM judge for what code cannot decide. -> (verdict dict). Strict, evidence-based, JSON."""
    prompt = (f"Judge this {what} against the criteria. Score each criterion 1-5 (5 = excellent). Be strict: a criterion with no "
              f"supporting evidence in what you were shown scores 1. Decide pass only if every score is >= 3 and the average is >= 3.5.\n\n"
              f"CRITERIA:\n{criteria}\n\nEVIDENCE:\n{evidence}\n\n"
              'Answer ONLY JSON: {"scores": {"<criterion>": {"score": 1-5, "evidence": "..."}}, "verdict": "pass"|"fail", "issues": ["specific fix", "..."]}')
    r, j = e.ask_json("evaluator", prompt, schema=arch_schemas.JUDGE)
    if not isinstance(j, dict) or "scores" not in j:
        return {"verdict": "unknown", "issues": ["the evaluator did not return usable JSON"], "call": r["id"], "avg": None}
    vals = []
    for v in (j.get("scores") or {}).values():
        try:
            vals.append(float(v["score"] if isinstance(v, dict) else v))
        except (TypeError, ValueError, KeyError):
            pass
    avg = sum(vals) / len(vals) if vals else None
    ok = bool(vals) and min(vals) >= 3 and avg >= 3.5          # we recompute: never trust the model's own verdict alone
    return {"verdict": "pass" if ok else "fail", "issues": [str(x) for x in (j.get("issues") or [])][:8], "avg": avg, "call": r["id"],
            "scores": j.get("scores")}


# ------------------------------------------------------------------------------------------ flow base
class Flow(abc.ABC):
    id, title, blurb, domains = "", "", "", list(DOMAINS)
    steps = []          # for the GUI: [{"id","title","role","kind":"llm|code|judge"}]
    produces = []

    @abc.abstractmethod
    def run(self, e, params):
        """Do the flow's work and return {"ok": bool, ...}. Every flow defines this."""


def S(i, title, role, kind):
    return {"id": i, "title": title, "role": role, "kind": kind}


# ------------------------------------------------------------------------------------------ DESIGN
DESIGN_SPEC = """Write the SPEC for this request as Markdown with exactly these headings:
# Spec: <short name>
## Goals
## Non-goals
## Users and key scenarios
## Requirements   (numbered R1, R2, ... each one testable, one sentence)
## Constraints and assumptions   (list every assumption you made instead of asking)
## Risks and open questions
Stay at the level of WHAT, not HOW. No TBD: make a reasonable assumption and write it down.

REQUEST: {goal}

{context}"""

DESIGN_BUNDLE = """Write the design for the spec below. Output each file as:
DOC: relative/path
<file content, no code fence around the whole file>

Required files:
1. DOC: docs/architecture.md with headings: Overview, Components (a table: component | responsibility | owns data | talks to), Diagram (a ```mermaid flowchart or C4 diagram with at least 4 nodes and labelled edges), Data flow (numbered), Failure modes (what breaks, how we detect it, what happens), Testing strategy (what is tested how), Security and privacy basics.
2. DOC: docs/adr/0001-<slug>.md (and 0002... for each other significant decision): headings Status, Context, Decision, Alternatives considered (at least two, with why rejected), Consequences (good and bad).
{domain_files}
Reference spec requirement ids (R1, R2...) where a component or decision satisfies them. Prefer the simplest design that meets the requirements; justify any extra moving part.

SPEC:
{spec}

{context}"""

DOMAIN_FILES = {
    "api": "3. DOC: api/openapi.json : a complete OpenAPI 3.0 document in JSON (info, paths, components/schemas, every operation with operationId and a 2xx response, request bodies for POST/PUT/PATCH, consistent error schema, auth scheme if needed). Valid JSON only.",
    "data": "4. DOC: db/schema.sql : portable SQL (runs on SQLite): tables with primary keys, NOT NULL / CHECK / UNIQUE constraints, foreign keys, and indexes on foreign keys and on columns used for lookups. Comment each table's purpose.",
    "web": "5. DOC: docs/ui.md : headings Screens, Components (name, props/state, events), States (empty, loading, error, success for each screen), Accessibility (labels, focus order, contrast), Responsive behaviour.",
}


class DesignFlow(Flow):
    id, title = "design", "Design"
    blurb = "Request -> spec -> architecture, decision records, API contract and data model, verified by structure checks and a strict judge."
    steps = [S("spec", "Write the spec", "planner", "llm"), S("design", "Design bundle", "architect", "llm"),
             S("checks", "Structure checks (Mermaid, OpenAPI, SQL, ADR)", "-", "code"),
             S("judge", "Strict judge", "evaluator", "judge"), S("revise", "Revise on failure (up to N)", "architect", "llm"),
             S("handoff", "Write handoff for the build flow", "-", "code")]
    produces = ["docs/spec.md", "docs/architecture.md", "docs/adr/*.md", "api/openapi.json", "db/schema.sql", "docs/ui.md", ".arch/handoff.json"]

    def run(self, e, params):
        goal, doms = e.goal, params.get("domains") or detect_domains(e, e.goal)
        ctx = handoff_text(e) + ("EXISTING PROJECT (for context):\n" + e.context(goal, 8000) if core.list_files(e.root, e.cfg) else "")
        e.report(f"- Domains: {', '.join(doms)}", "")
        r = e.call("planner", DESIGN_SPEC.format(goal=goal, context=ctx), label="spec")
        spec = r["text"].strip()
        spec = re.sub(r"^```(?:markdown|md)?\n|\n```$", "", spec)
        sp = e.put_file("docs/spec.md", spec, "spec")
        sres = ck.check_spec(spec)
        sv = ck.summarize(sres)
        e.mark(r["id"], sv["ok"], sv["failures"])
        e.say(f"[check] spec: {sv['passed']}/{sv['total']}" + ("" if sv["ok"] else "\n" + sv["failures"]))
        if not sv["ok"]:
            r2 = e.call("planner", DESIGN_SPEC.format(goal=goal, context=ctx) + "\n\nYour previous attempt failed these checks, fix them:\n" + sv["failures"] + "\n\nPREVIOUS:\n" + spec, label="spec-revise")
            spec = re.sub(r"^```(?:markdown|md)?\n|\n```$", "", r2["text"].strip())
            e.put_file("docs/spec.md", spec, "spec (revised)")
            e.mark(r2["id"], ck.summarize(ck.check_spec(spec))["ok"])
        bundle_prompt = DESIGN_BUNDLE.format(domain_files="\n".join(DOMAIN_FILES[d] for d in doms if d in DOMAIN_FILES), spec=spec, context=ctx)
        r = e.call("architect", bundle_prompt, label="design")
        feedback, written, verdict = "", [], None
        for rev in range(e.cfg["limits"]["max_revisions"] + 1):
            docs = parse_docs(r["text"])
            if not docs:
                feedback = "No DOC: files were found. Use the exact 'DOC: path' format."
            else:
                for path, content in docs.items():
                    p = e.put_file(path, content, "design file")
                    if p["status"] in ("applied", "identical") and path not in written:
                        written.append(path)
                v = verify(e, written, run_tests=False)
                log_checks(e, f"design checks (round {rev + 1})", v)
                ev = "\n".join(f"=== {w} ===\n{core.read_text(os.path.join(e.root, w), 12000)[0]}" for w in written if os.path.isfile(os.path.join(e.root, w)))
                if not any(w.endswith("architecture.md") for w in written):
                    v["ok"], v["text"] = False, v["text"] + "\n- docs/architecture.md is missing"
                if not any("/adr/" in w for w in written):
                    v["ok"], v["text"] = False, v["text"] + "\n- no ADR file under docs/adr/"
                judged = None
                if v["ok"] and e.cfg["behavior"]["evaluator"] != "off":
                    judged = judge(e, "software design", f"SPEC:\n{spec}\n\n{ev}",
                                   "traceability (every requirement R# is addressed by a named component or decision)\nsimplicity (no moving part without a stated reason)\n"
                                   "failure_modes (realistic failures, detection and behaviour are described)\ntestability (how each component will be tested is clear)\n"
                                   "decisions (alternatives are real and trade-offs are honest)")
                    verdict = judged
                    e.report(f"  - judge: {judged['verdict']} (avg {judged['avg']})")
                    e.say(f"[judge] {judged['verdict']} avg={judged['avg']}")
                ok = v["ok"] and (judged is None or judged["verdict"] in ("pass", "unknown"))
                e.mark(r["id"], ok, v["text"] or "; ".join((judged or {}).get("issues", [])))
                if ok:
                    break
                feedback = (v["text"] + "\n" + "\n".join("- " + i for i in (judged or {}).get("issues", []))).strip()
            if rev >= e.cfg["limits"]["max_revisions"]:
                e.say("[!] design still has open issues after the allowed revisions")
                e.report("- Design finished with open issues: " + feedback[:500])
                break
            r = e.call("architect", bundle_prompt + "\n\nYour previous output was rejected. Fix ALL of these and resend every file in full:\n" + feedback + "\n\nPREVIOUS OUTPUT:\n" + r["text"][:9000], label=f"design-revise-{rev + 1}")
        write_handoff(e, goal=goal, domains=doms, flow="design", spec="docs/spec.md", artifacts=written,
                      decisions=[w for w in written if "/adr/" in w], next="build", design_verdict=(verdict or {}).get("verdict"))
        e.report("", f"- Wrote: {', '.join(['docs/spec.md'] + written)}")
        return {"ok": True, "files": ["docs/spec.md"] + written}


# ------------------------------------------------------------------------------------------ BUILD
BUILD_PLAN = """Plan the work as small SPRINTS that each leave the project working. Answer ONLY JSON:
{{"sprints": [{{"name": "...", "kind": "backend|ui|sql|mixed", "goal": "one sentence", "files": ["paths you expect to create or change"],
  "acceptance": [{{"id": "A1", "text": "observable, checkable criterion"}}]}}],
 "interface": ["path.py: function_name(arg1, arg2)", "path.py: ClassName(arg)", "path.py: ClassName.method(arg)"]}}
Rules: at most {max_sprints} sprints; the first sprint is the thinnest working slice; every sprint has 2-5 acceptance criteria that a test or a script can check; backend code gets tests under tests/.
"interface" lists EVERY public function, class and method the finished code will have, with its exact arguments, decided now: every sprint builds against it, so later sprints never rename or re-shape what earlier ones made. Use exactly the names and arguments the goal gives.

GOAL: {goal}

{context}"""

BUILD_SPRINT = """Implement this sprint. Make the smallest complete change that satisfies the acceptance criteria, including tests for backend behaviour (under tests/, runnable with the project's test command).

SPRINT {n}/{total}: {name}
GOAL: {sgoal}
FILES THIS SPRINT MUST CREATE OR CHANGE: {files}
ACCEPTANCE CRITERIA (the contract):
{criteria}
INTERFACE OF THE FINISHED CODE (keep these names and arguments exactly; other sprints build on them):
{interface}

PROJECT CONTEXT:
{context}

{feedback}"""


# A planned interface entry, in the shapes planners write: "dates.py: days_in_month(year: int, month: int) -> int", "dates.py::weekday(y)",
# "def day_of_year(year, month, day):", "dates.py: is_leap(year) - True for leap years". Group 1 path, 2 name, 3 arguments, 4 return type.
_ISIG = re.compile(r"^\s*(?:([\w./-]+\.py)\s*(?:::|[:\-])\s*)?(?:async\s+)?(?:def\s+|class\s+)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*"
                   r"(\([^()]*\))?\s*(?:->\s*([\w\[\], .|]+?))?\s*:?\s*(?:(?:#|--| - ).*)?$")


def interface_lines(goal, planned=None, cap=20, dropped=None):
    """The interface every sprint is shown: what the REQUEST names first (it is the contract the build is checked against), then what the
    planner decided for everything else, normalised to `path.py: name(args) -> type`. A planned entry never overrides the request: one
    with the same name is dropped. Entries that are not a signature at all go to `dropped` (when given) so the report can say so. -> [str]"""
    c = interface_contract(goal)
    out, names = [], set()
    calls = {n: t for n, _, _, t in c["calls"]}
    for mod, name in c["imports"]:
        out.append(f"from {mod} import {name}" + (f"  ->  {calls[name]}" if name in calls and calls[name] != name else ""))
        names.add(name)
    for cls, meth, _, _, t in c.get("methods") or []:
        out.append(f"{cls}.{meth}: {t}")
        names.add(f"{cls}.{meth}")
    out += [f"python -m {m}" for m in c["mains"]]
    for line in planned or []:
        if not isinstance(line, str):
            continue
        m = _ISIG.match(line) if len(line) <= 200 else None
        # a bare word is not a signature: it needs a path, an argument list or def/class
        if not m or not (m.group(1) or m.group(3) or re.match(r"\s*(?:[\w./-]+\.py\s*(?:::|[:\-])\s*)?(?:async\s+)?(?:def|class)\s", line)):
            if dropped is not None and not line.startswith(("from ", "python -m ")) and " -> " not in line[:120].split(":")[0]:
                dropped.append(line)
            continue
        if m.group(2) in names or m.group(2).split(".")[-1] in names:
            continue
        names.add(m.group(2))
        out.append((f"{m.group(1)}: " if m.group(1) else "") + m.group(2) + (m.group(3) or "") + (f" -> {m.group(4).strip()}" if m.group(4) else ""))
    return out[:cap]


def small_request(goal, sprints, named):
    """One module and its tests: splitting it across sprints only adds hand-offs where a small model can drop work it already finished.
    Positive evidence only: every sprint lists its files and together they (with the files the request names) hold exactly one source
    file. A plan without file lists is "unknown", never "one module" -- a 4-sprint web app must not collapse into one sprint."""
    if len(sprints) < 2 or len(goal or "") > 700:
        return False
    if any(not [f for f in (s.get("files") or []) if isinstance(f, str)] for s in sprints):
        return False
    src = {f for f in list(named) + [f for s in sprints for f in (s.get("files") or []) if isinstance(f, str)]
           if not (f.startswith("tests/") or os.path.basename(f).startswith("test_"))}
    return len(src) == 1


_WANTS_TESTS = re.compile(r"\b(?:with|and|plus|write|add|include|including|covered by)\s+(?:(?:unit|pytest|automated|its|the|some|own)\s+){0,2}"
                          r"tests\b|\btests?\s+(?:in|under)\s+`?tests/|\bpytest\b", re.I)


def wants_tests(goal):
    """Does the request ask for tests as part of what is built ("with tests in tests/", "and its tests", "pytest")?"""
    return bool(_WANTS_TESTS.search(goal or ""))


def test_file_for(sprints):
    """The test file the first sprint writes when the request asks for tests: one the plan already has, else tests/test_<module>.py."""
    files = [f for s in sprints for f in (s.get("files") or []) if isinstance(f, str)]
    planned = next((f for f in files if _test_module(f)), "")
    if planned:
        return planned
    src = next((f for f in files if f.endswith(".py") and not arch_fakecode.is_test_file(f)), "app.py")
    parts = [p for p in src.split("/") if p and p != "src"]
    stem = parts[0] if len(parts) > 1 else os.path.splitext(parts[-1] if parts else "app")[0]
    return f"tests/test_{re.sub(r'[^A-Za-z0-9_]', '_', stem) or 'app'}.py"


def _test_module(f):
    b = os.path.basename(f.replace("\\", "/"))
    return f.endswith(".py") and (b.startswith("test_") or b.endswith("_test.py"))


def has_tests(e):
    return any(_test_module(f) for f in core.list_files(e.root, e.cfg))


def merge_sprints(goal, sprints):
    acc = [a for s in sprints for a in (s.get("acceptance") or []) if isinstance(a, dict)]       # every criterion: none is lost in the merge
    files = list(dict.fromkeys(f for s in sprints for f in (s.get("files") or []) if isinstance(f, str)))
    kinds = {s.get("kind") for s in sprints if s.get("kind")}
    return [{"name": "implementation", "kind": kinds.pop() if len(kinds) == 1 else "mixed", "goal": goal, "files": files,
             "acceptance": acc or [{"id": "A1", "text": "the goal works and the tests pass"}]}]


_TCOUNT = {k: re.compile(r"(\d+) " + k) for k in ("passed", "failed", "error")}
# "Broken" means the code or the tests no longer LOAD -- structural evidence only. An exception NAME anywhere in the output is not enough:
# "AssertionError: expected NameError" or one test failing on a typo made a clearly better try look broken, and it was undone.
_BROKEN = re.compile(r"ERROR collecting|errors? during collection|while loading conftest|Failed to import test module|ImportError while importing test module")


class BestState:
    """The best version of the files a step changed, so one bad try cannot undo the progress of the tries before it.

    A try counts as WORSE only when it is clearly worse: it broke the code (a file no longer parses, or the tests no longer load, where
    the kept version had neither), it removed a function an earlier sprint built, or -- comparing like with like -- fewer tests pass AND
    more fail. A try that clears a gate the kept version failed (the request's interface, a required file) is never worse, whatever its
    test counts: the request decides, not the model's own tests. A worse try's files are put back, byte for byte, before the next try, and
    the model is told what still fails in the kept version. Ties go to the newest. Seen live: a heavy model's correct fix was followed by
    an edit that left an IndentationError, and the run ended on the broken one; a rewrite that dropped days_in_month() was carried into
    every later try."""

    def __init__(self, e):
        self.e, self.origin, self.best, self.snap, self.text = e, {}, None, {}, ""

    def note(self, res):
        for x in res or []:
            if x.get("status") == "applied" and x.get("rel") and x["rel"] not in self.origin:
                self.origin[x["rel"]] = (x.get("old", ""), bool(x.get("crlf"))) if x.get("existed") else None

    @staticmethod
    def measure(v, lost=False, gates_ok=True):
        """`v` is what verify() returned, before any advisory text is added to it. `lost` is the list (or flag) of definitions this try
        removed; `gates_ok` is False when a gate outside verify() failed (the interface contract, a file the request names)."""
        text = (v.get("text") or "") + "\n" + ((v.get("tests") or ("", ""))[1] or "")
        syntax = any(not r["ok"] and re.search(r"parses|compiles", str(r["name"])) for r in (v.get("results") or []))
        bad_check = syntax or any(not r["ok"] and "imports are installed" in str(r["name"]) for r in (v.get("results") or []))
        summary = [ln for ln in text.split("\n") if re.search(r"\b\d+ (?:passed|failed|errors?)\b.* in [\d.]+s", ln)]
        last = summary[-1] if summary else ""
        cnt = {k: sum(int(x) for x in rx.findall(last)) for k, rx in _TCOUNT.items()}
        names = [f"{n} from {r}" for r, n in lost][:4] if isinstance(lost, (list, tuple)) else []
        ran = ((v.get("tests") or ("none", ""))[0] or "none") in ("pass", "fail")      # from the runner's status: unittest and custom commands too
        return {"broken": bool(bad_check or _BROKEN.search(text)), "syntax": bool(syntax), "ran": ran, "lost": bool(lost), "lost_names": names, "gates_ok": bool(gates_ok),
                "passed": cnt["passed"], "failed": cnt["failed"] + cnt["error"], "scope": "related" if "(related tests first" in text else "all"}

    @staticmethod
    def worse(m, b):
        if m.get("gates_ok", True) and not b.get("gates_ok", True) and not m["broken"] and not m["lost"]:
            return ""                                    # it fixed what the request requires: kept, even if the model's own tests now disagree
        if m.get("syntax") and not b.get("syntax"):      # a file that no longer parses is always worse, even than a version whose tests did not load
            return "it broke the code: a file no longer parses"
        # Tests that do not load yet are only worse than tests that ran. Seen live: a build's first try had no tests, every later try that
        # added them (with an import still to fix) was undone as "broken", and the run ended with no tests at all.
        if m["broken"] and not b["broken"] and (b.get("ran") or b["passed"] + b["failed"] > 0):
            return "it broke the code: the tests no longer load, or an import is missing"
        if m["lost"] and not b["lost"]:
            return ("it removed " + ", ".join(m.get("lost_names") or []) if m.get("lost_names") else "it removed code") + ", which an earlier sprint built"
        if m["broken"] == b["broken"] and m["scope"] == b["scope"] and m["passed"] < b["passed"] and m["failed"] > b["failed"]:
            return f"fewer tests pass ({b['passed']} -> {m['passed']}) and more fail ({b['failed']} -> {m['failed']})"
        return ""

    def _read(self):
        out = {}
        for rel in self.origin:
            p = os.path.join(self.e.root, rel)
            try:
                with open(p, "rb") as f:
                    out[rel] = f.read()                  # bytes: line endings and encoding come back exactly as they were
            except OSError:
                out[rel] = None
        return out

    def judge(self, v, lost=False, gates_ok=True, shown=None):
        """After a failing try. -> "" when it is kept, else the plain reason it was undone (the files are already restored).
        `shown` is the full feedback the try produced (with the interface, missing-file and lost-function notes); it is what the model is
        told still fails when a later try is undone back to this one."""
        m = self.measure(v, lost, gates_ok)
        why = self.worse(m, self.best) if self.best is not None else ""
        if not why:
            self.best, self.snap = m, self._read()
            self.text = (shown if shown is not None else v.get("text")) or ""
            return ""
        for rel, orig in self.origin.items():
            p = os.path.join(self.e.root, rel)
            want = self.snap.get(rel, "origin")
            try:
                if want == "origin":
                    want = orig
                    if want is not None:
                        core.write_text(p, want[0], want[1])
                if want is None:
                    if os.path.isfile(p):
                        os.remove(p)
                elif isinstance(want, bytes):
                    with open(p, "wb") as f:
                        f.write(want)
                    core._READ_CACHE.pop(os.path.abspath(p), None)
            except OSError:
                pass
        if getattr(self.e, "_retriever", None) is not None:
            self.e._retriever.touch()
        self.e.say(f"[undo] that try was worse than the kept version ({why}); its changes were undone")
        self.e.report(f"  - A try was undone: {why}")
        return why

    def still_fails(self):
        return self.text.strip() or "(its own checks pass there; what the request still needs is in the acceptance criteria and the interface above)"


class BuildFlow(Flow):
    plan_prompt = None          # set by subclasses; None = BUILD_PLAN
    all_role = None             # force one role for every sprint (the feature flow uses the full-stack role)
    id, title = "build", "Build"
    blurb = "Goal (or a finished design) -> sprints with acceptance contracts -> generator -> code checks and tests -> strict judge."
    steps = [S("plan", "Plan sprints + contracts", "planner", "llm"), S("code", "Implement the sprint", "generator*", "llm"),
             S("checks", "Syntax, HTML, SQL, OpenAPI checks + tests (+ browser)", "-", "code"),
             S("fix", "Fix loop on failure (up to N)", "generator*", "llm"), S("judge", "Judge against the contract", "evaluator", "judge"),
             S("handoff", "Save progress for the next session", "-", "code")]
    produces = ["source files", "tests/", ".arch/contract.json", ".arch/handoff.json"]

    def run(self, e, params):
        goal = e.goal
        doms = params.get("domains") or detect_domains(e, goal)
        ho = handoff_text(e)
        files0 = core.list_files(e.root, e.cfg)
        ctx = ho + (e.context(goal, 9000, always=[x for x in ("docs/spec.md",) if os.path.isfile(os.path.join(e.root, x))]) if files0 else "(empty project)")
        rs = params.get("resume") or {}
        if rs.get("plan"):                                              # resuming: the saved plan, not a new one
            r, plan = None, {"sprints": rs["plan"]}
            e.say(f"[resume] continuing run {rs.get('from')}: {len(rs.get('done') or [])} sprint(s) already passed")
            e.report(f"- Resumed from run `{rs.get('from')}`; sprints already passed are skipped: {', '.join(rs.get('done') or []) or 'none'}")
        else:
            r, plan = e.ask_json("planner", (self.plan_prompt or BUILD_PLAN).format(goal=goal, context=ctx, max_sprints=params.get("max_sprints") or e.cfg["limits"].get("max_sprints", 5)),
                                 schema=arch_schemas.PLAN if self.plan_prompt is None else None)
        sprints = (plan or {}).get("sprints") if isinstance(plan, dict) else None
        sprints = [s for s in sprints if isinstance(s, dict)][:6] if isinstance(sprints, list) else []
        named = named_files(goal)                                       # files the request names outright: they WILL be checked for
        if not sprints:                                                 # checked AFTER filtering: a list of plain strings used to leave zero
            sprints = [{"name": "implementation", "kind": "mixed", "goal": goal, "files": list(named),   # sprints, so nothing was built at all
                        "acceptance": [{"id": "A1", "text": "the goal works and the tests pass"}]}]
            e.say("[i] planner gave no usable sprint list; running one sprint")
        if (not rs.get("plan") and self.plan_prompt is None and e.cfg["behavior"].get("merge_small_plans", True)
                and small_request(goal, sprints, named)):                # the feature flow keeps its contract-first layers
            e.report(f"- The plan split a one-module request into {len(sprints)} sprints; merged into one, so no step can drop what another finished")
            e.say(f"[i] one module: the {len(sprints)} planned sprints run as one")
            sprints = merge_sprints(goal, sprints)
        wanted = rs.get("interface") if rs.get("plan") else ((plan or {}).get("interface") if isinstance(plan, dict) else None)
        unusable = []
        iface = interface_lines(goal, wanted, dropped=unusable)          # a resume rebuilds the same list: request first, then the saved plan's
        if iface:
            e.report("- Interface every sprint builds against:", *[f"  - `{x}`" for x in iface])
        if unusable:
            e.report(f"- {len(unusable)} planned interface entr{'y was' if len(unusable) == 1 else 'ies were'} not a usable signature and "
                     "were left out: " + "; ".join(f"`{x[:60]}`" for x in unusable[:4]))
        planned = {str(f) for s in sprints for f in (s.get("files") or []) if isinstance(f, str)}
        orphans = [f for f in named if f not in planned]
        if orphans:                                                     # the plan forgot a file the request asked for: the last sprint owns it
            sprints[-1]["files"] = [f for f in (sprints[-1].get("files") or []) if isinstance(f, str)] + orphans
            e.report(f"- The plan left out {', '.join(orphans)}, which the request names; added to the last sprint")
        # A request that asks for tests gets them from the FIRST sprint, and every sprint must leave tests that run. Seen live: plans put
        # the tests in the last sprint, the run ended early, and two whole projects finished with no test file at all.
        tests_for = test_file_for(sprints) if wants_tests(goal) else ""
        if tests_for and tests_for not in (sprints[0].get("files") or []):
            sprints[0]["files"] = [f for f in (sprints[0].get("files") or []) if isinstance(f, str)] + [tests_for]
            e.report(f"- The request asks for tests: the first sprint writes `{tests_for}`, and every sprint must leave tests that run")
        if r:
            e.mark(r["id"], bool(plan))
        e.checkpoint(plan=sprints, interface=iface)
        os.makedirs(os.path.join(e.root, ".arch"), exist_ok=True)
        with open(os.path.join(e.root, ".arch", "contract.json"), "w", encoding="utf-8") as f:
            json.dump({"goal": goal, "sprints": sprints, "interface": iface}, f, indent=2)
        iface_text = "\n".join("- " + x for x in iface) or "(none named; keep the names earlier sprints created)"
        e.report(f"- Sprints planned: {len(sprints)}", "")
        if (tests_for and e.cfg["behavior"].get("tests_first", "off") == "on" and e.review in ("auto", "dry")
                and not os.path.isfile(os.path.join(e.root, tests_for))):
            if tests_first(e, goal, sprints, iface_text, tests_for):
                for sp_ in sprints:                                     # the tests exist: no sprint writes them, and none may edit them
                    sp_["files"] = [f for f in (sp_.get("files") or []) if f != tests_for]
                e.deny_prefixes = list(e.deny_prefixes) + [tests_for]
        done, all_files = [], list(rs.get("files") or [])
        carry = ""                                                      # the failure of a folded sprint, handed to the next one
        import arch_chain
        defer, why = arch_chain.defer_judge(e)
        deferred = []                                                   # (name, criteria, files) judged together after the loop
        if defer:
            e.say(f"[i] judge batched at the end: {why}")
            e.report(f"- Judge batched at the end: {why}", "")
        for n, sp in enumerate(sprints, 1):
            name, sgoal = str(sp.get("name") or f"sprint {n}"), str(sp.get("goal") or goal)
            if name in (rs.get("done") or []):
                e.say(f"[resume] sprint {n}/{len(sprints)} '{name}' already passed: skipped")
                done.append({"sprint": name, "ok": True, "files": []})
                continue
            acc = [a for a in (sp.get("acceptance") or []) if isinstance(a, dict)]
            criteria = "\n".join(f"- {a.get('id', '?')}: {a.get('text', '')}" for a in acc) or "- the goal works and tests pass"
            want = [str(x) for x in (sp.get("files") or []) if isinstance(x, str)]
            e.sprint_files = set(want)                                  # files this sprint must write never count toward max_files_per_step
            role = self.all_role or {"ui": "generator_ui", "sql": "generator_sql", "backend": "generator"}.get(str(sp.get("kind")), gen_role_for(want, doms))
            e.say(f"\n=== Sprint {n}/{len(sprints)}: {name}  (role: {role}) ===")
            e.report(f"## Sprint {n}: {name}", f"- Role: {role}", f"- Contract:\n{criteria}")
            feedback, ok, changed = carry, False, []
            carry, last_text = "", None
            coach = arch_progress.Coach(e, role)
            kept = public_defs(e.root)                                  # what earlier sprints built; a rewrite may not silently drop it
            best = BestState(e)                                         # one bad try may not undo the progress of the tries before it
            for attempt in range(e.cfg["limits"]["max_revisions"] + 2):
                k_res = int(e.cfg["behavior"].get("stall_samples", 3) or 1)
                rescue = attempt == e.cfg["limits"]["max_revisions"] + 1
                if rescue and not (k_res > 1 and not e.pins.get(role) and e.review in ("auto", "dry")):
                    break                                               # the extra round is only a best-of-N rescue for a stuck step
                cx = ho + e.context(sgoal + " " + " ".join(want), None, always=[x for x in ("docs/spec.md", "docs/architecture.md", "api/openapi.json", "db/schema.sql") if os.path.isfile(os.path.join(e.root, x))][:3])
                prompt = BUILD_SPRINT.format(n=n, total=len(sprints), name=name, sgoal=sgoal, criteria=criteria, feedback=feedback, context=cx,
                                             files=", ".join(want) or "(your choice)", interface=iface_text)
                k = int(e.cfg["behavior"].get("stall_samples", 3) or 1)
                if (coach.stalled or rescue) and k > 1 and not e.pins.get(role) and e.review in ("auto", "dry"):
                    r = best_of_n(e, role, prompt, f"sprint{n}-try{attempt + 1}", k, changed)
                else:
                    r = e.call(role, prompt, label=f"sprint{n}-try{attempt + 1}", temperature=(0.7 if last_text == "repeat" else None))
                if last_text not in (None, "repeat") and r["text"].strip() == last_text:
                    # The same answer to the same failure will fail the same way. Say so, and sample differently next time.
                    e.mark(r["id"], False, "identical to the previous try")
                    feedback = ("FEEDBACK: your reply was IDENTICAL to your previous one, which failed. Do something different.\n"
                                + feedback)
                    last_text = "repeat"
                    continue
                last_text = r["text"].strip()
                res = e.apply(r["text"], f"sprint {n} try {attempt + 1}")
                if any(x["status"] == "quit" for x in res):
                    e.report("- Stopped by user.")
                    return {"ok": False, "files": all_files, "stopped": True}
                best.note(res)
                applied = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
                changed = sorted(set(changed + applied))
                if e.review == "dry":
                    e.mark(r["id"], False, "dry run")
                    return {"ok": True, "files": all_files, "dry": True}
                if not applied:
                    feedback = "FEEDBACK: none of your changes could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res if x["status"] == "error") + "\nResend using the exact FILE:/EDIT:/DEF: formats.\n\n"
                    e.mark(r["id"], False, feedback)
                    feedback = coach.after(feedback, r["text"], feedback, [])
                    continue
                v = v0 = verify(e, changed)
                cok_last = True
                lost = lost_defs(kept, public_defs(e.root), sgoal + " " + goal, keep=iface)
                if lost:                                                # a whole-file rewrite that left out work an earlier sprint finished
                    v = dict(v, ok=False, text="Your FILE rewrite removed code an earlier sprint built and the request still needs: "
                             + "; ".join(f"{name} from {rel}" for rel, name in lost[:8])
                             + ". Keep it: change one function at a time with DEF: blocks, or send the COMPLETE file including it.\n\n" + (v.get("text") or ""))
                missing = [f for f in want if f in named and not os.path.isfile(os.path.join(e.root, f))]
                if tests_for and not has_tests(e):                     # asked for tests, none yet: a failure whatever else passed
                    missing = missing + [tests_for]
                    v = dict(v, ok=False, text=(v.get("text") or "") + f"\n\nThe request asks for tests and this project has none yet. In this reply, "
                             f"write {tests_for} with pytest test functions that import and call the code you wrote.")
                if missing and v["ok"]:                                 # everything that exists passes, but a file the request asked for is not there
                    v = dict(v, ok=False, text="These files are required by the request and do not exist yet: " + ", ".join(missing)
                             + ". Create them (with FILE: blocks) now.")
                if n < len(sprints) and not v["ok"]:                    # earlier sprints: an advisory note next to a failure, never a failure itself
                    cok, ctext = contract_check(e, goal)
                    if not cok:
                        v = dict(v, text=(v.get("text") or "") + "\n\n(For later in this build) " + ctext)
                if n == len(sprints):                                   # the finished build must have the interface the request spelled out.
                    cok, ctext = contract_check(e, goal)                # Checked even while tests fail: otherwise the model only ever hears
                    cok_last = cok
                    if not cok:                                         # about its own (often wrong) tests and never about the shape asked for.
                        v = dict(v, ok=False, text=(ctext + ("\n\n" + v["text"] if v.get("text") else "")))
                        e._trace({"type": "check", "t": time.time(), "flow": e.flow, "step": f"sprint{n}", "gate": "interface_contract",
                                  "ok": False, "text": ctext[:2000], "metrics": {}, "secs": 0})
                log_checks(e, f"sprint {n} checks", v)
                e.mark(r["id"], v["ok"], v["text"])
                if v["ok"]:
                    ok = True
                    break
                undone = (best.judge(v0, lost, gates_ok=cok_last and not missing, shown=v["text"])
                          if e.cfg["behavior"].get("keep_best_try", True) else "")
                mine = [f for f in changed if (f.startswith("tests/") or "/tests/" in f or os.path.basename(f).startswith("test_"))]
                hint = ""
                if mine and iface:
                    hint = ("\nThe failing tests are ones YOU wrote in this run (" + ", ".join(mine[:4]) + "). The interface is fixed:\n"
                            + "\n".join("  " + s for s in iface[:8]) + "\nIf a test contradicts that interface, rewrite the TEST, not the interface.\n")
                elif mine:
                    # Seen live (2026-11-04): a model wrote `slugify('Müsic!') == 'musik'` -- no code can pass that -- and spent every
                    # retry bending correct code toward its own wrong test. A test written in THIS run can be wrong too.
                    hint = ("\nFix the CODE so the tests pass. Only if a failing assertion's expected value clearly contradicts the goal "
                            "(an impossible or misspelled result) may you correct that one assertion in your test (" + ", ".join(mine[:4]) + "). "
                            "Never delete a test, and never loosen one that matches the goal.\n")
                if "FileNotFoundError" in (v["text"] or "") and mine:
                    hint += "\nA test reads a file that does not exist. Put the test data inline in the test (or create it in tmp_path), do not rely on fixture files.\n"
                if undone:
                    changed = [f for f in changed if os.path.exists(os.path.join(e.root, f))]
                    head = (f"FEEDBACK: your last change made things worse ({undone}), so it was UNDONE: the files are back to the version before it "
                            "(the project context shows them as they are now). What still fails in that version:\n" + best.still_fails())
                    feedback = coach.after(head + hint + "\n\n", r["text"], v["text"], applied)
                    continue
                feedback = coach.after("FEEDBACK: your last change failed these checks. Fix them (smallest change):\n" + v["text"] + hint + "\n\n", r["text"], v["text"], applied)
            coach.close()
            all_files = sorted(set(all_files + changed))
            judged = None
            if ok and e.cfg["behavior"]["evaluator"] == "each" and acc:
                if defer:
                    deferred.append((name, criteria, self._evidence(e, changed, v["tests"])))     # what a judge would see NOW; judged later from this
                else:
                    judged = self._judge(e, name, criteria, changed)
            e.report(f"- Result: {'passed checks' if ok else 'FAILED checks'}" + (f"; judge {judged['verdict']}" if judged else "; judged at the end" if defer and ok and acc else ""), "")
            done.append({"sprint": name, "ok": ok, "files": changed})
            e.checkpoint(sprints_done=[d["sprint"] for d in done if d["ok"]], files=all_files)
            write_handoff(e, goal=goal, domains=doms, flow="build", sprints_done=done, remaining=[s.get("name") for s in sprints[n:]], files=all_files,
                          next=("continue with the next sprint" if ok else f"fix sprint '{name}': " + feedback[:300]))
            if not ok:
                if n < len(sprints) and e.review == "auto" and e.cfg["behavior"].get("fold_failed_sprints", True):
                    # Unattended: stopping here means the parts of the request planned for later sprints are never attempted -- and
                    # those are often the parts that are checked. Fold this sprint's work into the next one and carry the failure.
                    nxt = sprints[n]
                    nxt["goal"] = f"{sgoal} -- and then: {nxt.get('goal') or ''}"
                    nxt["acceptance"] = acc + [a for a in (nxt.get("acceptance") or []) if isinstance(a, dict)]
                    nxt["files"] = list(dict.fromkeys(want + [f for f in (nxt.get("files") or []) if isinstance(f, str)]))
                    carry = feedback
                    done[-1]["folded"] = True
                    e.say(f"[i] sprint '{name}' did not pass; its work is folded into the next sprint instead of ending the build")
                    e.report(f"- Not passing yet; folded into the next sprint so the rest of the request is still attempted")
                    continue
                e.say(f"[!] sprint '{name}' still failing; stopping so you can look at it")
                break
        if deferred and done and not all(d["ok"] for d in done):
            e.report(f"- Deferred judge skipped: the build stopped at a failing sprint ({len(deferred)} passed sprint(s) not judged).")
            deferred = []
        for name, criteria, ev in deferred:                              # the batched per-sprint judgements: the same evidence, one model load
            try:
                j = self._judge(e, name, criteria, None, evidence=ev)
            except core.BudgetExceeded as ex:
                e.report(f"- Deferred judge stopped: {ex}")
                break
            e.report(f"- Judge for sprint '{name}': {j['verdict']} (avg {j['avg']})" + (f"; issues: {'; '.join(j['issues'])[:300]}" if j["issues"] else ""))
        if e.cfg["behavior"]["evaluator"] == "final" and done and all(d["ok"] for d in done):
            crit = "\n".join(f"{s.get('name')}: " + "; ".join(str(a.get("text")) for a in (s.get("acceptance") or []) if isinstance(a, dict)) for s in sprints)
            j = self._judge(e, "whole build", crit, all_files)
            e.report(f"- Final judge: {j['verdict']} (avg {j['avg']}); issues: {'; '.join(j['issues'])[:400]}")
            if j["verdict"] == "fail" and j["issues"] and e.review != "dry":
                role = self.all_role or gen_role_for(all_files, doms)
                r = e.call(role, "Fix these issues found by review, with the smallest changes:\n" + "\n".join("- " + i for i in j["issues"]) + "\n\nPROJECT CONTEXT:\n" + e.context(" ".join(j["issues"]), None), label="final-fix")
                res = e.apply(r["text"], "final fix")
                v = verify(e, [x["rel"] for x in res if x["status"] == "applied"])
                e.mark(r["id"], v["ok"], v["text"])
                log_checks(e, "final fix checks", v)
        ok_all = bool(done) and done[-1]["ok"] and all(d["ok"] or d.get("folded") for d in done)
        e.report("", f"- Build {'complete' if ok_all else 'incomplete'}: {sum(1 for d in done if d['ok'])}/{len(sprints)} sprints passed their checks"
                 + (" (failed sprints were folded into later ones that passed)" if ok_all and any(d.get("folded") for d in done) else "") + ".")
        return {"ok": ok_all, "files": all_files}

    def _evidence(self, e, files, tests=None):
        """What the judge sees: the sprint's files as they are NOW and the test result (`tests` = (status, log) from the sprint's own checks when given, else a fresh run)."""
        ev = "\n".join(f"=== {f} ===\n{core.read_text(os.path.join(e.root, f), 9000)[0]}" for f in files[:6] if os.path.isfile(os.path.join(e.root, f)))
        t = tests if tests is not None else core.run_tests(e.root, e.cfg)
        return ev + f"\n=== test result: {t[0]} ===\n{(t[1] or '')[-1500:]}"

    def _judge(self, e, name, criteria, files, evidence=None):
        ev = evidence if evidence is not None else self._evidence(e, files)
        j = judge(e, f"implementation of '{name}'", ev, "each acceptance criterion below, one score each:\n" + criteria + "\nno_stubs (no placeholder or fake behaviour)")
        e.say(f"[judge] {name}: {j['verdict']} avg={j['avg']}")
        return j


# ------------------------------------------------------------------------------------------ BUGFIX
BUG_REPRO = """Write ONE focused pytest test that reproduces this bug. It must FAIL right now because of the bug (an assertion about behaviour; not an import error, typo or syntax error) and PASS once the bug is fixed. Put it in tests/test_repro_{slug}.py. Do not change application code in this step.

BUG REPORT:
{goal}

{context}"""

BUG_FIX = """Fix this bug with the smallest correct change. Change application code, not the reproduction test.

BUG REPORT:
{goal}

CODE (most relevant first):
{context}

REPRODUCTION TEST ({repro}) CURRENTLY FAILS WITH:
{failure}

{feedback}"""


def traceback_files(e, text):
    out = []
    for m in re.finditer(r'File "([^"]+)", line (\d+)', text):
        p = m.group(1).replace("\\", "/")
        if os.path.isabs(p) and p.startswith(e.root.replace("\\", "/")):
            p = os.path.relpath(p, e.root).replace("\\", "/")
        if os.path.isfile(os.path.join(e.root, p)) and p not in out and not p.startswith("tests/"):
            out.append(p)
    return out


class BugfixFlow(Flow):
    id, title = "bugfix", "Bug fix"
    blurb = "Bug report -> failing reproduction test -> locate -> minimal fix -> full tests -> review notes."
    domains = ["api", "data", "web"]
    steps = [S("repro", "Write a failing test", "generator", "llm"), S("confirm", "Confirm it fails for the right reason", "-", "code"),
             S("locate", "Locate (traceback + ranking)", "-", "code"), S("fix", "Minimal fix, loop on failure", "generator", "llm"),
             S("validate", "Repro + full tests", "-", "code"), S("review", "Risk review (advice only)", "reviewer", "llm")]
    produces = ["tests/test_repro_*.py", "fixed source files"]

    def run(self, e, params):
        goal = e.goal
        slug = re.sub(r"[^a-z0-9]+", "_", goal.lower())[:28].strip("_") or "bug"
        tb = traceback_files(e, goal)
        files = core.list_files(e.root, e.cfg)
        ranked = [f for f, s in core.rank_files(e.root, e.cfg, goal, files) if not f.startswith(("tests/", "docs/", "bench/")) and not f.endswith((".md", ".txt", ".json"))][:6]
        focus = tb + [f for f in ranked if f not in tb]
        e.say(f"[locate] suspect files: {', '.join(focus[:5]) or '(none found)'}")
        e.report(f"- Suspect files: {', '.join(focus[:6])}", "")
        repro_rel, failure = "", ""
        if params.get("repro") and os.path.isfile(os.path.join(e.root, params["repro"])):        # the hunt flow already proved the bug with this test
            repro_rel = params["repro"]
            st, log = core.run_tests(e.root, e.cfg, [repro_rel])
            failure = log[-1500:] if st == "fail" else ""
            e.say(f"[repro] using {repro_rel}: {'fails as expected' if st == 'fail' else st}")
        elif core.has_module("pytest") or e.cfg["tests"].get("command"):
            ctx = e.context(goal, 12000)
            r = e.call("generator", BUG_REPRO.format(goal=goal, slug=slug, context=ctx), label="repro")
            # the reproduction is a NEW file: an edit to the existing tests (easy with a DEF block) would make the suite itself the repro
            e.deny_prefixes = list(files)
            res = e.apply(r["text"], "reproduction test")
            e.deny_prefixes = []
            new = [x["rel"] for x in res if x["status"] == "applied" and x["rel"].startswith("tests/")]
            if e.review == "dry":
                return {"ok": True, "dry": True}
            if new:
                repro_rel = new[0]
                st, log = core.run_tests(e.root, e.cfg, [repro_rel])
                bad_fail = re.search(r"SyntaxError|ModuleNotFoundError|ImportError|NameError|IndentationError", log)
                if st == "fail" and not bad_fail:
                    failure = log[-1500:]
                    e.mark(r["id"], True, "fails for the right reason")
                    e.say("[+] reproduction test fails as it should")
                else:
                    why = "it passes already (cannot reproduce)" if st == "pass" else "it fails for the wrong reason:\n" + log[-500:]
                    e.mark(r["id"], False, why)
                    e.say(f"[!] reproduction test is not usable: {why[:100]}")
                    e.report(f"- Reproduction test not usable: {why[:200]}")
                    # A rejected repro must not stay in the suite: one that asserts the bug itself later fails every correct fix. Every
                    # file the step changed goes back -- a new test file is removed, an existing one (a FILE rewrite or a DEF appended into
                    # tests/test_x.py) gets its old content back, and so does any application file the step was not allowed to touch.
                    first = {}
                    for x in res:
                        if x["status"] == "applied" and x["rel"] not in first:
                            first[x["rel"]] = x
                    for rel, x in first.items():
                        p = os.path.join(e.root, rel)
                        try:
                            if x.get("existed"):
                                core.write_text(p, x.get("old", ""), bool(x.get("crlf")))
                                e.say(f"[-] {rel}: put back as it was (the reproduction test was not usable)")
                            else:
                                os.remove(p)
                                e.say(f"[-] {rel}: removed (the reproduction test was not usable)")
                            e.changed_files = [c for c in e.changed_files if c != rel]
                            e.created = [c for c in e.created if c != rel] if isinstance(e.created, list) else e.created
                        except OSError:
                            pass
                    repro_rel, failure = "", ""
        else:
            e.say("[i] pytest is not installed and no test command is set: skipping the reproduction test")
        feedback, fixed = "", False
        # the existing tests, and the files that decide which tests run (a fix that switches a test off is not a fix); a NEW root
        # conftest.py is blocked too, since it could skip or rewrite every test
        kept_tests = [f for f in files if arch_fakecode.is_test_file(f) or "/__tests__/" in "/" + f or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", f)
                      or os.path.basename(f) in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")] + ["conftest.py"]
        coach = arch_progress.Coach(e, "generator")
        best = BestState(e)
        k_bf = int(e.cfg["behavior"].get("stall_samples", 3) or 1)
        can_bon = k_bf > 1 and e.review in ("auto", "dry")
        for attempt in range(e.cfg["limits"]["max_revisions"] + 2):
            rescue = attempt == e.cfg["limits"]["max_revisions"] + 1
            if rescue and not (can_bon and not e.pins.get("generator")):
                break                                                   # the extra round is only a best-of-N rescue
            cx = e.context(goal + " " + failure[:500], None, always=focus[:3])
            prompt = BUG_FIX.format(goal=goal, repro=repro_rel or "(none)", failure=failure or "(no reproduction available; use the bug report)", feedback=feedback, context=cx)
            e.deny_prefixes = ([repro_rel] if repro_rel else []) + kept_tests      # the fix changes the code, never the tests that judge it
            if (coach.stalled or rescue) and can_bon and not e.pins.get("generator"):
                # stuck, or out of normal tries: several different attempts, each tested, the best kept (no more identical retries)
                r = best_of_n(e, "generator", prompt, f"fix-try{attempt + 1}", k_bf, [f for f in focus[:3]])
            else:
                r = e.call("generator", prompt, label=f"fix-try{attempt + 1}")
            res = e.apply(r["text"], f"fix try {attempt + 1}")
            if any(x["status"] == "quit" for x in res):
                return {"ok": False, "stopped": True}
            if e.review == "dry":
                return {"ok": True, "dry": True}
            best.note(res)
            changed = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
            if not changed:
                feedback = "FEEDBACK: nothing could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res) + "\n\n"
                e.mark(r["id"], False, feedback)
                feedback = coach.after(feedback, r["text"], feedback, [])
                continue
            v = verify(e, changed, run_tests=True)
            log_checks(e, f"fix try {attempt + 1}", v)
            e.mark(r["id"], v["ok"], v["text"])
            if v["ok"]:
                fixed = True
                break
            undone = best.judge(v) if e.cfg["behavior"].get("keep_best_try", True) else ""
            if undone:
                feedback = coach.after(f"FEEDBACK: your last change made things worse ({undone}), so it was UNDONE: the files are back to the version "
                                       "before it. What still fails in that version:\n" + best.still_fails() + "\n\n", r["text"], v["text"], changed)
                continue
            feedback = coach.after("FEEDBACK: after your last change this still fails:\n" + v["text"] + "\n\n", r["text"], v["text"], changed)
        coach.close()
        e.deny_prefixes = []
        if fixed and e.cfg["behavior"]["evaluator"] != "off":
            r = e.call("reviewer", "Review this bug fix for remaining risks (edge cases, regressions, other callers). Max 5 bullets, most severe first; say 'no concerns' if none.\n\nBUG: " + goal + "\n\nFILES CHANGED: " + ", ".join(e.changed_files) + "\n\n" + e.context(goal, 7000, always=e.changed_files[:3]), label="review")
            e.report("- Reviewer notes (advice only):", *["  " + ln for ln in r["text"].splitlines()[:10]])
        e.report("", f"- Bug {'fixed: repro and tests pass' if fixed else 'NOT fixed after the allowed attempts'}")
        return {"ok": fixed, "files": e.changed_files}


# ------------------------------------------------------------------------------------------ REFACTOR
REF_CHAR = """Write CHARACTERIZATION tests that pin down what the code does TODAY, so a refactor can be proven safe. Test the public behaviour of these files, including edge cases and error cases, asserting the CURRENT actual outputs (even if you think they are odd). Do NOT change application code. Put tests in tests/characterization/test_<name>.py. They must pass on the unmodified code.

REFACTOR GOAL (for context): {goal}
FILES IN SCOPE: {files}

CODE:
{context}

{feedback}"""

REF_DO = """Refactor as described. Behaviour must NOT change: the characterization tests and all existing tests must still pass. Do not edit tests/characterization/. Prefer many small, mechanical edits over rewrites.

REFACTOR GOAL: {goal}
FILES IN SCOPE: {files}
STRUCTURE BEFORE: {before}

CODE:
{context}

{feedback}"""


class RefactorFlow(Flow):
    id, title = "refactor", "Refactor / migrate"
    blurb = "Pin current behaviour with characterization tests -> refactor in small steps -> tests must stay green -> before/after structure report."
    steps = [S("scope", "Choose files in scope", "-", "code"), S("pin", "Characterization tests (must pass today)", "generator", "llm"),
             S("baseline", "Baseline metrics", "-", "code"), S("change", "Refactor, tests protected", "generator*", "llm"),
             S("verify", "All tests + metrics after", "-", "code"), S("review", "Risk review (advice only)", "reviewer", "llm")]
    produces = ["tests/characterization/*", "refactored source files"]

    def run(self, e, params):
        goal = e.goal
        explicit = [m for m in re.findall(r"[\w./-]+\.(?:py|js|ts|sql|html|css)\b", goal) if os.path.isfile(os.path.join(e.root, m))]
        files = explicit or [f for f, s in core.rank_files(e.root, e.cfg, goal) if not f.startswith("tests/") and s > 0][:4]
        if not files:
            e.say("[-] no files in scope; name the files in the goal")
            e.report("- No files in scope found.")
            return {"ok": False}
        flist = ", ".join(files)
        e.say(f"[scope] {flist}")
        e.report(f"- In scope: {flist}", "")
        can_test = core.has_module("pytest") or bool(e.cfg["tests"].get("command"))
        py_scope = any(f.endswith(".py") for f in files)
        pinned = False
        if can_test and py_scope:
            feedback = ""
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                r = e.call("generator", REF_CHAR.format(goal=goal, files=flist, feedback=feedback, context=e.context(goal, None, always=files[:3])), label=f"pin-try{attempt + 1}")
                res = e.apply(r["text"], "characterization tests")
                if e.review == "dry":
                    return {"ok": True, "dry": True}
                newt = [x["rel"] for x in res if x["status"] in ("applied", "identical") and x["rel"].startswith("tests/")]
                if not newt:
                    feedback = "FEEDBACK: no test file was written under tests/characterization/:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res) + "\n\n"
                    e.mark(r["id"], False, feedback)
                    continue
                st, log = core.run_tests(e.root, e.cfg)
                e.mark(r["id"], st == "pass", log[-300:])
                if st == "pass":
                    pinned = True
                    e.say("[+] current behaviour is pinned by passing tests")
                    break
                feedback = "FEEDBACK: the tests fail on the UNMODIFIED code, so they do not describe current behaviour. Fix the TESTS:\n" + log[-1400:] + "\n\n"
            e.report(f"- Characterization tests: {'pinned (pass on current code)' if pinned else 'NOT established'}")
            if not pinned:
                e.say("[!] could not pin behaviour; refusing to refactor without a safety net")
                e.report("- Stopped: no safety net. Add tests or set a test command, then retry.")
                return {"ok": False}
        elif can_test:
            st, _ = core.run_tests(e.root, e.cfg)
            pinned = st == "pass"
            e.report(f"- Using the project's own tests as the safety net: {st}")
            if st == "fail":
                e.say("[!] existing tests already fail; fix them first")
                return {"ok": False}
        else:
            e.say("[!] no test runner available: install pytest or set a test command in Settings. Refusing to refactor without a safety net.")
            e.report("- Stopped: no test runner.")
            return {"ok": False}
        before = ck.metrics_python(e.root, files)
        e.report(f"- Structure before: {before}")
        shape0 = code_shape(e.root, files)
        dups0 = duplicate_bodies(e.root, files) if DEDUP_WORDS.search(goal) else []
        e.deny_prefixes = ["tests/characterization/"]
        role = gen_role_for(files)
        feedback, ok = "", False
        for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
            r = e.call(role, REF_DO.format(goal=goal, files=flist, before=before, feedback=feedback, context=e.context(goal, None, always=files[:3])), label=f"refactor-try{attempt + 1}")
            res = e.apply(r["text"], f"refactor try {attempt + 1}")
            if any(x["status"] == "quit" for x in res):
                return {"ok": False, "stopped": True}
            changed = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
            if not changed:
                feedback = "FEEDBACK: nothing could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res) + "\n\n"
                e.mark(r["id"], False, feedback)
                continue
            v = verify(e, changed)
            # Green tests prove behaviour did not change; they do not prove the refactor HAPPENED. Seen live: a 4B model sent the file
            # back re-spaced, tests passed, and the run reported a refactor that changed nothing.
            if v["ok"] and shape0 and code_shape(e.root, files) == shape0:
                v = dict(v, ok=False, text="Your reply changed only spacing or formatting: the code itself is exactly what it was, so the "
                         "refactor was not done. Make the change the goal asks for: " + goal)
            still = duplicate_bodies(e.root, files) if dups0 else []
            if v["ok"] and dups0 and len(still) >= len(dups0):
                v = dict(v, ok=False, text="These functions still have the same body under different names: "
                         + "; ".join(f"{', '.join(n)} in {f}" for f, n in still[:4])
                         + ". The goal asks to remove that duplication: keep ONE (or replace them with one expression), point every caller "
                         "at it, and delete the others.")
            log_checks(e, f"refactor try {attempt + 1}", v)
            e.mark(r["id"], v["ok"], v["text"])
            if v["ok"]:
                ok = True
                break
            feedback = ("FEEDBACK: " + (v["text"] if v["text"].startswith(("Your reply changed only", "These functions still"))
                                        else "behaviour changed or checks failed. Make a smaller step that keeps every test green:\n" + v["text"]) + "\n\n")
        e.deny_prefixes = []
        after = ck.metrics_python(e.root, files)
        e.report(f"- Structure after: {after}")
        worse = [k for k in ("longest_function", "max_nesting") if after[k] > before[k]]
        if worse:
            e.report(f"- Note: {', '.join(worse)} got worse; check that this matches your goal.")
        if ok and e.cfg["behavior"]["evaluator"] != "off":
            r = e.call("reviewer", "Review this refactor for behaviour changes the tests might not catch. Max 5 bullets; 'no concerns' if none.\n\nGOAL: " + goal + "\nBEFORE: " + str(before) + "\nAFTER: " + str(after) + "\n\n" + e.context(goal, 7000, always=e.changed_files[:3]), label="review")
            e.report("- Reviewer notes (advice only):", *["  " + ln for ln in r["text"].splitlines()[:10]])
        e.report("", f"- Refactor {'complete: tests green' if ok else 'NOT completed'}")
        return {"ok": ok, "files": e.changed_files}


FLOWS = {f.id: f for f in (DesignFlow(), BuildFlow(), BugfixFlow(), RefactorFlow())}


# ------------------------------------------------------------------------------------------ router
KEYWORDS = {
    "design": r"\b(design|architect(?:ure)?|adr|decision record|spec(?:ification)?|plan the|blueprint|diagram|data model|api contract|openapi|propose|evaluate options|trade-?offs?)\b",
    "bugfix": r"\b(bug|fix|broken|crash(?:es)?|error|exception|traceback|fails?|failing|regression|wrong|incorrect|doesn'?t work|not working)\b",
    "refactor": r"\b(refactor|clean ?up|restructure|reorganize|simplify|rename|extract|deduplicate|migrate|upgrade|port|modernize|split (?:up|into))\b",
    "build": r"\b(build|create|implement|add|write|make|develop|scaffold|generate|set ?up|new)\b",
    "new": r"\b(from scratch|new project|bootstrap|start(?:ing)? (?:a|an|the) (?:new )?(?:project|app|service|site)|scaffold|boilerplate|starter|init(?:ialize)? a)\b",
    "feature": r"\b(feature|end[- ]to[- ]end|full[- ]?stack|vertical slice|ui and api|screen and endpoint)\b",
    "test": r"\b(add|write|improve|more|increase|missing|better)\b[^.]{0,30}\btests?\b|\bcoverage\b|\bmutation\b|\bunit tests?\b|\btest suite\b|\bflaky\b",
    "optimize": r"\b(optimi[sz]e|optimi[sz]ation|faster|slow(?:er|ly)?|performance|speed ?up|latency|throughput|bottleneck|profil(?:e|ing)|n\+1|memory usage|too many queries)\b",
    "hunt": r"\b(find bugs|hunt|bug hunt|audit|vulnerabilit\w*|look for bugs|what could break|edge cases?|sanity check|stress)\b",
    "review": r"\b(code review|review (?:the|this|my)|critique|assess|health check)\b",
    "secure": r"\b(secur(?:e|ity)|harden(?:ing)?|cwe|owasp|secrets? scan|injection|xss|csrf|insecure)\b",
    "cicd": r"\b(ci/cd|cicd|ci pipeline|ci workflow|continuous (?:integration|delivery|deployment)|github actions?|gitlab[- ]ci|\.github/workflows|workflows?\.ya?ml|release (?:pipeline|automation|workflow)|deploy(?:ment)? pipeline|build pipeline|pipeline (?:fails?|failing|is red|broken)|ci\b)",
    "config": r"\b(dockerfile|docker ?compose|yaml|\.toml|lint(?:ing)? config|config(?:uration)? harden\w*|pin(?:ned)? version)\b",
    "network": r"\b(network plan|subnet|cidr|vlan|firewall|ingress|egress|port expos\w*|segmentation|topology)\b",
    "ml": r"\b(ml pipeline|machine learning|model train(?:ing)?|dataset|feature engineering|data leak(?:age)?|inference|training pipeline)\b",
    "pentest": r"\b(pentest|penetration test|red team|blue team|attack path|scope\.json|rules of engagement|authorized target)\b",
    "a11y": r"\b(accessib\w*|a11y|screen ?readers?|alt text|aria[- ]labels?|wcag|keyboard (?:access\w*|navigation|users?))\b",
    "types": r"\b(type hints?|type annotations?|annotate (?:the|every|all)|mypy|pyright)\b",
    "document": r"\b(docstrings?|document (?:the|this|every|all|each)|write (?:the )?(?:docs|documentation)|documentation for)\b",
    "harden": r"\b(error handling|bare except|swallow\w* (?:errors?|exceptions?)|fail (?:safely|loudly)|robust(?:ness)?|resilien\w*|add timeouts?|input validation)\b",
    "migrate": r"\b(migrate (?:from|off|away)|switch(?:ing)? from \S+ to|move (?:off|away from)|replace (?:every|all) (?:uses?|calls?) of|port (?:from|off))\b",
    "deps": r"\b(dependenc(?:y|ies) (?:audit|check|review)|audit (?:the )?dependenc\w+|unused (?:packages?|dependenc\w+)|requirements\.txt|unpinned)\b",
}
PRIORITY = ["pentest", "secure", "network", "cicd", "config", "ml", "a11y", "types", "document", "harden", "migrate", "deps", "bugfix", "refactor", "design", "hunt", "optimize", "test", "review", "feature", "new", "build"]


def route(e, goal, forced=None):
    """-> (flow_id, domains, why). The router MODEL decides (behavior.routing = model, the default); the keyword match is given to it as a hint and is
    the fallback when no model answers. behavior.routing = keywords restores the old rule-first order (the model only breaks ties)."""
    doms = detect_domains(e, goal)
    if forced and forced in FLOWS:
        return forced, doms, "chosen by you"
    g = goal.lower()
    empty = not core.list_files(e.root, e.cfg)
    score = {k: len(re.findall(p, g)) for k, p in KEYWORDS.items()}
    if "Traceback" in goal or re.search(r'File ".*", line \d+', goal):
        score["bugfix"] += 3
    if re.search(r"github actions?|gitlab[- ]ci|ci/cd|\bcicd\b|\.github/workflows|\.gitlab-ci", g):
        score["cicd"] += 2                              # these name a pipeline outright: stronger evidence than one ordinary keyword
    if empty:                                          # nothing to test, optimise or review yet; a plain "build" means: start the project
        for k in ("test", "optimize", "hunt", "review"):
            score[k] = 0
        score["new"] += score["build"]
        score["build"] = 0
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], PRIORITY.index(kv[0])))
    top, second = ranked[0], ranked[1]
    beats_build = second[0] == "build" and top[0] != "build" and top[1] >= second[1]      # "add tests" matches 'add' too: the specific flow wins
    specific_wins = top[0] == "cicd" and second[0] in ("bugfix", "build", "refactor", "feature", "new", "test", "review") and top[1] >= second[1]   # "fix the failing GitHub Actions workflow" is a pipeline job
    strong = top[1] > 0 and (top[1] >= second[1] + 2 or beats_build or specific_wins or second[1] == 0)
    kw_why = f"keywords: {', '.join(m.group(0) for m in re.finditer(KEYWORDS[top[0]], g))[:80]}" if top[1] > 0 else ""
    mode = (e.cfg.get("behavior") or {}).get("routing", "model")
    if strong and mode == "keywords":
        return top[0], doms, kw_why
    hint = (f"\n\nA simple keyword matcher suggests: {top[0]} ({kw_why}). Treat that as a weak hint; decide from what the person actually wants." if top[1] > 0 else "") + \
           ("\nThe project folder is empty." if empty else "\nThe project already has files.")
    try:
        r, j = e.ask_json("router", "Pick the workflow for this request. new = start a project from nothing (a template plus a first build); design = decide structure/architecture/contracts before code; "
                                    "build = create new functionality; feature = one feature across data, API and UI; bugfix = something is broken; refactor = change structure without changing behaviour; "
                                    "test = write better tests; optimize = make it faster; hunt = look for hidden bugs; review = assess code quality; "
                                    "secure = fix security findings; config = harden Dockerfile/CI/config; network = fix a network/firewall plan; "
                                    "ml = build/fix an ML pipeline; pentest = turn findings+scope you supply into a scope-checked attack-path write-up. "
                                    'Answer ONLY JSON {"flow": "<one of: ' + "|".join(FLOWS) + '>", "why": "short"}.\n\nREQUEST: ' + goal + hint, schema=arch_schemas.router(FLOWS))
        if isinstance(j, dict) and j.get("flow") in FLOWS:
            pick = j["flow"]
            if empty and pick in ("test", "optimize", "hunt", "review", "refactor", "bugfix"):
                pick = "new"                                  # nothing exists to test, review or fix yet: start the project (checked in code)
            agree = "agrees with" if pick == top[0] else "differs from"
            return pick, doms, f"router model ({agree} the keyword hint): " + str(j.get("why", ""))[:100]
    except (LLMError, BudgetExceeded, LoopDetected):
        pass
    if strong:
        return top[0], doms, kw_why + " (no model answered)"
    return (top[0] if top[1] > 0 else ("new" if empty else "build")), doms, "best keyword match" if top[1] > 0 else "default"


def run_flow(e, flow_id, params=None):
    params = params or {}
    flow = FLOWS[flow_id]
    t0 = time.time()
    e.state["params"] = json.loads(json.dumps({k: v for k, v in params.items() if k != "resume"}, default=str))
    e._write_state()
    model_down = False
    if not (e.state.get("todo")) and getattr(flow, "steps", None):
        try:                                              # Python flows have no per-step hook: the list is set now and closed at the end
            e.todo_set([{"id": f"s{i}", "title": str(t)} for i, t in enumerate(flow.steps, 1)])
        except Exception:
            pass
    try:
        out = flow.run(e, params)
    except (LLMError, BudgetExceeded, LoopDetected) as ex:
        e.say(f"[!] stopped: {ex}")
        e.report("", f"- Stopped: {ex}")
        out = {"ok": False, "error": str(ex)}
        model_down = isinstance(ex, LLMError)
    except BaseException:
        e._bring_back()                                   # Ctrl+C or a harness bug: a server that stepped aside still comes back
        raise
    for it in e.state.get("todo") or []:
        if it.get("status") in ("pending", "doing"):
            it["status"] = "done" if out.get("ok") else "failed"
    e.finish(out.get("ok"), out.get("error"))
    verdict = "not_sure" if out.get("unsure") else ("pass" if out.get("ok") else "fail")
    try:                                                  # the run's own conclusion, as a record: the live view shows it without parsing prose
        e._trace({"type": "verdict", "t": time.time(), "flow": flow_id, "verdict": verdict,
                  "why": str(out.get("error") or out.get("why") or "")[:400], "meter": dict(e.meter)})
        import arch_hooks
        arch_hooks.fire(e, "after_verdict", files=sorted(set(e.changed_files) | set(e.created)), verdict=verdict)
    except Exception:
        pass
    if verdict != "pass" and not out.get("dry") and not out.get("stopped") and not model_down:
        # After the verdict is on record, so a supervisor's time limit can never cost the verdict. Not when the model itself was the
        # problem (unreachable, timed out): asking it to explain would only fail again, slowly.
        import arch_postmortem                            # advice for the person and the next attempt; never changes the verdict
        e._local_restarts = 99                            # and it may never restart the server
        arch_postmortem.explain(e, verdict, str(out.get("error") or out.get("why") or ""))
        e._bring_back()                                   # its call may have made room again
    e.report(f"- Models used: {dict(e.models_used)}", f"- Time: {round(time.time() - t0)}s, model calls: {e.calls}")
    if out.get("ok") and not out.get("dry") and e.changed_files and e.cfg["behavior"].get("git") == "commit":
        core.git_checkpoint(e.root, f"architect {flow_id}: {e.goal}", e.say)
    return out


import arch_flows_pro  # noqa: E402,F401  (registers the extra flows into FLOWS)
import arch_flows_fake  # noqa: E402,F401  (registers the purge flow)
import arch_flows_ci  # noqa: E402,F401  (registers the cicd flow)
import arch_pipeline  # noqa: E402,F401  (registers the ship flow)

try:                                                    # flows as data (flows/*.yaml): see arch_dataflow; which ones win is Settings > Flows > flows.engine
    import arch_dataflow
    DATA_FLOWS = arch_dataflow.register(FLOWS, (core.load_config().get("flows") or {}).get("engine", "legacy"), say=lambda m: None)
except Exception as _ex:                                # a broken YAML must never take the engine down
    DATA_FLOWS = []


# [localintel-bridge] begin: app-specific layer (removed by apply_localintel_layer.py --remove)
try:
    import localintel_arch  # noqa: F401
except ImportError:
    pass
# [localintel-bridge] end
