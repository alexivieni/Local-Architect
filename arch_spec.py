"""The spec a request states, extracted by code (no model), and the acceptance-test step built on it (behavior.test_first).

spec(goal) -> {"interface", "usages", "invariants", "files", "exercises"}: deterministic and conservative -- only what the request says in
so many words. The acceptance step (BuildFlow, when behavior.test_first = on and the request states an interface) asks the tester role
for tests/test_acceptance.py from the request and this spec alone, checks the file in code, and locks it for the builder. Because a wrong
acceptance test must never block a correct build forever, drop_rule() removes failing acceptance test functions ONLY when they are the
only failures left and the request's own interface check and contract smoke pass; the builder's files are never touched.
"""
import ast
import os
import re
import sys
import time

ACCEPTANCE = "tests/test_acceptance.py"

_SPAN = re.compile(r"`([^`\n]{3,200})`")
# Statements a test can check, in the shapes requests write them. Each match keeps the clause it sits in.
_INVARIANTS = [
    re.compile(r"\bthe same \w+ always (?:gets|returns|gives|yields|maps to) the same \w+", re.I),
    re.compile(r"\b\w+ start(?:s)? at \d+ and (?:increase|increment|go up)", re.I),
    re.compile(r"\bexit(?:s|ing)? (?:with (?:status|code) )?\d+\b", re.I),
    re.compile(r"\braises? [A-Z]\w*(?:Error|Exception)\b", re.I),
    re.compile(r"\bsorted by \w+", re.I),
    re.compile(r"\brounded to \d+ decimals?\b", re.I),
    re.compile(r"(?:->|returns?)\s*\d{3}\b"),
    re.compile(r"\bcreated if missing\b", re.I),
    re.compile(r"\b(?:persists|is removed|is NOT counted|is not counted|changes nothing)\b"),
]


def _clauses(goal):
    """The request cut into clauses at sentence ends and semicolons (a backtick span is never cut)."""
    spans = []
    hold = re.sub(r"`[^`\n]*`", lambda m: spans.append(m.group(0)) or f"\x00{len(spans) - 1}\x00", goal or "")
    out = []
    for c in re.split(r"(?<=[.;])\s+(?=[A-Za-z`(\"\x00])", hold):
        c = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], c).strip().rstrip(";").strip()
        if c:
            out.append(c)
    return out


def invariants(goal):
    out = []
    for c in _clauses(goal):
        if any(p.search(c) for p in _INVARIANTS):
            c = c.rstrip(".")
            if c not in out:
                out.append(c[:240])
    return out


def usages(goal):
    """The backticked lines that USE the interface: imports, calls, method calls and command lines."""
    import arch_flows as fl
    out = []
    for s in _SPAN.findall(goal or ""):
        s = s.strip().rstrip(";")
        if (fl._FROM.match(s) or fl._MAIN.match(s) or fl._CALL.match(s) or fl._METH.match(s) or fl._ASSIGN.match(s)
                or re.match(r"^python3?\s", s)):
            if s not in out:
                out.append(s)
    return out


def spec(goal):
    import arch_flows as fl
    sp = {"interface": fl.interface_contract(goal), "usages": usages(goal), "invariants": invariants(goal), "files": fl.named_files(goal)}
    try:
        import arch_smoke
        sp["exercises"] = [x.get("display", "") for x in arch_smoke.plan(goal)[0]]
    except Exception:
        sp["exercises"] = []
    return sp


def has_interface(sp):
    i = (sp or {}).get("interface") or {}
    return bool(i.get("imports") or i.get("mains"))


def spec_text(sp):
    i = sp.get("interface") or {}
    lines = ["INTERFACE (import and run exactly these; nothing else exists):"]
    lines += [f"- from {m} import {n}" for m, n in i.get("imports") or []]
    lines += [f"- python -m {m} ..." for m in i.get("mains") or []]
    lines += [f"- {t}" for _n, _p, _k, t in i.get("calls") or []]
    lines += [f"- {t}" for *_x, t in i.get("methods") or []]
    lines += ["USAGES THE REQUEST SHOWS:"] + ([f"- `{u}`" for u in sp.get("usages") or []] or ["- (none)"])
    lines += ["STATEMENTS TO TEST:"] + ([f"- {s}" for s in sp.get("invariants") or []] or ["- (none beyond the usages)"])
    if sp.get("files"):
        lines += ["FILES THE REQUEST NAMES: " + ", ".join(sp["files"])]
    return "\n".join(lines)


