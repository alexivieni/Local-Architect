"""More flows (registered into arch_flows.FLOWS):

  new       start from NOTHING: pick a template (no model needed), write it, prove its tests pass, then build the goal on top.
  feature   one feature across data, API and UI, contract first, with the full-stack role and an integration test.
  test      strengthen the tests: measure with code mutations, write tests for what survives, measure again.
  optimize  measure -> change -> re-measure. A change is kept only if tests stay green AND the benchmark improves.
  hunt      look for real bugs; each suspicion must be PROVEN by a failing test or it is thrown away.
  review    static checks + metrics + a reviewer's notes, written to docs/review.md.

Same principle as the first four flows: code decides pass or fail wherever it can; models only propose.
"""
import json
import os
import sys
import re
import time

import arch_checks as ck
import arch_core as core
import arch_flows as base
import arch_quality as q
import arch_schemas
import arch_templates as T
from arch_flows import Flow, S, verify, log_checks, write_handoff, handoff_text, gen_role_for
from arch_core import LLMError


# ------------------------------------------------------------------------------------------------ helpers
def capture(e, limit=400):
    """{rel: text} for every source file (cheap safety net so one failed attempt can be rolled back exactly)."""
    out = {}
    for f in core.list_files(e.root, e.cfg)[:limit]:
        try:
            out[f] = open(os.path.join(e.root, f), "rb").read()
        except OSError:
            pass
    return out


def restore(e, cap):
    """Put every file back to the captured bytes and delete files that did not exist then. -> list of paths touched."""
    touched = []
    for f in core.list_files(e.root, e.cfg):
        if f not in cap:
            try:
                os.remove(os.path.join(e.root, f))
                touched.append(f)
            except OSError:
                pass
    for f, data in cap.items():
        p = os.path.join(e.root, f)
        try:
            cur = open(p, "rb").read() if os.path.isfile(p) else None
        except OSError:
            cur = None
        if cur != data:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(data)
            touched.append(f)
    return touched


def explicit_files(e, goal, exts=(".py",)):
    pat = r"[\w./\\-]+\.(?:" + "|".join(x.lstrip(".") for x in exts) + r")\b"
    return [m.replace("\\", "/") for m in re.findall(pat, goal) if os.path.isfile(os.path.join(e.root, m))]


def scope_files(e, goal, n=4, exts=(".py",)):
    exp = explicit_files(e, goal, exts)
    if exp:
        return exp[:n]
    ranked = [f for f, s in core.rank_files(e.root, e.cfg, goal) if f.endswith(tuple(exts)) and not f.startswith(("tests/", "bench/")) and "/tests/" not in f]
    if ranked:
        return ranked[:n]
    return [f for f in core.list_files(e.root, e.cfg) if f.endswith(tuple(exts)) and not f.startswith(("tests/", "bench/"))][:n]


def can_test(e):
    return core.has_module("pytest") or bool(e.cfg["tests"].get("command"))


def slug(text, n=24):
    return re.sub(r"[^a-z0-9]+", "_", text.lower())[:n].strip("_") or "x"


# ------------------------------------------------------------------------------------------------ NEW
class NewFlow(Flow):
    id, title = "new", "Start a new project"
    blurb = "From an empty folder: choose a template, write a working tested base (no model needed), then build your goal on top of it."
    steps = [S("pick", "Choose a template", "router", "llm"), S("write", "Write the template (one approval)", "-", "code"),
             S("verify", "Run its tests + validators", "-", "code"), S("build", "Build your goal on top (sprints)", "planner", "llm"),
             S("handoff", "Save progress for the next session", "-", "code")]
    produces = ["a working project from the template", "tests/", ".arch/handoff.json"]
    uses_roles = ["router", "planner", "generator", "generator_ui", "generator_sql", "evaluator"]

    def run(self, e, params):
        goal = e.goal
        name = params.get("name") or os.path.basename(e.root.rstrip("/\\")) or "app"
        tid, why = params.get("template"), "chosen by you"
        if not tid or tid == "auto":
            tid, why = T.suggest(goal)
            if tid == "empty" and len(goal.split()) >= 3:
                try:
                    r, j = e.ask_json("router", "Pick the best starting template for this project. Templates: " + "; ".join(f"{t['id']} = {t['blurb']}" for t in T.TEMPLATES.values())
                                      + '\nAnswer ONLY JSON {"template": "<id>", "why": "short"}.\n\nPROJECT: ' + goal, schema=arch_schemas.template_pick(T.TEMPLATES))
                    if isinstance(j, dict) and j.get("template") in T.TEMPLATES:
                        tid, why = j["template"], "router model: " + str(j.get("why", ""))[:80]
                except LLMError:
                    pass                                    # no model yet: the template is still created below
        addons = [a for a in (params.get("addons") or []) if a in T.ADDONS]
        files = T.render(tid, name, addons)
        todo = [p for p in sorted(files) if not os.path.exists(os.path.join(e.root, p))]
        kept = [p for p in sorted(files) if p not in todo]
        tpl = T.TEMPLATES[tid]
        e.say(f"[template] {tpl['title']}  ({why})")
        e.say(f"          {len(todo)} file(s) to create" + (f", {len(kept)} already exist and are kept" if kept else ""))
        for p in todo[:14]:
            e.say("          + " + p)
        e.report(f"- Template: `{tid}` ({why}); addons: {', '.join(addons) or 'none'}", f"- Files to create: {len(todo)}; kept as they were: {len(kept)}", "")
        if e.review == "dry":
            return {"ok": True, "dry": True, "files": todo}
        if todo and e.review == "ask":
            a = (e.ask(f"  Create these {len(todo)} files in {e.root}?  [y]es  [n]o > ") or "").strip().lower()
            if a not in ("y", "yes", "a", "all"):
                e.report("- Stopped: not approved.")
                return {"ok": False, "stopped": True}
        os.makedirs(e.root, exist_ok=True)
        written = [p for p in todo if e.put_file(p, files[p], "template file", quiet=True)["status"] == "applied"]
        e.say(f"[+] wrote {len(written)} file(s)")
        v = verify(e, [w for w in written if not w.startswith("tests/")], run_tests=True)
        log_checks(e, "template checks", v)
        if tpl["needs"] and not all(core.has_module(m) for m in tpl["needs"]):
            e.say(f"[i] this template needs: pip install -r requirements.txt ({', '.join(tpl['needs'])}); its tests are skipped until then")
        write_handoff(e, goal=goal, flow="new", template=tid, domains=tpl["domains"], files=written, run=T.rendered_run(tid, name),
                      next="build the goal on top of the template" if goal else "describe what to build")
        ok = v["ok"]
        then = params.get("then_build")
        if then is None:
            then = len(goal.split()) >= 5
        if then and ok and e.review != "dry":
            e.say("\n[build] building your goal on top of the template ...")
            e.report("", "## Build on top of the template")
            try:
                out = base.BuildFlow().run(e, {"domains": params.get("domains") or tpl["domains"]})
                ok = ok and out.get("ok", False)
            except LLMError as ex:
                e.say(f"[!] the template is ready, but the build step needs a model: {ex}")
                e.report(f"- Build step skipped: {ex}")
        e.report("", f"- Project {'ready' if ok else 'created, with failing checks'}. Run it with: `{tpl['run']}`")
        return {"ok": ok, "files": written}


