"""Self-test for arch_augment (sharpen a request before it runs).
A real Engine whose model call is scripted: the real ask_json parses and schema-checks every reply. No model, no network, no Claude.
Run: python arch_selftest_augment.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CALC = '''def paginate(items, page, size):
    """Rows for one page (1-based)."""
    start = page * size
    return items[start:start + size]


class Ledger:
    def total(self):
        return 0
'''
TEST_DRAFT = '''from calc import paginate


def test_first_page():
    assert paginate(list(range(10)), 1, 3) == [0, 1, 2]
'''


def _project(tmp, files):
    root = os.path.join(tmp, "proj%d" % len(os.listdir(tmp)))
    os.makedirs(root)
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return root


def _snapshot(root):
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = os.path.getsize(p)
    return out


def _engine(root, script, log):
    """A real Engine; only `call` (the model) and the index are scripted. script: role -> reply text, or an exception to raise."""
    import arch_core as core
    e = core.Engine(root, "sharpen", "test", review="dry", say=lambda m: None, persist=False)

    def call(role, user, extra_system="", json_out=False, think=None, temperature=None, num_predict=None, label=None, schema=None):
        log.append({"role": role, "user": user, "schema": schema, "model": (e.cfg.get("roles") or {}).get(role)})
        r = script.get(role, "")
        if isinstance(r, BaseException):
            raise r
        return {"text": r, "model": "fake:" + role, "id": len(log)}

    def no_index():
        raise RuntimeError("no index in this test")
    e.call = call
    e.retriever = no_index
    e.context = lambda q, budget=None, always=(): "=== PROJECT\ncalc.py"
    return e


GOOD = {"goal": "Make paginate(items, page, size) in calc.py treat page as 1-based so page 1 returns the first `size` rows.",
        "flow": "bugfix",
        "target_files": [{"path": "calc.py", "why": "paginate computes the start index"},
                         {"path": "tests/test_calc.py", "why": "reproduction", "new": True},
                         {"path": "helpers/ghost.py", "why": "invented"},
                         {"path": "../outside.py", "why": "escapes the project"}],
        "functions": ["paginate", "Ledger.total", "make_coffee"],
        "acceptance": ["paginate(list(range(10)), 1, 3) == [0, 1, 2]", "paginate(list(range(10)), 2, 3) == [3, 4, 5]"],
        "test_path": "tests/test_calc.py", "test_draft": TEST_DRAFT,
        "risks": ["callers that already pass 0-based pages"], "ambiguous": False, "questions": ["Should page 0 be an error?"]}


def run():
    results = []

    def check(name, cond, detail=None):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or detail is None else f"  -> {str(detail)[:400]}"))

    tmp = tempfile.mkdtemp(prefix="arch_augment_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ["ARCHITECT_SELFTEST"] = "1"
    projs = os.path.join(tmp, "projects")
    os.makedirs(projs)
    try:
        import arch_augment as aug
        import arch_core as core
        import arch_flows
        import arch_schemas

        # ---- the schema
        sch = aug.schema(arch_flows.FLOWS)
        check("a complete reply fits the schema", arch_schemas.valid(GOOD, sch), arch_schemas.errors(GOOD, sch))
        check("an unknown flow breaks the schema", not arch_schemas.valid(dict(GOOD, flow="teleport"), sch))
        check("a reply without acceptance breaks the schema", not arch_schemas.valid({k: v for k, v in GOOD.items() if k != "acceptance"}, sch))

        # ---- a good local reply, grounded
        root = _project(projs, {"calc.py": CALC})
        before = _snapshot(root)
        log = []
        e = _engine(root, {"router": json.dumps(GOOD)}, log)
        res = aug.sharpen(root, "fix paginate, page 1 gives the wrong rows", engine=e)
        by = {f["path"]: f for f in res["target_files"]}
        check("the router tier was asked first, with the schema", log and log[0]["role"] == "router" and log[0]["schema"] is not None, log[:1])
        check("one call is enough when the reply is good", len(log) == 1, len(log))
        check("source is local", res["source"] == "local", res["source"])
        check("the sharpened goal is the model's", res["goal"].startswith("Make paginate"), res["goal"])
        check("the flow is kept when it is registered", res["flow"] == "bugfix" and "model" in res["flow_why"], res["flow"])
        check("an existing file is marked exists", by.get("calc.py", {}).get("status") == "exists", by)
        check("a file marked new is accepted as new", by.get("tests/test_calc.py", {}).get("status") == "new", by)
        check("an invented file is flagged unknown", by.get("helpers/ghost.py", {}).get("status") == "unknown"
              and any("helpers/ghost.py" in f for f in res["flags"]), res["flags"])
        check("a file outside the project is dropped and flagged", "../outside.py" not in by
              and any("outside the project" in f for f in res["flags"]), res["flags"])
        fn = {f["name"]: f for f in res["functions"]}
        check("a real function is found with its place", fn.get("paginate", {}).get("found") and fn["paginate"]["where"].startswith("calc.py:"), fn)
        check("a method is found by its own name", fn.get("Ledger.total", {}).get("found"), fn)
        check("an invented function is flagged", fn.get("make_coffee", {}).get("found") is False
              and any("make_coffee" in f for f in res["flags"]), fn)
        acc = res["acceptance"]
        check("acceptance statements are kept", len(acc["statements"]) == 2, acc)
        check("the draft test is kept and parses", acc["test_parses"] is True and acc["test_path"] == "tests/test_calc.py", acc)
        check("questions are dropped when the model says it is not ambiguous", res["questions"] == [], res["questions"])
        check("local estimate costs no money", res["estimate"]["usd"] == 0 and res["estimate"]["calls"] > 0
              and "estimate" in res["estimate"]["basis"], res["estimate"])
        u = log[0]["user"]
        check("the prompt is prefix-stable (instructions first, the request last)",
              u.startswith(aug.PROMPT) and u.rstrip().endswith("REQUEST: fix paginate, page 1 gives the wrong rows"), u[-200:])
        check("the code hints went into the prompt", "def paginate" in u, u[:600])
        check("nothing in the project was written", _snapshot(root) == before, _snapshot(root))
        s = aug.summary(res)
        check("summary is plain words with the flags", all(x in s for x in ("Sharpened goal:", "Workflow: bugfix", "NOT FOUND",
                                                                             "Done when:", "Estimate:", "[new]")), s)

        # ---- fallbacks
        log = []
        e = _engine(root, {"router": '{"goal": "x"}', "worker": json.dumps(GOOD)}, log)
        res = aug.sharpen(root, "fix paginate", engine=e)
        check("a reply that breaks the schema goes to the worker", [c["role"] for c in log] == ["router", "worker"], [c["role"] for c in log])
        check("the worker's reply is used", res["source"] == "local" and res["goal"].startswith("Make paginate"), res["source"])
        check("the broken reply is noted", any("router" in n and "shape" in n for n in res["notes"]), res["notes"])

        log = []
        e = _engine(root, {"router": "not json at all", "worker": core.LLMError("server down")}, log)
        res = aug.sharpen(root, "fix paginate, page 1 gives the wrong rows", engine=e)
        check("no usable model -> code-only result", res["source"] == "code", res["source"])
        check("code-only flow comes from the keyword rule", res["flow"] == "bugfix" and "no model answered" in res["flow_why"], res)
        check("code-only files come from code hints and exist", [f["path"] for f in res["target_files"]] == ["calc.py"]
              and res["target_files"][0]["status"] == "exists", res["target_files"])
        check("code-only invents no acceptance", res["acceptance"]["statements"] == [] and not res["acceptance"]["test_draft"], res["acceptance"])
        check("the model error is said in words", any("server down" in n for n in res["notes"]), res["notes"])
        check("summary says no model answered", "code only" in aug.summary(res), aug.summary(res))

        log = []
        e = _engine(root, {"router": core.BudgetExceeded("calls budget")}, log)
        res = aug.sharpen(root, "fix paginate", engine=e)
        check("a spent budget stops asking more models", len(log) == 1 and res["source"] == "code", [c["role"] for c in log])

        # ---- Claude only when allowed (never inside the self-tests)
        log = []
        e = _engine(root, {"router": json.dumps(GOOD)}, log)
        roles_before = json.dumps(e.cfg.get("roles"), sort_keys=True)
        res = aug.sharpen(root, "fix paginate", use="claude", engine=e)
        check("use=claude in the self-tests stays local and says why", res["source"] == "local"
              and any("Claude was not used" in n for n in res["notes"]), res["notes"])
        check("no call was routed to a claude: model", all(not str(c["model"] or "").startswith("claude:") for c in log), log)
        check("the role settings are unchanged", json.dumps(e.cfg.get("roles"), sort_keys=True) == roles_before)
        est = aug.estimate("bugfix", "claude", "claude:claude-sonnet-5-5")
        check("a Claude estimate is priced from the price card", est["usd"] > 0 and "price card" in est["basis"], est)

        # ---- grounding details
        g = aug.ground(root, dict(GOOD, flow="teleport", ambiguous=True, questions=["a?", "b?", "c?", "d?"]), {}, list(arch_flows.FLOWS), "x")
        check("an unknown flow is not trusted", g["flow"] is None)
        check("questions kept only when ambiguous, at most 3", g["questions"] == ["a?", "b?", "c?"], g["questions"])
        g = aug.ground(root, dict(GOOD, test_draft="def test_x(:\n  pass", test_path="/etc/x.py"), {}, list(arch_flows.FLOWS), "x")
        check("a draft that does not parse is flagged", g["acceptance"]["test_parses"] is False
              and any("does not parse" in f for f in g["flags"]), g["flags"])
        check("a test path outside the project is replaced", g["acceptance"]["test_path"].startswith("tests/test_"), g["acceptance"]["test_path"])
        g = aug.ground(root, {"goal": "", "target_files": []}, {}, list(arch_flows.FLOWS), "keep me")
        check("an empty goal keeps the person's own words", g["goal"] == "keep me")

        # ---- empty project
        empty = _project(projs, {})
        log = []
        e = _engine(empty, {"router": json.dumps(dict(GOOD, target_files=[{"path": "app.py", "why": "start", "new": True}]))}, log)
        res = aug.sharpen(empty, "fix the app", engine=e)
        check("an empty project runs new, not bugfix", res["flow"] == "new", res["flow"])
        check("an empty project gets no project context", "the project folder is empty" in log[0]["user"])
        kf, _ = aug.keyword_flow("add a login page", empty=True)
        check("keyword rule: build in an empty folder means new", kf == "new", kf)
        kf, _ = aug.keyword_flow("speed up the report query, it is slow")
        check("keyword rule: optimize words pick optimize", kf == "optimize", kf)
        check("summary of nothing is safe", aug.summary(None) == "Nothing to show.")
    except Exception as ex:
        import traceback
        traceback.print_exc()
        check("suite ran without an exception", False, ex)
    finally:
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' AUGMENT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' AUGMENT CHECKS FAILED'}\n")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
