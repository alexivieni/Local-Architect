"""purge flow (registered into arch_flows.FLOWS): find fake code -> have the team make it real or delete it -> prove it.

  purge   arch_fakecode scans (stubs, assertion-free tests, always-true assertions, functions that pretend to work, swallowed
          errors, placeholder data) -> the generator implements each for real, or deletes it if nothing uses it, or adds
          real assertions to a test -> the SAME scanner runs again, the project's tests run, and the number of tests with
          real assertions must not go down (deleting a real test is not a fix) -> docs/fakecode.md

Code decides: a finding is only gone when the scanner no longer reports it.
"""
import os
import re

import arch_core as core
import arch_fakecode as fc
import arch_flows as base
from arch_flows import Flow, S, verify, log_checks
from arch_flows_pro import explicit_files

if "purge" not in base.PRIORITY:
    base.PRIORITY.insert(base.PRIORITY.index("test"), "purge")      # wins a tie against "test" for requests about empty/placeholder tests
base.KEYWORDS["purge"] = r"fake code|placeholder|\bstubs?\b|dead code|remove (?:the )?todos?|not implemented|mock data|assert(?:s)? nothing|empty tests?"

PURGE_FIX = """These are findings from a scanner for FAKE CODE: code that looks finished but is not. Fix every one for real:
- stub / not implemented / constant stub / pretends to work: implement it properly from what the name, docstring, callers and tests tell you;
  if nothing calls it and its purpose is unclear, DELETE it instead of leaving a placeholder.
- test that asserts nothing / always-true assertion: add assertions that check real behaviour of the code under test. Do NOT delete a
  test that checks something real, and do not replace one placeholder with another.
- swallowed error: handle it (log with context, re-raise, or narrow the except) rather than ignoring it.
- placeholder text: replace with real content or remove it.
If a finding is a false positive here, leave it and say why in one line.

FINDINGS:
{findings}

CODE:
{context}

{feedback}"""


def _targets(found):
    return [x for x in found if x["severity"] in ("high", "medium")]


def real_tests(root, files):
    """How many test functions really assert something (used so 'fixing' can never mean deleting real tests)."""
    import ast
    n = 0
    for rel in files:
        if not (rel.endswith(".py") and fc.is_test_file(rel)):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8", errors="replace").read())
        except (OSError, SyntaxError):
            continue
        n += sum(1 for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test") and fc._has_assertion(f))
    return n


class PurgeFlow(Flow):
    id, title = "purge", "Remove fake code"
    blurb = ("Finds stubs, assertion-free tests, always-true assertions, functions that pretend to work, swallowed errors and placeholder data, "
             "has the team make each real or delete it, and proves it by scanning again with the same scanner (and by never losing a real test).")
    steps = [S("scan", "Scan for fake code", "-", "code"), S("fix", "Make it real or remove it (loop)", "generator", "llm"),
             S("rescan", "Re-scan: findings gone, tests pass, no real test lost", "-", "code"), S("report", "Write docs/fakecode.md", "-", "code")]
    produces = ["fixed source files", "docs/fakecode.md"]
    uses_roles = ["generator"]

    def run(self, e, params):
        goal = e.goal
        files = explicit_files(e, goal, (".py",)) or [f for f in core.list_files(e.root, e.cfg) if f.endswith(".py")]
        if not files:
            e.say("[-] no Python files in scope")
            return {"ok": False}
        found = fc.scan(e.root, files)
        before = _targets(found)
        tests_before = real_tests(e.root, files)
        e.say(f"[scan] {len(before)} fake-code finding(s) (high/medium) in {len(files)} file(s); {tests_before} test(s) with real assertions")
        e.report(f"- Scanned: {len(files)} file(s)", f"- Findings (high/medium): {len(before)}", f"- Tests with real assertions: {tests_before}", "")
        for x in before[:15]:
            e.report(f"  - `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']}: {x['msg']}")
        if not before:
            e.put_file("docs/fakecode.md", "# Fake code\n\nNo findings in the scanned files.\n", "fake-code report", quiet=True)
            e.report("", "- Nothing to fix. Report: docs/fakecode.md")
            return {"ok": True, "files": ["docs/fakecode.md"], "findings_before": 0, "findings_after": 0}
        affected = sorted({x["file"] for x in before})
        txt = "\n".join(f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}" for x in before[:25])
        feedback, ok, after = "", False, before
        for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
            r = e.call("generator", PURGE_FIX.format(findings=txt, feedback=feedback, context=e.context(goal, None, always=affected[:4])), label=f"purge-try{attempt + 1}")
            res = e.apply(r["text"], f"purge try {attempt + 1}")
            if any(x["status"] == "quit" for x in res):
                return {"ok": False, "stopped": True}
            if e.review == "dry":
                return {"ok": True, "dry": True}
            changed = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
            if not changed:
                feedback = "FEEDBACK: nothing could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res) + "\n\n"
                e.mark(r["id"], False, feedback)
                continue
            v = verify(e, changed)
            log_checks(e, f"purge try {attempt + 1}", v)
            now = fc.scan(e.root, files)
            after = _targets(now)
            tests_after = real_tests(e.root, files)
            lost = tests_before - tests_after
            gone = len(before) - len(after)
            good = v["ok"] and gone > 0 and lost <= 0
            why = v["text"] or (f"{lost} real test(s) were lost: do not delete tests that check something" if lost > 0 else f"{gone} finding(s) gone, {len(after)} remain")
            e.mark(r["id"], good, why)
            if good:
                ok = True
                break
            feedback = "FEEDBACK: " + (why if lost > 0 or v["text"] else f"no finding actually went away ({len(after)} still reported)") + "\n\n"
        e.say(f"[scan] after: {len(after)} finding(s) remain (started with {len(before)})")
        e.report(f"- Findings after: {len(after)} (from {len(before)})")
        body = ["# Fake code", "", f"Scanned: {', '.join(files[:20])}", "", f"## Before: {len(before)}", ""]
        body += [f"- `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']}: {x['msg']}" for x in before]
        body += ["", f"## After: {len(after)}", ""]
        body += [f"- `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']}: {x['msg']}" for x in after] or ["- none"]
        e.put_file("docs/fakecode.md", "\n".join(body) + "\n", "fake-code report", quiet=True)
        e.report("", "- Report: docs/fakecode.md")
        return {"ok": ok, "files": e.changed_files + ["docs/fakecode.md"], "findings_before": len(before), "findings_after": len(after)}


for _f in (PurgeFlow(),):
    base.FLOWS[_f.id] = _f