# ------------------------------------------------------------------------------------------------ FEATURE
FEATURE_PLAN = """Plan ONE feature across all layers as small SPRINTS, contract first. Answer ONLY JSON:
{{"sprints": [{{"name": "...", "kind": "mixed", "goal": "one sentence", "files": ["paths you expect to create or change"],
  "acceptance": [{{"id": "A1", "text": "observable, checkable criterion"}}]}}]}}
Order: (1) the contract: data shape/schema and the API request/response/error shapes; (2) data layer + API; (3) UI that uses exactly that contract (empty, loading, error and success states); (4) an end-to-end check through the public interface.
Rules: at most {max_sprints} sprints; each leaves the project working; every sprint has 2-5 acceptance criteria that a test or script can check; backend code gets tests under tests/.

FEATURE: {goal}

{context}"""

E2E = """Write an END-TO-END test for this feature through the project's PUBLIC interface (start the server on a free port in a fixture and use HTTP, or call the CLI), not internal functions. Cover the main success path, one validation failure and one not-found case. Create tests/test_e2e_{slug}.py (a new file; do not edit existing tests). Use only the standard library plus pytest.

FEATURE: {goal}

PROJECT CONTEXT:
{context}

{feedback}"""


class FeatureFlow(base.BuildFlow):
    id, title = "feature", "Full-stack feature"
    blurb = "Contract first, then data + API, then UI, then an end-to-end test: one feature across every layer, built by the full-stack role."
    plan_prompt = FEATURE_PLAN
    all_role = "fullstack"
    steps = [S("plan", "Plan layered sprints (contract first)", "planner", "llm"), S("code", "Implement each layer", "fullstack", "llm"),
             S("checks", "Syntax, HTML, SQL, OpenAPI checks + tests (+ browser)", "-", "code"), S("fix", "Fix loop on failure", "fullstack", "llm"),
             S("e2e", "End-to-end test through the public interface", "tester", "llm"), S("judge", "Judge against the contract", "evaluator", "judge")]
    produces = ["data, API and UI files", "tests/", "tests/test_e2e_*.py", ".arch/contract.json"]
    uses_roles = ["planner", "fullstack", "tester", "evaluator"]

    def run(self, e, params):
        out = base.BuildFlow.run(self, e, params)
        if not out.get("ok") or e.review == "dry" or not can_test(e):
            return out
        e.say("\n=== End-to-end test ===")
        feedback, existing = "", set(core.list_files(e.root, e.cfg))
        e.deny_prefixes = [f for f in existing if f.startswith("tests/")]
        okk = False
        try:
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                r = e.call("tester", E2E.format(goal=e.goal, slug=slug(e.goal), feedback=feedback, context=e.context(e.goal, None)), label=f"e2e-try{attempt + 1}")
                res = e.apply(r["text"], f"e2e test try {attempt + 1}")
                if any(x["status"] == "quit" for x in res):
                    return {"ok": False, "stopped": True}
                new = [x["rel"] for x in res if x["status"] == "applied"]
                if not new:
                    feedback = "FEEDBACK: no new test file was written:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res if x.get("msg")) + "\n\n"
                    e.mark(r["id"], False, feedback)
                    continue
                st, log = core.run_tests(e.root, e.cfg)
                e.mark(r["id"], st == "pass", log[-300:])
                if st == "pass":
                    okk = True
                    e.say("[+] end-to-end test passes")
                    break
                for f in new:                                    # a red end-to-end test must not stay behind
                    if f not in existing:
                        try:
                            os.remove(os.path.join(e.root, f))
                        except OSError:
                            pass
                feedback = "FEEDBACK: the end-to-end test (or the feature) fails:\n" + log[-1400:] + "\nFix the TEST if it is wrong. If the feature is wrong, still only write the test; it will be reported.\n\n"
        finally:
            e.deny_prefixes = []
        e.report(f"- End-to-end test: {'written, passes' if okk else 'could not be made to pass (not kept)'}")
        out["e2e"] = okk
        return out