def _allowed(sp):
    i = sp.get("interface") or {}
    names = {n for _m, n in i.get("imports") or []}
    mods = {m for m, _n in i.get("imports") or []} | set(i.get("mains") or [])
    return names, mods, {m.split(".")[0] for m in mods}


def check_text(text, sp):
    """-> [problem]: empty when the file parses, imports only stdlib, pytest and the names the spec states, and every test asserts."""
    try:
        tree = ast.parse(text)
    except SyntaxError as ex:
        return [f"the file does not parse: {ex}"]
    names, mods, roots = _allowed(sp)
    std = set(getattr(sys, "stdlib_module_names", ())) | {"pytest"}
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if n.level:
                bad.append(f"relative import from {'.' * n.level}{mod}")
            elif mod.split(".")[0] in std:
                continue
            elif mod in mods or mod.split(".")[0] in roots:
                bad += [f"`from {mod} import {a.name}`: {a.name} is not a name the request states" for a in n.names if a.name not in names]
            else:
                bad.append(f"`from {mod} import ...`: {mod} is not a module the request states")
        elif isinstance(n, ast.Import):
            bad += [f"`import {a.name}`: not a module the request states" for a in n.names
                    if a.name.split(".")[0] not in std and a.name.split(".")[0] not in roots]
    tests = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
    if not tests:
        bad.append("no test functions")
    import arch_fakecode
    bad += [f"{x['msg']}" for x in arch_fakecode.scan_text(ACCEPTANCE, text) if x.get("gate")]
    return bad


def _code_block(text, rel):
    m = re.search(r"FILE:\s*`?" + re.escape(rel) + r"`?\s*\n```[\w]*\n(.*?)```", text or "", re.S)
    if not m:
        blocks = re.findall(r"```(?:python|py)?\n(.*?)```", text or "", re.S)
        return blocks[0] if len(blocks) == 1 else None
    return m.group(1)


