"""Fake-code detector: finds code that LOOKS finished but is not -- stubs, tests that cannot fail, errors swallowed so
nothing can go wrong, functions that pretend to do work, dummy values -- using real AST and tokenizer parsing (Python).

    python architect.py fakecode [--root PATH] [--json]       scan; exit 1 if any gate-level finding
    python architect.py run "Remove fake code in billing/" --flow purge     have the team fix or delete what it finds

Same principle as the rest of the Architect: CODE decides. A finding names a rule, a file and a line; "removed" is proven by
scanning again. Two levels:
  * high  -> gate: `behavior.fake_code=fail` (default) makes the model fix these in files a step changed;
  * medium/low -> reported, never block (they have legitimate uses; a person decides).
Conservative on purpose: abstract methods, Protocols, overloads, dunder hooks, base-class templates and exception classes are
exempt, because a false alarm teaches people to ignore the check.
"""
import ast
import io
import os
import re
import tokenize

SEV_RANK = {"high": 3, "medium": 2, "low": 1}
PLACEHOLDER = re.compile(r"lorem ipsum|\bTODO\b|\bFIXME\b|\bXXX\b|implement me|placeholder|dummy data|fake data|mock data|your[_ -]?api[_ -]?key[_ -]?here|changeme", re.I)  # fakecode: allow (this line defines or reports the marker)
PRETEND_NAMES = re.compile(r"^(send|save|delete|upload|download|charge|pay|process_payment|connect|sync|publish|deploy|notify|email|submit|verify|authenticate|encrypt|backup|restore)(_|$)", re.I)
LOG_FUNCS = {"print", "log", "debug", "info", "warning", "warn", "error", "exception"}
MOCK_MODULES = {"mock", "unittest.mock", "faker", "pytest_mock"}
HOOK_NAMES = {"setUp", "tearDown", "setUpClass", "tearDownClass", "setup", "teardown"}


def is_test_file(rel):
    r = rel.replace("\\", "/")
    base = os.path.basename(r)
    return "/tests/" in "/" + r or base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"


def _f(rule, sev, rel, line, msg, gate=None):
    return {"rule": rule, "severity": sev, "file": rel, "line": line, "msg": msg, "gate": (sev == "high") if gate is None else gate}


def _decorator_names(fn):
    out = set()
    for d in fn.decorator_list:
        n = d
        if isinstance(n, ast.Call):
            n = n.func
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return out


def _is_doc(stmt):
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _body_core(fn):
    """Statements after an optional docstring."""
    b = list(fn.body)
    if b and _is_doc(b[0]):
        b = b[1:]
    return b


def _is_ellipsis(stmt):
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis


def _class_is_template(cls):
    if cls is None:
        return False
    if re.search(r"(Base|Mixin|Interface|Protocol|ABC|Abstract)$", cls.name):
        return True
    for b in cls.bases:
        n = b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "")
        if n in ("ABC", "Protocol", "ABCMeta", "Exception", "BaseException", "Enum", "TypedDict", "NamedTuple") or n.endswith("Error") or n.endswith("Exception"):
            return True
    return False


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _call_name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _has_assertion(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Call):
            name = _call_name(n).lower()
            if name.startswith(("assert", "check", "verify", "expect", "should", "fail")) or name in ("raises", "warns", "approx", "deprecated_call"):
                return True
        if isinstance(n, ast.With):
            for it in n.items:
                c = it.context_expr
                if isinstance(c, ast.Call) and _call_name(c).lower() in ("raises", "warns", "assertraises", "assertwarns", "assertlogs", "assertraisesregex"):
                    return True
    return False