# ------------------------------------------------------------------------------------------------ TEST
TEST_WRITE = """Write NEW tests for these files. The goal is tests that FAIL when the code is wrong, not tests that merely run the code.
Cover: boundaries (empty, one, many, max), invalid input and the exact errors raised, each branch, and documented behaviour. Assert on results (values, raised errors, state), never just "does not crash".
Create ONE new file {target} (do not edit existing tests). Every test must PASS on the current code.

GOAL: {goal}
FILES UNDER TEST: {files}

{survivors}CODE:
{context}

{feedback}"""

SURVIVOR_TXT = """These small code changes were made on purpose and NO test noticed (so those behaviours are unprotected). Write tests that would fail for each:
{items}

"""


class TestFlow(Flow):
    __test__ = False                                   # not a pytest class
    id, title = "test", "Strengthen the tests"
    blurb = "Mutate the code in small ways; every mutation the tests miss is a gap. Write tests for the gaps; measure again. Code, not opinion, says whether the tests got better."
    steps = [S("scope", "Choose files", "-", "code"), S("baseline", "Tests green? + mutation score before", "-", "code"),
             S("write", "Write new tests", "tester", "llm"), S("verify", "New tests must pass on today's code", "-", "code"),
             S("mutate", "Mutation score after", "-", "code"), S("strengthen", "Tests for surviving mutations (once)", "tester", "llm")]
    produces = ["tests/test_<name>_extra.py", "tests/test_<name>_gaps.py", "mutation before/after in the report"]
    uses_roles = ["tester"]

    def _write(self, e, files, goal, target, survivors, label):
        existing = set(core.list_files(e.root, e.cfg))
        e.deny_prefixes = [f for f in existing if f.startswith("tests/") or "/tests/" in f]
        feedback = ""
        try:
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                sv = SURVIVOR_TXT.format(items="\n".join(f"- {x['file']} {x['where']}" for x in survivors)) if survivors else ""
                r = e.call("tester", TEST_WRITE.format(goal=goal, files=", ".join(files), target=target, survivors=sv, feedback=feedback,
                                                       context=e.context(goal, None, always=files[:3])), label=f"{label}-try{attempt + 1}")
                res = e.apply(r["text"], f"{label} try {attempt + 1}")
                if any(x["status"] == "quit" for x in res):
                    return "quit"
                if e.review == "dry":
                    return "dry"
                new = [x["rel"] for x in res if x["status"] == "applied"]
                if not new:
                    feedback = "FEEDBACK: nothing was written:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res if x.get("msg")) + f"\nCreate exactly {target}.\n\n"
                    e.mark(r["id"], False, feedback)
                    continue
                st, log = core.run_tests(e.root, e.cfg)
                if st != "pass":
                    kept = self._salvage(e, [f for f in new if f not in existing], files)
                    if kept:
                        st, log = core.run_tests(e.root, e.cfg)
                e.mark(r["id"], st == "pass", log[-300:])
                if st == "pass":
                    fresh = [f for f in new if f not in existing and f.endswith(".py") and (f.startswith("tests/") or "/tests/" in f)]
                    if target not in new and len(fresh) == 1 and not os.path.exists(os.path.join(e.root, target)):
                        # The model named its test file differently; the flow's output (and every later step) is `target`. Seen live:
                        # every failed `test` task ended with passing tests and no tests/test_<name>_extra.py. Moving one passing file is wiring.
                        try:
                            os.makedirs(os.path.dirname(os.path.join(e.root, target)), exist_ok=True)
                            os.replace(os.path.join(e.root, fresh[0]), os.path.join(e.root, target))
                            core._READ_CACHE.pop(os.path.abspath(os.path.join(e.root, fresh[0])), None)
                            e.say(f"[wire] {fresh[0]} -> {target}: the tests passed under another name; the flow's output is {target}")
                            e._trace({"type": "assist", "id": r["id"], "t": time.time(), "flow": e.flow, "step": label,
                                      "fixes": [{"file": target, "why": f"moved {fresh[0]} to {target}"}]})
                            new = [target if f == fresh[0] else f for f in new]
                        except OSError:
                            pass
                    return new
                for f in new:
                    if f not in existing:
                        try:
                            os.remove(os.path.join(e.root, f))
                        except OSError:
                            pass
                feedback = ("FEEDBACK: your new tests FAIL on the current code. A test that fails on today's code is wrong (or exposes a bug, which another flow handles). "
                            "Fix the tests so they describe what the code actually does correctly, or drop the failing ones:\n" + log[-1400:] + "\n\n")
        finally:
            e.deny_prefixes = []
        return []

    def _salvage(self, e, new, under_test):
        """New tests that fail on today's code: keep the good ones instead of throwing the file away. -> True when something was kept.

        Two repairs, both code, both checked again afterwards: a name the tests use but never import (`add` from calc.py) gets its import,
        and test functions that still fail are removed one by one. What is left must still pass the full suite AND the fake-code check
        (no assertion-free or always-true tests), so this can only keep real tests -- the mutation score then says whether they help.
        Seen live: a 4B tester's file with five sound tests and one wrong one was deleted whole, four tries in a row."""
        if not new or not core.has_module("pytest") or e.cfg["tests"].get("command"):
            return False
        import ast
        import arch_fakecode
        defs = {}
        for f in under_test:
            if f.endswith(".py"):
                try:
                    tree = ast.parse(core.read_text(os.path.join(e.root, f))[0])
                except (OSError, SyntaxError, ValueError):
                    continue
                mod = os.path.splitext(f)[0].replace("/", ".")
                for n in tree.body:
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_"):
                        defs.setdefault(n.name, mod)
        kept_any = False
        for rel in new:
            p = os.path.join(e.root, rel)
            argv = [sys.executable, "-m", "pytest", "-q", "--tb=line", "-p", "no:cacheprovider", rel]
            rc, out = core.run_cmd(argv, e.root, e.cfg["limits"]["test_timeout"])
            src = core.read_text(p)[0]
            missing = sorted({n for n in re.findall(r"NameError: name '(\w+)' is not", out) if n in defs})
            if missing:                                   # the import the model forgot
                lines = src.split("\n")
                at = max([i + 1 for i, ln in enumerate(lines) if ln.startswith(("import ", "from "))], default=0)
                lines[at:at] = [f"from {defs[n]} import {n}" for n in missing]
                src = "\n".join(lines)
                core.write_text(p, src)
                rc, out = core.run_cmd(argv, e.root, e.cfg["limits"]["test_timeout"])
            if rc != 0 and re.search(r"ERROR collecting|errors? during collection", out):
                continue                                  # the file does not even load: nothing to keep
            bad = set(re.findall(r"^(?:FAILED|ERROR) [^\s:]+::(?:\w+::)?(\w+)", out, re.M))
            if bad:
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                lines, drop = src.split("\n"), []
                for n in tree.body:
                    if isinstance(n, ast.FunctionDef) and n.name in bad:
                        drop.append(n)
                    elif isinstance(n, ast.ClassDef):
                        drop += [m for m in n.body if isinstance(m, ast.FunctionDef) and m.name in bad]
                for n in sorted(drop, key=lambda x: -x.lineno):
                    start = min([n.lineno] + [d.lineno for d in n.decorator_list]) - 1
                    del lines[start:n.end_lineno]
                src = re.sub(r"\n{4,}", "\n\n\n", "\n".join(lines))
                try:
                    left = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
                except SyntaxError:
                    left = []
                if not left:
                    continue
                core.write_text(p, src)
                rc, out = core.run_cmd(argv, e.root, e.cfg["limits"]["test_timeout"])
                if rc != 0:
                    continue
                e.say(f"[salvage] {rel}: kept {len(left)} test(s), dropped {len(bad)} that fail on today's code ({', '.join(sorted(bad)[:6])})")
                e.report(f"- {rel}: dropped {len(bad)} test(s) that fail on the current code ({', '.join(sorted(bad)[:6])}); kept {len(left)}")
            elif rc != 0:
                continue
            fake = arch_fakecode.check(e.root, [rel], e.cfg["behavior"].get("fake_code", "fail"))
            if any(not x["ok"] for x in fake):
                continue
            kept_any = True
            if missing:
                e.report(f"- {rel}: added the missing import(s) {', '.join(missing)}")
        return kept_any

    def run(self, e, params):
        goal = e.goal
        if not can_test(e):
            e.say("[!] no test runner: install pytest or set a test command in Settings")
            e.report("- Stopped: no test runner.")
            return {"ok": False}
        files = scope_files(e, goal)
        if not files:
            e.say("[-] no Python files to test; name the files in the goal")
            e.report("- No files in scope.")
            return {"ok": False}
        e.say(f"[scope] {', '.join(files)}")
        e.report(f"- In scope: {', '.join(files)}", "")
        st, log = core.run_tests(e.root, e.cfg)
        if st == "fail":
            e.say("[!] the existing tests already fail; fix them first (bugfix flow)")
            e.report("- Stopped: existing tests fail.\n```\n" + log[-600:] + "\n```")
            return {"ok": False}
        mut_on = e.cfg["behavior"].get("mutation", "on") != "off" and st == "pass"
        mmax = int(e.cfg["behavior"].get("mutation_max", 8))
        before = q.mutation_check(e.root, e.cfg, files, mmax, log=e.say) if mut_on else None
        if before and before["ok"]:
            e.say(f"[mutation] before: {before['killed']}/{before['total']} mutations caught ({before['score']}%)")
            e.report(f"- Mutation score before: {before['killed']}/{before['total']} ({before['score']}%)")
            for x in before["survived"]:
                e.report(f"  - missed: `{x['file']}` {x['where']}")
        elif before:
            e.say("[i] mutation check skipped: " + before["error"][:120])
        stem = slug(os.path.splitext(os.path.basename(files[0]))[0])
        done = self._write(e, files, goal, f"tests/test_{stem}_extra.py", (before or {}).get("survived") or [], "write")
        if done in ("quit", "dry"):
            return {"ok": done == "dry", "stopped": done == "quit", "dry": done == "dry"}
        if not done:
            e.say("[!] could not produce passing tests; your project is unchanged")
            e.report("- No passing tests could be produced; nothing was kept.")
            return {"ok": False}
        e.report(f"- New tests: {', '.join(done)} (pass on the current code)")
        after = q.mutation_check(e.root, e.cfg, files, mmax, log=e.say) if mut_on else None
        if after and after["ok"]:
            e.say(f"[mutation] after: {after['killed']}/{after['total']} caught ({after['score']}%)")
            if after["survived"] and e.review != "dry":
                e.say("\n=== Tests for what is still unprotected ===")
                more = self._write(e, files, goal, f"tests/test_{stem}_gaps.py", after["survived"], "gaps")
                if isinstance(more, list) and more:
                    e.report(f"- Gap tests: {', '.join(more)}")
                    after = q.mutation_check(e.root, e.cfg, files, mmax, log=e.say)
                    e.say(f"[mutation] final: {after['killed']}/{after['total']} caught ({after['score']}%)")
            e.report(f"- Mutation score after: {after['killed']}/{after['total']} ({after['score']}%)")
            for x in after["survived"]:
                e.report(f"  - still missed: `{x['file']}` {x['where']}")
        e.report("", "- Tests strengthened: all tests pass.")
        return {"ok": True, "files": e.changed_files, "mutation_before": (before or {}).get("score"), "mutation_after": (after or {}).get("score")}