def write_acceptance(e, goal, sp, template, tries=2):
    """The tester writes tests/test_acceptance.py from the request and the spec only. Checked in code; written only when it passes.
    -> the path written, or "" (the build then runs as it would without it)."""
    import arch_core as core
    e.say(f"[test first] the tester writes {ACCEPTANCE} from the request's stated interface, before any code")
    prompt, feedback, problems = template.format(goal=goal, spec=spec_text(sp), path=ACCEPTANCE), "", []
    t0 = time.time()
    for t in range(tries):
        try:
            r = e.call("tester", prompt + feedback, label=f"acceptance-try{t + 1}")
        except Exception as ex:
            problems = [f"the tester could not be asked: {type(ex).__name__}: {str(ex)[:120]}"]
            break
        body = _code_block(r["text"], ACCEPTANCE)
        problems = ["no `FILE: tests/test_acceptance.py` block"] if body is None else check_text(body, sp)
        e.mark(r["id"], not problems, "; ".join(problems)[:500] or "acceptance tests accepted")
        if not problems:
            p = os.path.join(e.root, *ACCEPTANCE.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            core.write_text(p, body)
            break
        feedback = "\n\nFEEDBACK: that file was refused:\n" + "\n".join("- " + x for x in problems[:8]) + "\nSend the corrected file.\n"
    ok = not problems
    e._trace({"type": "check", "t": time.time(), "flow": e.flow, "step": "acceptance", "gate": "acceptance_tests", "ok": ok,
              "text": ("written and locked: " + ACCEPTANCE) if ok else "; ".join(problems)[:2000], "metrics": {}, "secs": round(time.time() - t0, 2)})
    e.report(f"- Test first: {'the tester wrote ' + ACCEPTANCE + ' from the request alone; the builder may not edit it' if ok else 'not used (' + '; '.join(problems)[:200] + ')'}")
    return ACCEPTANCE if ok else ""


def failing_tests(e):
    """-> set of failing node ids ("tests/x.py::name", or "tests/x.py" for a file that does not load), or None when this cannot tell
    (a custom test command, no pytest)."""
    import arch_core as core
    if e.cfg["tests"].get("command") or not core.has_module("pytest"):
        return None
    rc, out = core.run_cmd([sys.executable, "-m", "pytest", "-q", "--tb=no", "-rfE", "-p", "no:cacheprovider"], e.root, e.cfg["limits"]["test_timeout"])
    if rc in (0, 5):
        return set()
    ids = {m.group(1).replace("\\", "/") for m in re.finditer(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", out, re.M)}
    return ids or None                                              # failed but nothing parsable: cannot tell


def only_acceptance(ids, rel=ACCEPTANCE):
    return bool(ids) and all(i == rel or i.startswith(rel + "::") for i in ids)


def only_acceptance_fails(e, v, rel=ACCEPTANCE):
    """Every other check of this verify passed, and every failing test is in the acceptance file."""
    if v.get("ok") or any(not r["ok"] and r["name"] != "project tests pass" for r in v.get("results") or []):
        return False
    return only_acceptance(failing_tests(e), rel)


def drop_failing(e, ids, rel=ACCEPTANCE):
    """Remove the acceptance test functions in `ids` (the whole file when it does not load or nothing is left). -> names dropped."""
    import arch_core as core
    p = os.path.join(e.root, *rel.split("/"))
    names = {i.split("::")[-1].split("[")[0] for i in ids if "::" in i}
    src = core.read_text(p)[0] if os.path.isfile(p) else ""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is None or rel in ids:
        os.remove(p) if os.path.isfile(p) else None
        return ["(the whole file)"]
    lines, drop = src.split("\n"), []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
            drop.append(n)
        elif isinstance(n, ast.ClassDef):
            drop += [m for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in names]
    for n in sorted(drop, key=lambda x: -x.lineno):
        start = min([n.lineno] + [d.lineno for d in n.decorator_list]) - 1
        del lines[start:n.end_lineno]
    src = re.sub(r"\n{4,}", "\n\n\n", "\n".join(lines))
    left = [n for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
    if left:
        core.write_text(p, src)
    else:
        os.remove(p)
    return sorted(n.name for n in drop)


def drop_rule(e, goal, verify_fn, contract_fn, changed, rel=ACCEPTANCE):
    """After every try of the last sprint: when the ONLY failures are in the acceptance file AND the request's interface check and its
    contract smoke both pass, the failing acceptance tests are taken to be wrong and dropped. -> (ok, why). Nothing else is ever removed."""
    if not os.path.isfile(os.path.join(e.root, *rel.split("/"))):
        return False, "no acceptance file"
    v = verify_fn(e, changed)
    ids = failing_tests(e)
    why = None
    import arch_flows as fl
    if v.get("ok"):
        why = "the tests pass; what failed is not the acceptance file"
    elif any(not os.path.isfile(os.path.join(e.root, f)) for f in fl.named_files(goal)):
        why = "a file the request names is missing"
    elif any(not r["ok"] and r["name"] != "project tests pass" for r in v.get("results") or []):
        why = "other checks fail too"
    elif not only_acceptance(ids, rel):
        why = "tests outside the acceptance file fail too" if ids else "the failing tests could not be told apart"
    else:
        cok, ctext = contract_fn(e, goal)
        if not cok:
            why = "the request's interface check fails"
        else:
            try:
                import arch_smoke
                sok, _st = arch_smoke.smoke_contract(e, goal)
            except Exception as ex:
                sok = None
            if sok is not True:
                why = "the contract smoke did not pass" if sok is False else "the contract smoke could not exercise the request"
    if why:
        e._trace({"type": "check", "t": time.time(), "flow": e.flow, "step": "acceptance", "gate": "acceptance_drop", "ok": False,
                  "text": "kept the acceptance tests: " + why, "metrics": {}, "secs": 0})
        return False, why
    dropped = drop_failing(e, ids, rel)
    v2 = verify_fn(e, changed)
    msg = (f"dropped {len(dropped)} acceptance test(s) that failed on a build whose interface check and contract smoke pass: "
           + ", ".join(dropped[:8]))
    e._trace({"type": "check", "t": time.time(), "flow": e.flow, "step": "acceptance", "gate": "acceptance_drop", "ok": bool(v2.get("ok")),
              "text": msg, "metrics": {"dropped": len(dropped)}, "secs": 0})
    e.say(f"[test first] {msg}")
    e.report(f"- Test first: {msg}")
    return bool(v2.get("ok")), msg
