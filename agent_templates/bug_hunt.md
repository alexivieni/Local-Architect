---
name: bug_hunt
title: Bug hunt
purpose: Reproduces one reported problem with a new failing test, then fixes it in the named file without touching the tests that judge it.
why: A fix is only believable when a test failed before it and passes after it. Keeping the existing tests read-only (and the reproduction in a NEW file) stops the easy cheat of editing the judge; naming the one file to fix keeps the hunt from wandering into other agents' files.
mode: acceptEdits
model:
budget_usd: 2
files: {target}, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: CLAUDE.md (the bugfix flow's rules), METHOD.md (checks must ignore problems that predate the run)
slot.symptom: what goes wrong, in the reporter's words
slot.target: the file the fix goes in
slot.suite: the NEW self-test that reproduces it
slot.clues: where to start looking (functions, logs, the input that triggered it)
example.symptom: paginate() returns page 0 rows for page 1
example.target: arch_widget.py
example.suite: paginate_repro
example.clues: arch_widget.paginate, the offset arithmetic
---
You are the bug-hunt agent for "{app}". YOUR FILES: {target}, NEW arch_selftest_{suite}.py.
The problem: {symptom}. Start here: {clues}.
1. Write arch_selftest_{suite}.py first: the smallest test that fails because of this problem (and would pass once it is fixed).
   It must fail for the RIGHT reason: say in a comment which line of the output shows the bug.
2. Fix it in {target}, the smallest change that makes the reproduction pass. Existing tests are read-only: do not edit them,
   the files that decide which tests run, or add a conftest.py.
3. Look for the same mistake nearby (the same arithmetic, the same pattern) and list it in the report; fix it only in {target}.
