"""Self-tests for template contracts (arch_templates_catalog.contract) and the recipe library (arch_recipes).

Every template's contract is derived from its rendered code, written as CONTRACT.json into the rendered project, and
re-checked against the files on disk (every module exists, every public name is really defined with that signature,
the test count equals the test functions found). Recipes: valid fields, real flows / workflows / agents, fill() against
a rendered template, success "not measured" on an empty history and a rate from synthetic traces.
    python arch_selftest_contracts.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import arch_recipes as RC                      # noqa: E402
import arch_templates as T                     # noqa: E402
import arch_templates_catalog as C             # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def _sig(fn):
    r = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    return f"({ast.unparse(fn.args)}){r}"


def contracts(tmp):
    for tid in T.TEMPLATES:
        dest = os.path.join(tmp, tid)
        C.render(tid, dest, {"name": "app"})
        saved = C.read_contract(dest)
        check(f"{tid}: CONTRACT.json written", saved is not None)
        if saved is None:
            continue
        check(f"{tid}: file contract equals the catalog entry", saved == C.entry(tid)["contract"])
        bad = []
        for m in saved["modules"]:
            p = os.path.join(dest, *m["path"].split("/"))
            if not os.path.isfile(p):
                bad.append("missing " + m["path"])
                continue
            tree = ast.parse(open(p, encoding="utf-8").read())
            defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            for pub in m["public"]:
                n = defs.get(pub["name"])
                if n is None:
                    bad.append(f"{m['path']}:{pub['name']} not defined")
                elif pub["kind"] == "function" and pub["signature"] != _sig(n):
                    bad.append(f"{m['path']}:{pub['name']} signature differs")
        check(f"{tid}: modules and signatures match the code", not bad, "; ".join(bad[:3]))
        n_tests = 0
        for rel in saved["tests"]["files"]:
            n_tests += sum(1 for n in ast.walk(ast.parse(open(os.path.join(dest, *rel.split("/")), encoding="utf-8").read()))
                           if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))
        check(f"{tid}: test count matches", n_tests == saved["tests"]["count"], f"{n_tests} vs {saved['tests']['count']}")
        check(f"{tid}: JSON-able", json.loads(json.dumps(saved)) == saved)
    ic = C.as_interface_contract(C.contract("python_lib"))
    check("as_interface_contract has the flow-contract keys", set(ic) == {"imports", "calls", "mains", "methods"} and ic["imports"])
    check("http routes derived (rest_api)", {h["path"] for h in C.contract("rest_api")["http"]} >= {"/health", "/items"})
    check("cli commands derived (cli_tool)", {"add", "list"} <= set(C.contract("cli_tool")["cli"]["commands"]))
    check("a different name changes the package", C.contract("python_lib", "My Lib")["package"] == T.pkg_name("My Lib"))


def recipes(tmp):
    rs = RC.list(roots=[tmp])
    check("at least 10 recipes", len(rs) >= 10, str(len(rs)))
    import arch_flows
    for r in rs:
        rid = r["id"]
        check(f"{rid}: fields", all(k in r for k in ("title", "summary", "goal", "flow", "agents", "requires", "params")))
        check(f"{rid}: flow exists", r["flow"] in arch_flows.FLOWS, r["flow"])
        if r.get("workflow"):
            check(f"{rid}: workflow exists", os.path.isfile(os.path.join(HERE, "workflows", r["workflow"] + ".yaml")))
        for a in r["agents"]:
            check(f"{rid}: agent {a} exists", any(os.path.isfile(os.path.join(HERE, "agents", a + e)) for e in (".md", ".yaml")))
        names = {p["name"] for p in r["params"]} | {"test_dir", "cli_module"}
        import string
        used = {f for _, f, _, _ in string.Formatter().parse(r["goal"]) if f}
        check(f"{rid}: every placeholder is a param", used <= names, str(used - names))
        check(f"{rid}: success is labelled", r["success"]["label"])
    empty = os.path.join(tmp, "empty_history")
    os.makedirs(empty, exist_ok=True)
    check("no history -> not measured", RC.success("add_caching", roots=[empty]) == {"measured": False, "n": 0, "label": "not measured"})
    hist = os.path.join(tmp, "hist", ".arch", "runs", "r1")
    os.makedirs(hist)
    with open(os.path.join(hist, "trace.jsonl"), "w") as f:
        for v in ("pass", "pass", "fail", "not_sure"):
            f.write(json.dumps({"type": "verdict", "flow": "feature", "recipe": "add_caching", "verdict": v}) + "\n")
        f.write(json.dumps({"type": "verdict", "flow": "bugfix", "verdict": "pass"}) + "\n")
    s = RC.success("add_caching", roots=[os.path.join(tmp, "hist")])
    check("rate from traces", s["measured"] and s["pass"] == 2 and s["n"] == 4 and s["rate"] == 0.5 and s["source"] == "recipe", str(s))
    check("too few runs -> not measured", not RC.success("fix_bug_with_repro", roots=[os.path.join(tmp, "hist")])["measured"])
    proj = os.path.join(tmp, "p_rest")
    C.render("rest_api", proj, {"name": "app"})
    f = RC.fill("add_http_endpoint", proj, path="/ping", behaviour="return pong")
    check("fill uses the contract", f["ready"] and "/ping" in f["goal"] and "tests" in f["goal"] and f["contract_source"] == "CONTRACT.json", str(f))
    check("fill names the flow and agents", f["flow"] == "feature" and f["agents"])
    p2 = os.path.join(tmp, "p_lib")
    C.render("python_lib", p2, {"name": "app"})
    f2 = RC.fill("add_http_endpoint", p2)
    check("a project without routes is not ready", not f2["ready"] and f2["missing"] == ["http"], str(f2["missing"]))
    os.remove(os.path.join(p2, "CONTRACT.json"))
    f3 = RC.fill("add_caching", p2)
    check("no CONTRACT.json -> derived from the code", f3["contract_source"].startswith("derived") and "{function}" not in f3["goal"], f3["goal"][:80])


def run():
    tmp = tempfile.mkdtemp(prefix="arch_contracts_")
    try:
        contracts(tmp)
        recipes(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"contracts+recipes: {PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