# ------------------------------------------------------------------------------------------------ OPTIMIZE
OPT_BENCH = """Write a micro-benchmark for the code described below, in the NEW file {target}. Rules:
- import the code under test and call the operation that matters with REALISTIC, deterministic input (seed any randomness), big enough that one run takes at least 0.05 seconds;
- time it with time.perf_counter(); if the operation is destructive, rebuild the input inside the timed loop setup;
- print, as the LAST line of output and nothing after it, JSON like {{"seconds": 0.123}} (seconds per run);
- standard library only; no network; do not change application code.

WHAT TO MEASURE: {goal}
FILES: {files}

CODE:
{context}"""

OPT_DO = """Make this code faster WITHOUT changing behaviour. All tests must still pass.
Think from the measurement, not from habit: find what dominates (algorithmic complexity, repeated work, wrong data structure, N+1 queries, missing index, needless copies) and fix THAT, one change at a time.

GOAL: {goal}
MEASUREMENT: {baseline:.5f} s per run ({detail})
{evidence}CODE:
{context}

{feedback}"""

OPT_SQL = """Make these SQL queries avoid full table scans, WITHOUT changing their results. Prefer adding an index in a NEW migration file (never edit a migration that already exists); change a query only if an index cannot help.

GOAL: {goal}
QUERY PLANS NOW (SCAN = reads every row):
{plans}

FILES:
{context}

{feedback}"""


