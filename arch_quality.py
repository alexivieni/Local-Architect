"""Code-based quality tools the flows use as VERIFIERS (so a model's claim is never the only evidence):

  mutation_check   change the code in small ways (flip a comparison, swap + and -); good tests must fail.
                   Survivors are behaviours no test protects. Runs on a throw-away copy of the project.
  static_scan      Python bug patterns found by reading the syntax tree (no model, no guessing).
  run_bench        run a benchmark script several times; median of the seconds it reports.
  sql_plans        EXPLAIN QUERY PLAN for named queries against the project's migrations/schema; flags full scans.
  snapshot         throw-away copy of a project (for mutation, A/B benchmarks).
"""
import ast
import json
import os
import re
import shutil
import sqlite3
import statistics
import tempfile

import arch_core as core

SKIP_DIRS = {".git", ".arch", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", "dist", "build", ".mypy_cache"}


def snapshot(root):
    dst = tempfile.mkdtemp(prefix="arch_snap_")
    out = os.path.join(dst, "p")
    shutil.copytree(root, out, ignore=lambda d, names: [n for n in names if n in SKIP_DIRS or n.endswith((".db", ".sqlite", ".pyc"))])
    return out


def drop(path):
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


# ------------------------------------------------------------------------------------------------ mutation
CMP_FLIP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
BIN_FLIP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult, ast.Div: ast.Mult, ast.Mod: ast.Mult}
SYM = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.GtE: ">=", ast.Gt: ">", ast.LtE: "<=", ast.In: "in", ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not",
       ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//", ast.Div: "/", ast.Mod: "%", ast.And: "and", ast.Or: "or"}


class _Mut(ast.NodeTransformer):
    """Applies exactly the target-th mutation (counting in tree order); records what it did."""

    def __init__(self, target):
        self.target, self.n, self.desc = target, -1, None

    def _hit(self, node, text):
        self.n += 1
        if self.n == self.target:
            self.desc = f"line {node.lineno}: {text}"
            return True
        return False

    def visit_Compare(self, node):
        self.generic_visit(node)
        op = type(node.ops[0])
        if op in CMP_FLIP and self._hit(node, f"{SYM[op]} -> {SYM[CMP_FLIP[op]]}"):
            node.ops[0] = CMP_FLIP[op]()
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        op = type(node.op)
        if op in BIN_FLIP and self._hit(node, f"{SYM[op]} -> {SYM[BIN_FLIP[op]]}"):
            node.op = BIN_FLIP[op]()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        flip = ast.Or if isinstance(node.op, ast.And) else ast.And
        if self._hit(node, f"{SYM[type(node.op)]} -> {SYM[flip]}"):
            node.op = flip()
        return node

    def visit_Return(self, node):
        self.generic_visit(node)
        if node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            if self._hit(node, "return value -> None"):
                node.value = ast.Constant(value=None)
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, int) and not isinstance(node.value, bool) and abs(node.value) < 10 ** 6:
            if self._hit(node, f"{node.value} -> {node.value + 1}"):
                return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node


def _count_mutations(tree):
    m = _Mut(-1)
    m.visit(ast.parse(ast.unparse(tree)))
    return m.n + 1


