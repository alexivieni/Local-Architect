"""Error-driven repair: a retry after a failing test carries ONLY what the failure is about -- the failing assertion/traceback
tail, the function(s) under test, and the request's contract -- not the whole project context.

Stable part first (the contract, then the functions), the failure tail last, so the model server's prompt cache keeps the prefix.
Setting: behavior.repair_focus ("on" | "off", default "off"). When the feedback holds no recognisable test failure (an apply error,
a missing file) nothing is shrunk and the normal context is used.
"""
import ast
import os
import re

TAIL_CHARS = 1600
FUNC_CHARS = 3200
_MARKERS = re.compile(r"(Traceback \(most recent call last\)|AssertionError|^FAILED |^E {2,}|^=+ FAILURES =+|^FAIL: |^ERROR: |\bError\b.*:)", re.M)
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_PYTEST_LOC = re.compile(r"^([\w./\\-]+\.py):(\d+): ", re.M)
_FAILED_NAME = re.compile(r"(?:FAILED|FAIL:|ERROR:?)\s+([\w./\\-]+\.py)?(?:::)?(\w+)?")


def enabled(cfg):
    v = (cfg.get("behavior") or {}).get("repair_focus", "off")
    return v is True or str(v).lower() in ("on", "true", "1", "yes")


def failure_tail(text, limit=TAIL_CHARS):
    """The failing assertion / traceback tail of a test output, or "" when the text holds no test failure."""
    text = text or ""
    ms = list(_MARKERS.finditer(text))
    if not ms:
        return ""
    tb = [m.start() for m in ms if m.group(0).startswith("Traceback")]
    start = tb[-1] if tb else ms[0].start()
    tail = text[start:].strip()
    if len(tail) > limit:
        tail = "...\n" + tail[-limit:]
    return tail


def _def_source(path, line=None, name=None):
    """The source of the top-level function/class (or method) holding `line` / called `name` in a .py file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return ""
    lines = src.splitlines()
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if line is not None and node.lineno <= line <= end:
            if best is None or node.lineno >= best.lineno:           # the innermost definition
                best = node
        elif line is None and node.name == name and best is None:
            best = node
    if best is None:
        return ""
    return "\n".join(lines[best.lineno - 1:getattr(best, "end_lineno", best.lineno)])


def functions_under_test(root, text, is_test=None, limit=FUNC_CHARS):
    """-> [(rel, name, source)] for project (non-test) functions named in the traceback frames, nearest to the failure first."""
    is_test = is_test or (lambda rel: bool(re.search(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.py$|conftest\.py$)", rel)))
    root_abs = os.path.realpath(root)
    seen, out, used = set(), [], 0
    frames = [(f, int(n), fn) for f, n, fn in _FRAME.findall(text or "")]
    frames += [(f, int(n), None) for f, n in _PYTEST_LOC.findall(text or "")]
    for f, n, fn in reversed(frames):
        p = f if os.path.isabs(f) else os.path.join(root, f)
        p = os.path.realpath(p)
        if not p.startswith(root_abs + os.sep) or not p.endswith(".py") or not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, root_abs).replace("\\", "/")
        if is_test(rel) or rel.startswith(".arch/"):
            continue
        src = _def_source(p, line=n) or (_def_source(p, name=fn) if fn else "")
        if not src:
            continue
        key = (rel, src.splitlines()[0])
        if key in seen:
            continue
        seen.add(key)
        if used + len(src) > limit:
            src = src[:max(0, limit - used)]
            if not src:
                break
        used += len(src)
        out.append((rel, src.splitlines()[0].strip(), src))
    return out


_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.py$)")
_FAILED_NODE = re.compile(r"^(?:FAILED|ERROR)\s+([\w./\-]+\.py)", re.M)


def _norm(p):
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def failing_test_files(text):
    """The test files named by pytest's FAILED/ERROR lines (else by its `file.py:N:` locations), normalised. -> [rel]"""
    fs = _FAILED_NODE.findall(text or "") or [f for f, _ in _PYTEST_LOC.findall(text or "")]
    out = []
    for f in fs:
        f = _norm(f)
        if f not in out:
            out.append(f)
    return out