class OptimizeFlow(Flow):
    id, title = "optimize", "Optimize"
    blurb = "Benchmark first, change one thing, benchmark again. A change is kept only if the tests stay green and it is measurably faster; otherwise it is rolled back."
    steps = [S("scope", "Choose files", "-", "code"), S("baseline", "Tests green + baseline benchmark", "-", "code"),
             S("bench", "Write the benchmark if there is none", "optimizer", "llm"), S("evidence", "Static findings + query plans", "-", "code"),
             S("change", "Propose one change", "optimizer", "llm"), S("measure", "Tests + benchmark again", "-", "code"),
             S("decide", "Keep if faster, else roll back", "-", "code")]
    produces = ["bench/bench_*.py", "optimized source files", "before/after numbers in the report"]
    uses_roles = ["optimizer", "generator_sql"]

    def run(self, e, params):
        goal = e.goal
        if not can_test(e):
            e.say("[!] optimising needs a test runner to prove behaviour is unchanged: install pytest or set a test command")
            return {"ok": False}
        sql_mode = bool(explicit_files(e, goal, (".sql",))) or (not explicit_files(e, goal) and bool(re.search(r"\b(sql|quer(?:y|ies)|index(?:es)?|migrations?)\b", goal.lower())))
        files = scope_files(e, goal, 3, (".sql",) if sql_mode else (".py",))
        if not files and sql_mode:
            sql_mode, files = False, scope_files(e, goal, 3)
        if not files:
            e.say("[-] nothing to optimise: name the files in the goal")
            return {"ok": False}
        e.say(f"[scope] {', '.join(files)}")
        e.report(f"- In scope: {', '.join(files)}", f"- Mode: {'SQL query plans' if sql_mode else 'Python benchmark'}", "")
        st, log = core.run_tests(e.root, e.cfg)
        if st == "fail":
            e.say("[!] tests fail before any change; fix them first")
            e.report("- Stopped: tests fail on the unmodified code.\n```\n" + log[-500:] + "\n```")
            return {"ok": False}
        return self._sql(e, goal, files) if sql_mode else self._py(e, goal, files)

    # ---- Python: benchmark gate
    def _py(self, e, goal, files):
        cfg_b, runs = e.cfg["behavior"], int(e.cfg["behavior"].get("bench_runs", 5))
        need = float(cfg_b.get("min_gain_pct", 10)) / 100.0
        bench = next((x for x in explicit_files(e, goal) if x.startswith("bench/")), None) or next((f for f in core.list_files(e.root, e.cfg) if f.startswith("bench/") and f.endswith(".py")), None)
        if not bench:
            target = f"bench/bench_{slug(os.path.splitext(os.path.basename(files[0]))[0])}.py"
            feedback = ""
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                r = e.call("optimizer", OPT_BENCH.format(target=target, goal=goal, files=", ".join(files), context=e.context(goal, None, always=files[:3])) + ("\n\n" + feedback if feedback else ""), label=f"bench-try{attempt + 1}")
                res = e.apply(r["text"], "benchmark")
                if any(x["status"] == "quit" for x in res):
                    return {"ok": False, "stopped": True}
                if e.review == "dry":
                    return {"ok": True, "dry": True}
                made = [x["rel"] for x in res if x["status"] == "applied" and x["rel"].startswith("bench/")]
                if not made:
                    feedback = "FEEDBACK: create the benchmark exactly at " + target
                    e.mark(r["id"], False, feedback)
                    continue
                t, detail = q.run_bench(e.root, e.cfg, made[0], 2)
                e.mark(r["id"], t is not None, detail)
                if t is not None and t > 0:
                    bench = made[0]
                    break
                feedback = "FEEDBACK: the benchmark does not work: " + detail
            if not bench:
                e.say("[!] could not produce a working benchmark; nothing changed")
                e.report("- Stopped: no working benchmark.")
                return {"ok": False}
        base_t, detail = q.run_bench(e.root, e.cfg, bench, runs)
        if base_t is None:
            e.say("[!] benchmark problem: " + detail)
            e.report("- Stopped: " + detail)
            return {"ok": False}
        e.say(f"[bench] baseline {base_t:.5f}s ({detail})")
        e.report(f"- Benchmark: `{bench}`", f"- Baseline: {base_t:.5f}s per run ({detail})")
        scan = q.static_scan(e.root, files)
        evidence = ("STATIC FINDINGS (hints):\n" + "\n".join(f"- {x['file']}:{x['line']} {x['msg']}" for x in scan[:8]) + "\n\n") if scan else ""
        feedback, best, kept = "", base_t, False
        e.deny_prefixes = ["tests/", "bench/"]
        try:
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                cap = capture(e)
                r = e.call("optimizer", OPT_DO.format(goal=goal, baseline=best, detail=detail, evidence=evidence, feedback=feedback, context=e.context(goal, None, always=files[:3] + [bench])), label=f"opt-try{attempt + 1}")
                res = e.apply(r["text"], f"optimise try {attempt + 1}")
                if any(x["status"] == "quit" for x in res):
                    return {"ok": False, "stopped": True}
                if e.review == "dry":
                    return {"ok": True, "dry": True}
                changed = [x["rel"] for x in res if x["status"] == "applied"]
                if not changed:
                    feedback = "FEEDBACK: nothing could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res if x.get("msg")) + "\n\n"
                    e.mark(r["id"], False, feedback)
                    continue
                v = verify(e, changed)
                log_checks(e, f"optimise try {attempt + 1}", v)
                if not v["ok"]:
                    restore(e, cap)
                    e.mark(r["id"], False, v["text"])
                    feedback = "FEEDBACK: your change broke behaviour or checks, so it was rolled back:\n" + v["text"] + "\n\n"
                    continue
                t, d2 = q.run_bench(e.root, e.cfg, bench, runs)
                if t is None:
                    restore(e, cap)
                    e.mark(r["id"], False, d2)
                    feedback = "FEEDBACK: the benchmark no longer runs after your change (rolled back): " + d2 + "\n\n"
                    continue
                gain = (best - t) / best if best else 0
                e.say(f"[bench] {t:.5f}s  ({gain * 100:+.0f}% vs {best:.5f}s)")
                if gain >= need:
                    e.mark(r["id"], True, f"{gain * 100:.0f}% faster")
                    best, kept = t, True
                    break
                restore(e, cap)
                e.mark(r["id"], False, f"only {gain * 100:.0f}% faster, rolled back")
                feedback = f"FEEDBACK: tests pass but the change was only {gain * 100:+.0f}% (needs {need * 100:.0f}%), so it was rolled back. Find what actually dominates the time and change THAT.\n\n"
        finally:
            e.deny_prefixes = []
        total = (base_t - best) / base_t * 100 if base_t else 0
        e.report("", f"- Result: {'kept' if kept else 'no change kept'}; {base_t:.5f}s -> {best:.5f}s ({total:+.0f}%)")
        e.say(f"[done] {base_t:.5f}s -> {best:.5f}s ({total:+.0f}%)" if kept else "[done] no change met the bar; your code is unchanged")
        return {"ok": kept, "baseline": base_t, "after": best, "files": e.changed_files}

    # ---- SQL: query-plan gate
    def _sql(self, e, goal, files):
        allsql = [f for f in core.list_files(e.root, e.cfg) if f.endswith(".sql")]
        plans = q.sql_plans(e.root, allsql)
        scans = [p for p in plans if p["scan"]]
        e.say(f"[plans] {len(plans)} queries, {len(scans)} with a full scan")
        e.report(f"- Queries: {len(plans)}; full scans before: {len(scans)}", *[f"  - {p['query']}: {p['plan']}" for p in plans])
        if not plans:
            e.say("[-] no named queries found (use '-- name: x' above each query in a .sql file)")
            return {"ok": False}
        if not scans:
            e.say("[+] no full table scans: nothing to do")
            e.report("- Nothing to optimise: every query uses an index.")
            return {"ok": True}
        feedback, ok = "", False
        e.deny_prefixes = [f for f in allsql if "/migrations/" in "/" + f]
        try:
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                cap = capture(e)
                txt = "\n".join(f"- {p['query']}: {p['plan']}" + ("   <-- SCAN" if p["scan"] else "") for p in plans)
                r = e.call("generator_sql", OPT_SQL.format(goal=goal, plans=txt, feedback=feedback, context=e.context(goal, None, always=allsql[:6])), label=f"sqlopt-try{attempt + 1}")
                res = e.apply(r["text"], f"sql optimise try {attempt + 1}")
                if any(x["status"] == "quit" for x in res):
                    return {"ok": False, "stopped": True}
                if e.review == "dry":
                    return {"ok": True, "dry": True}
                changed = [x["rel"] for x in res if x["status"] == "applied"]
                v = verify(e, changed) if changed else {"ok": False, "text": "nothing could be applied", "passed": 0, "total": 0, "results": []}
                log_checks(e, f"sql optimise try {attempt + 1}", v)
                new = q.sql_plans(e.root, [f for f in core.list_files(e.root, e.cfg) if f.endswith(".sql")])
                now = [p for p in new if p["scan"]]
                good = v["ok"] and len(now) < len(scans)
                e.mark(r["id"], good, v["text"] or f"scans {len(scans)} -> {len(now)}")
                if good:
                    ok = True
                    e.say(f"[plans] full scans {len(scans)} -> {len(now)}")
                    e.report(f"- Full scans after: {len(now)}", *[f"  - {p['query']}: {p['plan']}" for p in new])
                    break
                restore(e, cap)
                feedback = "FEEDBACK: rolled back. " + (v["text"] or f"full scans were {len(scans)}, now {len(now)}: not improved") + "\n\n"
        finally:
            e.deny_prefixes = []
        e.report("", f"- Result: {'improved' if ok else 'no change kept'}")
        return {"ok": ok, "files": e.changed_files}


