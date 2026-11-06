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


def focused(e, goal, feedback, changed=()):
    """-> (feedback_short, context) for a retry, or None to leave the normal prompt alone."""
    if not enabled(e.cfg):
        return None
    tail = failure_tail(feedback)
    if not tail:
        return None
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
    try:
        e._trace({"type": "repair", "t": __import__("time").time(), "functions": [f"{r}:{n}" for r, n, _ in funcs],
                  "tail_chars": len(tail), "context_chars": len(ctx)})
    except Exception:
        pass
    return short, ctx