def actual_lines(text, cap=900):
    """pytest's assertion-rewrite lines (`E  assert 3 == 4`, `E  +  where 3 = f(...)`): the expectation and the ACTUAL value."""
    lines = [m.strip() for m in re.findall(r"^E {2,}(.*)$", text or "", re.M) if m.strip()]
    return "\n".join(lines)[:cap]


def test_or_code(e, feedback, changed=()):
    """Every failing test file is one THIS run created or edited -> the expectation itself may be what is wrong. -> info dict or None."""
    fails = failing_test_files(feedback)
    if not fails or not all(_TEST_PATH.search(f) for f in fails):
        return None
    mine = {_norm(x) for x in list(changed or ()) + list(getattr(e, "changed_files", None) or []) + list(getattr(e, "created", None) or [])}
    if not all(f in mine for f in fails):
        return None
    ev = actual_lines(feedback)
    if "assert" not in ev.lower():
        return None
    return {"tests": fails, "evidence": ev}


def record_decision(e, applied):
    """After a test_or_code try: which side did the model change? (trace: type repair, mode test_or_code, decision test|code|both|none)"""
    info = getattr(e, "_repair_toc", None)
    if not info:
        return None
    e._repair_toc = None
    ap = [_norm(x) for x in (applied or [])]
    t = [x for x in ap if x in info["tests"]]
    c = [x for x in ap if x not in info["tests"]]
    dec = "none" if not ap else "both" if (t and c) else "test" if t else "code"
    try:
        e._trace({"type": "repair", "t": __import__("time").time(), "mode": "test_or_code", "decision": dec, "tests": info["tests"], "applied": ap[:8]})
    except Exception:
        pass
    return dec


def focused(e, goal, feedback, changed=()):
    """-> (feedback_short, context) for a retry, or None to leave the normal prompt alone."""
    if not enabled(e.cfg):
        return None
    tail = failure_tail(feedback)
    if not tail:
        return None
    toc = test_or_code(e, feedback, changed)
    e._repair_toc = toc
    funcs = functions_under_test(e.root, feedback)
    if not funcs and changed:                         # no frames into project code: the outline of what the last try changed
        import arch_core as core
        funcs = [(rel, "outline", core.outline(os.path.join(e.root, rel))) for rel in list(changed)[:3]
                 if rel.endswith(".py") and os.path.isfile(os.path.join(e.root, rel))]
    parts = ["=== the request's contract ===\n" + str(goal).strip()[:1500]]      # stable first
    for rel, _, src in funcs:
        parts.append(f"=== {rel} (function under test) ===\n{src}")
    ctx = "\n\n".join(parts)
    head = (feedback.strip().splitlines() or ["FEEDBACK: this still fails:"])[0][:240]
    short = f"{head}\n{tail}\n\n"
    if toc:
        short += ("TEST OR CODE? The only failing tests (" + ", ".join(toc["tests"][:4]) + ") are ones YOU wrote or edited in this run. Failing assertion "
                  "and the ACTUAL value:\n" + toc["evidence"] + "\nDecide which side is wrong: the test's expected value (does it match what the request "
                  "says? recount it by hand) or the code. State your decision in one line, then fix ONLY that side. Do not bend correct code toward "
                  "a wrong expectation, and do not loosen a correct test.\n\n")
    try:
        e._trace({"type": "repair", "t": __import__("time").time(), "functions": [f"{r}:{n}" for r, n, _ in funcs],
                  "tail_chars": len(tail), "context_chars": len(ctx),
                  **({"mode": "test_or_code", "tests": toc["tests"]} if toc else {})})
    except Exception:
        pass
    return short, ctx