def mutation_check(root, cfg, files, max_mutants=8, seed=1, log=None):
    """-> {"ok": bool, "error": str?, "total", "killed", "survived": [{"file","where"}], "score": 0-100}.
    Python files only. The real project is never touched."""
    py = [f for f in files if f.endswith(".py") and not f.startswith("tests/") and "/tests/" not in f and not os.path.basename(f).startswith("test_") and os.path.basename(f) != "conftest.py"]
    sources, cand = {}, []
    for f in py:
        try:
            tree = ast.parse(core.read_text(os.path.join(root, f))[0])
        except SyntaxError:
            continue
        sources[f] = tree
        cand += [(f, i) for i in range(_count_mutations(tree))]
    if not cand:
        return {"ok": False, "error": "no mutable Python code found in these files", "total": 0, "killed": 0, "survived": [], "score": None}
    import random
    rnd = random.Random(seed)
    pick = sorted(rnd.sample(cand, min(max_mutants, len(cand))))
    snap = snapshot(root)
    try:
        base, blog = core.run_tests(snap, cfg)
        if base != "pass":
            return {"ok": False, "error": "tests do not pass on the unmodified code (" + base + "): " + blog[-300:], "total": 0, "killed": 0, "survived": [], "score": None}
        killed, survived = 0, []
        quick = core.deep_merge(cfg, {"limits": {"test_timeout": max(30, min(120, cfg["limits"]["test_timeout"] // 3))}})
        for f, i in pick:
            tree = ast.parse(ast.unparse(sources[f]))
            m = _Mut(i)
            new = m.visit(tree)
            ast.fix_missing_locations(new)
            path = os.path.join(snap, f)
            original = open(path, encoding="utf-8").read()
            try:
                open(path, "w", encoding="utf-8").write(ast.unparse(new))
                st, _ = core.run_tests(snap, quick)
            finally:
                open(path, "w", encoding="utf-8").write(original)
            if st == "pass":
                survived.append({"file": f, "where": m.desc})
                if log:
                    log(f"    survived: {f} {m.desc}")
            else:
                killed += 1
        total = len(pick)
        return {"ok": True, "total": total, "killed": killed, "survived": survived, "score": round(100 * killed / total)}
    finally:
        drop(snap)


# ------------------------------------------------------------------------------------------------ static bug patterns
BUILTINS = {"list", "dict", "set", "str", "int", "float", "id", "type", "input", "max", "min", "sum", "len", "map", "filter", "format", "object", "all", "any"}


def _is_sql_exec(call):
    return isinstance(call.func, ast.Attribute) and call.func.attr in ("execute", "executemany", "executescript") and call.args


def static_scan(root, files):
    """-> [{"file","line","rule","msg","severity"}]. Reads syntax trees only."""
    out = []

    def add(f, node, rule, msg, sev="medium"):
        out.append({"file": f, "line": getattr(node, "lineno", 0), "rule": rule, "msg": msg, "severity": sev})

    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            tree = ast.parse(core.read_text(os.path.join(root, f))[0])
        except SyntaxError as e:
            out.append({"file": f, "line": e.lineno or 0, "rule": "syntax", "msg": str(e.msg), "severity": "high"})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    add(f, node, "bare-except", "bare 'except:' also catches KeyboardInterrupt/SystemExit and hides bugs")
                elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    add(f, node, "swallowed-exception", "exception caught and silently ignored")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.args.defaults + [x for x in node.args.kw_defaults if x]:
                    if isinstance(d, (ast.List, ast.Dict, ast.Set)) or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id in ("list", "dict", "set")):
                        add(f, d, "mutable-default", f"mutable default argument in {node.name}(): shared between calls", "high")
                body = node.body
                for a, b in zip(body, body[1:]):
                    if isinstance(a, (ast.Return, ast.Raise)):
                        add(f, b, "unreachable", f"code after {type(a).__name__.lower()} in {node.name}() never runs")
                        break
            elif isinstance(node, ast.Compare):
                for op, c in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(c, ast.Constant) and c.value is None:
                        add(f, node, "none-compare", "compare with None using 'is' / 'is not'", "low")
                    if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(c, ast.Constant) and isinstance(c.value, (str, int, float)) and not isinstance(c.value, bool):
                        add(f, node, "is-literal", "'is' with a literal compares identity, not value", "high")
            elif isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple) and node.test.elts:
                add(f, node, "assert-tuple", "assert on a non-empty tuple is always true", "high")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in BUILTINS:
                        add(f, node, "shadows-builtin", f"'{t.id}' shadows a Python builtin", "low")
            elif isinstance(node, ast.Call):
                if _is_sql_exec(node):
                    a = node.args[0]
                    if isinstance(a, ast.JoinedStr) or (isinstance(a, ast.BinOp) and isinstance(a.op, (ast.Add, ast.Mod))) or (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == "format"):
                        add(f, node, "sql-injection", "SQL built from strings: use ? or :name parameters", "high")
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "call", "check_output", "Popen", "check_call") and any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
                    add(f, node, "shell-true", "subprocess with shell=True: command injection risk if any part is user input", "high")
                if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                    add(f, node, "eval-exec", f"{node.func.id}() on dynamic input is code injection", "high")
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("urlopen", "get", "post", "put", "request") and not any(k.arg == "timeout" for k in node.keywords):
                    base = ast.unparse(node.func.value) if hasattr(ast, "unparse") else ""
                    if base in ("requests", "urllib.request", "httpx", "session") or node.func.attr == "urlopen":
                        add(f, node, "no-timeout", "network call without a timeout can hang forever", "medium")
                if isinstance(node.func, ast.Attribute) and node.func.attr == "loads" and isinstance(node.func.value, ast.Name) and node.func.value.id == "pickle":
                    add(f, node, "pickle-loads", "pickle on untrusted data runs arbitrary code", "high")
            elif isinstance(node, ast.For):
                it = node.iter
                if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range" and it.args and isinstance(it.args[-1], ast.BinOp) and isinstance(it.args[-1].op, ast.Sub):
                    r = it.args[-1]
                    if isinstance(r.left, ast.Call) and isinstance(r.left.func, ast.Name) and r.left.func.id == "len" and isinstance(r.right, ast.Constant) and r.right.value == 1:
                        add(f, node, "off-by-one?", "range(len(x) - 1) skips the last element; intended?", "low")
    return out


# ------------------------------------------------------------------------------------------------ benchmarks
def run_bench(root, cfg, rel_script, runs=5):
    """The script prints its measurement as the LAST line: a number, or JSON {"seconds": n}. -> (median seconds | None, detail)."""
    vals, last = [], ""
    for _ in range(max(1, runs)):
        rc, out = core.run_cmd([os.sys.executable, rel_script], root, cfg["limits"]["test_timeout"], cfg["behavior"]["offline_tests"])
        last = out.strip().splitlines()[-1] if out.strip() else ""
        if rc != 0:
            return None, f"the benchmark failed (exit {rc}): {out[-300:]}"
        try:
            v = json.loads(last)
            v = v.get("seconds") if isinstance(v, dict) else v
            vals.append(float(v))
        except (ValueError, TypeError):
            return None, f"the last line must be a number of seconds or JSON {{\"seconds\": n}}; got: {last[:80]}"
    return statistics.median(vals), f"{len(vals)} runs, min {min(vals):.4f}s max {max(vals):.4f}s"


# ------------------------------------------------------------------------------------------------ SQL plans
def sql_plans(root, files):
    """Apply the project's migrations (or schema), then EXPLAIN QUERY PLAN each named query. -> [{"query","plan","scan"}]."""
    sql = [f for f in files if f.endswith(".sql")]
    con = sqlite3.connect(":memory:")
    migs = sorted(f for f in sql if "/migrations/" in "/" + f)
    try:
        if migs:
            d = os.path.dirname(os.path.join(root, migs[0]))
            for n in sorted(x for x in os.listdir(d) if x.endswith(".sql")):
                con.executescript(open(os.path.join(d, n), encoding="utf-8", errors="replace").read())
        else:
            for f in sql:
                t = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
                if re.search(r"create\s+table", t, re.I):
                    con.executescript(t)
    except sqlite3.Error:
        return []
    out = []
    for f in sql:
        if f in migs:
            continue
        t = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
        parts = re.split(r"(?m)^--\s*name:\s*(\w+)\s*$", t)
        for name, body in zip(parts[1::2], parts[2::2]):
            for stmt in [x.strip() for x in body.split(";") if x.strip() and not x.strip().startswith("--")]:
                try:
                    named = set(re.findall(r"[:@$](\w+)", stmt))
                    rows = con.execute("EXPLAIN QUERY PLAN " + stmt, {n: None for n in named} if named else tuple([None] * stmt.count("?"))).fetchall()
                except sqlite3.Error as e:
                    out.append({"query": name, "plan": "error: " + str(e)[:80], "scan": True})
                    continue
                plan = "; ".join(r[3] for r in rows)
                out.append({"query": name, "plan": plan, "scan": bool(re.search(r"\bSCAN\b(?! .*USING (?:COVERING )?INDEX)", plan))})
    return out