# ------------------------------------------------------------------------------------------------ HUNT
HUNT = """Hunt for REAL bugs in the code below, then PROVE each one with a failing test.
For every suspected defect create ONE file tests/hunt/test_<short_name>.py. Its FIRST line must be a docstring: \"\"\"BUG: <title> | <file>:<function> | <why, one sentence>\"\"\"
The test calls the real code with a concrete input and asserts the CORRECT behaviour, so it FAILS today because of the bug. It must not fail from import, syntax or name errors. Do not edit application code. At most {n} findings, most severe first. If you find nothing provable, write nothing and say so.

Look for: boundaries and off-by-one, empty/None/huge input, wrong error handling, state shared between calls, unvalidated input, resource leaks, time/timezone/rounding mistakes, concurrency, injection.

{hints}CODE:
{context}"""


class HuntFlow(Flow):
    id, title = "hunt", "Bug hunt"
    blurb = "A bug hunter reads the code adversarially. Every suspicion must be proven by a failing test or it is discarded; proven bugs are listed in docs/bugs.md and can be fixed automatically."
    steps = [S("scope", "Choose files", "-", "code"), S("static", "Static bug patterns (syntax tree)", "-", "code"),
             S("hunt", "Hunt: one proving test per suspicion", "bug_hunter", "llm"), S("prove", "Run each test: fails for the right reason = confirmed", "-", "code"),
             S("report", "Write docs/bugs.md", "-", "code"), S("fix", "Optionally fix each confirmed bug", "generator", "llm")]
    produces = ["tests/hunt/test_*.py (confirmed bugs only)", "docs/bugs.md"]
    uses_roles = ["bug_hunter", "generator", "reviewer"]

    def run(self, e, params):
        goal = e.goal
        if not core.has_module("pytest"):
            e.say("[!] the bug hunt needs pytest to prove findings: python -m pip install pytest")
            e.report("- Stopped: pytest is not installed.")
            return {"ok": False}
        n_files = int(e.cfg["behavior"].get("hunt_max_files", 4))
        files = scope_files(e, goal, n_files)
        if not files:
            e.say("[-] no Python files to hunt in")
            return {"ok": False}
        e.say(f"[scope] {', '.join(files)}")
        scan = q.static_scan(e.root, files)
        high = [x for x in scan if x["severity"] == "high"]
        e.say(f"[static] {len(scan)} pattern(s), {len(high)} high")
        e.report(f"- In scope: {', '.join(files)}", f"- Static findings: {len(scan)} ({len(high)} high)", "")
        hints = ("STATIC FINDINGS (hints; may be false alarms):\n" + "\n".join(f"- {x['file']}:{x['line']} [{x['rule']}] {x['msg']}" for x in scan[:12]) + "\n\n") if scan else ""
        existing = set(core.list_files(e.root, e.cfg))
        e.deny_prefixes = [f for f in existing if f.startswith("tests/") and not f.startswith("tests/hunt/")]
        r = e.call("bug_hunter", HUNT.format(n=int(e.cfg["behavior"].get("hunt_findings", 5)), hints=hints, context=e.context(goal, None, always=files[:4])), label="hunt")
        res = e.apply(r["text"], "hunt tests")
        e.deny_prefixes = []
        if any(x["status"] == "quit" for x in res):
            return {"ok": False, "stopped": True}
        if e.review == "dry":
            return {"ok": True, "dry": True}
        tests = [x["rel"] for x in res if x["status"] == "applied" and x["rel"].startswith("tests/hunt/") and x["rel"] not in existing]
        confirmed, rejected = [], 0
        for t in tests:
            full = os.path.join(e.root, t)
            doc = re.search(r"BUG:\s*(.+?)(?:\"\"\"|'''|\n)", core.read_text(full, 4000)[0])
            title = (doc.group(1).strip() if doc else t)[:200]
            st, log = core.run_tests(e.root, e.cfg, [t])
            wrong = re.search(r"SyntaxError|ModuleNotFoundError|ImportError|NameError|IndentationError|fixture .* not found", log)
            if st == "fail" and not wrong:
                confirmed.append({"test": t, "title": title, "evidence": log[-500:]})
                e.say(f"[CONFIRMED] {title}")
            else:
                rejected += 1
                e.say(f"[discarded] {title}  ({'passes: not a bug' if st == 'pass' else 'invalid test'})")
                try:
                    os.remove(full)
                    if t in e.created:
                        e.created.remove(t)
                    if t in e.changed_files:
                        e.changed_files.remove(t)
                except OSError:
                    pass
        e.mark(r["id"], bool(confirmed), f"{len(confirmed)} confirmed, {rejected} discarded")
        lines = ["# Bug hunt", "", f"Scope: {', '.join(files)}", "", f"## Confirmed bugs ({len(confirmed)})", ""]
        for c in confirmed:
            lines += [f"### {c['title']}", f"- Proving test: `{c['test']}` (fails today)", "- Evidence:", "```", c["evidence"].strip()[-400:], "```", ""]
        if not confirmed:
            lines += ["None proven in this pass. That is not proof the code is bug-free.", ""]
        lines += [f"## Discarded suspicions: {rejected}", "", "## Static findings (unproven hints)", ""]
        lines += [f"- `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']}: {x['msg']}" for x in scan] or ["- none"]
        e.put_file("docs/bugs.md", "\n".join(lines), "bug report", quiet=True)
        e.report("", f"- Confirmed: {len(confirmed)}; discarded: {rejected}; report: docs/bugs.md")
        fixed = 0
        if params.get("autofix") and confirmed:
            orig_goal = e.goal
            for c in confirmed[:3]:
                e.say(f"\n=== Fixing: {c['title']} ===")
                e.goal = c["title"] + "\n" + c["evidence"][-400:]
                try:
                    out = base.BugfixFlow().run(e, {"repro": c["test"]})
                finally:
                    e.goal = orig_goal
                fixed += 1 if out.get("ok") else 0
            e.report(f"- Auto-fixed: {fixed}/{min(len(confirmed), 3)}")
        return {"ok": True, "confirmed": len(confirmed), "fixed": fixed, "files": e.changed_files}