def _trivial_assert(a):
    t = a.test
    if isinstance(t, ast.Constant) and bool(t.value):
        return True
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not) and isinstance(t.operand, ast.Constant) and not t.operand.value:
        return True
    if isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], (ast.Eq, ast.Is, ast.LtE, ast.GtE)):
        return ast.dump(t.left) == ast.dump(t.comparators[0])
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel, is_test):
        self.rel, self.is_test, self.out, self.cls = rel, is_test, [], None

    def visit_ClassDef(self, node):
        prev, self.cls = self.cls, node
        self.generic_visit(node)
        self.cls = prev

    def _function(self, fn):
        decs = _decorator_names(fn)
        template = _class_is_template(self.cls) or decs & {"abstractmethod", "overload", "abstractproperty", "abstractclassmethod", "abstractstaticmethod"}
        dunder = fn.name.startswith("__") and fn.name.endswith("__")
        body = _body_core(fn)
        if self.is_test:
            if fn.name.startswith("test") and not decs & {"skip", "skipif", "xfail", "fixture"}:
                if not _has_assertion(fn):
                    self.out.append(_f("test_no_assertion", "high", self.rel, fn.lineno, f"test '{fn.name}' asserts nothing, so it cannot fail"))
            for n in ast.walk(fn):
                if isinstance(n, ast.Assert) and _trivial_assert(n):
                    self.out.append(_f("trivial_assertion", "high", self.rel, n.lineno, "assertion that is always true (assert True / x == x)"))
            return
        # BaseHTTPRequestHandler.log_message is overridden with an empty body on purpose, to silence per-request logging
        quiet_override = fn.name == "log_message" and self.cls is not None
        if template or dunder or quiet_override or fn.name in HOOK_NAMES:
            return
        only = [s for s in body if not isinstance(s, ast.Pass) and not _is_ellipsis(s)]
        if not body or (not only and body):
            self.out.append(_f("stub_body", "high", self.rel, fn.lineno, f"'{fn.name}' has no implementation (empty body / pass / ...)"))
            return
        if len(body) == 1 and isinstance(body[0], ast.Raise) and body[0].exc is not None and "NotImplementedError" in ast.dump(body[0].exc):
            self.out.append(_f("not_implemented", "medium", self.rel, fn.lineno, f"'{fn.name}' only raises NotImplementedError"))
            return
        if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, (ast.Constant, ast.List, ast.Dict, ast.Tuple, ast.Set)) and not decs:
            params = [a.arg for a in fn.args.args + fn.args.kwonlyargs if a.arg not in ("self", "cls")]
            if params and not (_names_in(body[0]) & set(params)) and not fn.name.startswith("_"):
                self.out.append(_f("constant_stub", "medium", self.rel, fn.lineno, f"'{fn.name}' ignores its parameters and returns a constant"))
                return
        if PRETEND_NAMES.match(fn.name) and body:
            calls = [n for s in body for n in ast.walk(s) if isinstance(n, ast.Call)]
            only_log = all(_call_name(c) in LOG_FUNCS for c in calls)
            ends_ok = isinstance(body[-1], ast.Return) and (body[-1].value is None or isinstance(body[-1].value, (ast.Constant, ast.Dict)))
            simple = all(isinstance(s, (ast.Expr, ast.Return, ast.Assign, ast.Pass)) for s in body)
            if only_log and ends_ok and simple and len(body) <= 6:
                self.out.append(_f("pretends_to_work", "high", self.rel, fn.lineno, f"'{fn.name}' only logs and returns a constant: it looks like it does the work but does not"))

    def visit_FunctionDef(self, node):
        self._function(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node):
        if self.is_test:
            return
        broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"))
        silent = all(isinstance(s, (ast.Pass, ast.Continue)) or _is_ellipsis(s) for s in node.body)
        if broad and silent:
            self.out.append(_f("swallowed_error", "medium", self.rel, node.lineno, "broad except that silently ignores every error"))
        self.generic_visit(node)

    def visit_Import(self, node):
        if not self.is_test:
            for a in node.names:
                if a.name in MOCK_MODULES or a.name.split(".")[0] in ("faker",):
                    self.out.append(_f("mock_in_production", "medium", self.rel, node.lineno, f"mock/fake-data library '{a.name}' imported outside tests"))

    def visit_ImportFrom(self, node):
        if not self.is_test and (node.module or "") in MOCK_MODULES:
            self.out.append(_f("mock_in_production", "medium", self.rel, node.lineno, f"mock/fake-data library '{node.module}' imported outside tests"))


def _placeholders(rel, text, is_test):
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING) and PLACEHOLDER.search(tok.string):
                if "fakecode: allow" in text.splitlines()[tok.start[0] - 1]:
                    continue          # advisory only: the pragma silences a marker, never a stub or an assertion-free test
                if is_test and tok.type == tokenize.STRING:
                    continue
                m = PLACEHOLDER.search(tok.string)
                sev = "medium" if re.search(r"lorem|implement me|changeme|api[_ -]?key", m.group(0), re.I) else "low"  # fakecode: allow (this line defines or reports the marker)
                out.append(_f("placeholder_text", sev, rel, tok.start[0], f"placeholder marker '{m.group(0)}'"))  # fakecode: allow (this line defines or reports the marker)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def scan_text(rel, text):
    is_test = is_test_file(rel)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    v = _Visitor(rel, is_test)
    v.visit(tree)
    return v.out + _placeholders(rel, text, is_test)


def scan(root, files):
    """-> findings for the given project-relative .py files, ordered by severity then place."""
    out = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            with open(os.path.join(root, rel), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        out += scan_text(rel, text)
    return sorted(out, key=lambda x: (-SEV_RANK[x["severity"]], x["file"], x["line"]))


def summarize(findings):
    by = {}
    for x in findings:
        by[x["rule"]] = by.get(x["rule"], 0) + 1
    return {"total": len(findings), "gate": sum(1 for x in findings if x["gate"]), "by_rule": by}


def check(root, files, mode="fail"):
    """-> list of check results in the shape arch_checks.R uses ({"name","ok","detail"}), or [] when mode is 'off' / nothing to look at.
    mode 'fail': gate-level findings fail the check; 'warn': same text, never fails."""
    if mode == "off":
        return []
    py = [f for f in files if f.endswith(".py")]
    if not py:
        return []
    found = scan(root, py)
    gate = [x for x in found if x["gate"]]
    if not gate:
        return [{"name": "no fake code in changed files", "ok": True, "detail": ""}]
    lines = [f"{x['file']}:{x['line']} {x['rule']}: {x['msg']}" for x in gate[:8]]
    more = f" (+{len(gate) - 8} more)" if len(gate) > 8 else ""
    return [{"name": "no fake code in changed files", "ok": mode == "warn",
             "detail": "; ".join(lines) + more + ". Implement it for real, or delete it; do not leave a placeholder."}]  # fakecode: allow (this line defines or reports the marker)


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="include low-severity placeholder markers")  # fakecode: allow (this line defines or reports the marker)
    a = ap.parse_args(argv)
    import arch_core as core
    root = os.path.abspath(a.root)
    files = core.list_files(root, core.load_config(root))
    found = scan(root, files)
    shown = found if a.all else [x for x in found if x["severity"] != "low"]
    if a.json:
        print(json.dumps({"summary": summarize(found), "findings": shown}, indent=1))
    else:
        for x in shown:
            print(f"  {x['severity'].upper():<6} {x['file']}:{x['line']}  {x['rule']}: {x['msg']}")
        s = summarize(found)
        print(f"\n{s['gate']} gate-level finding(s), {s['total']} total across {len([f for f in files if f.endswith('.py')])} Python file(s)." + ("" if a.all else "  (low-severity markers hidden; --all shows them)"))
    return 1 if any(x["gate"] for x in found) else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