# ------------------------------------------------------------------------------------------------ REVIEW
REVIEW = """Review this code as a senior engineer. Write Markdown with exactly these sections:
## Summary   (3 sentences: what it does, overall health, the single most important thing to fix)
## Risks     (a table: severity | where (file:function) | problem | suggested fix; most severe first; only real problems)
## Design    (structure, coupling, naming, duplication: max 5 bullets)
## Missing tests   (specific behaviours that no test seems to cover)
Be concrete. Do not invent problems in code you were not shown.

FACTS FROM TOOLS (trust these):
{facts}

CODE:
{context}"""


class ReviewFlow(Flow):
    id, title = "review", "Code review"
    blurb = "Static pattern scan and size/complexity metrics (facts, from code) plus a reviewer's notes, written to docs/review.md."
    steps = [S("scope", "Choose files", "-", "code"), S("facts", "Static scan + metrics", "-", "code"),
             S("review", "Reviewer's notes", "reviewer", "llm"), S("write", "Write docs/review.md", "-", "code")]
    produces = ["docs/review.md"]
    uses_roles = ["reviewer"]

    def run(self, e, params):
        files = scope_files(e, e.goal, 6)
        if not files:
            e.say("[-] no Python files to review")
            return {"ok": False}
        scan = q.static_scan(e.root, files)
        met = ck.metrics_python(e.root, files)
        facts = f"metrics: {met}\n" + "\n".join(f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}" for x in scan) if scan else f"metrics: {met}\n- no static findings"
        e.say(f"[facts] {len(scan)} static finding(s); {met}")
        r = e.call("reviewer", REVIEW.format(facts=facts, context=e.context(e.goal, None, always=files[:5])), label="review")
        text = core.strip_think(r["text"]).strip()
        text = re.sub(r"^```(?:markdown|md)?\n|\n```$", "", text)
        body = "# Code review\n\nFiles: " + ", ".join(files) + "\n\n## Facts from tools\n\n" + facts + "\n\n" + text + "\n"
        p = e.put_file("docs/review.md", body, "review", quiet=True)
        e.report("", f"- Review written: docs/review.md ({p['status']}); static findings: {len(scan)}")
        return {"ok": True, "files": ["docs/review.md"], "findings": len(scan)}


for _f in (NewFlow(), FeatureFlow(), TestFlow(), OptimizeFlow(), HuntFlow(), ReviewFlow()):
    base.FLOWS[_f.id] = _f

import arch_flows_sec  # noqa: E402,F401  (registers secure/config/network/ml/pentest into FLOWS)
