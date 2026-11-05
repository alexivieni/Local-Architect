---
name: suite_fixer
title: Fix failing self-test checks
purpose: Reads the failing checks and the code they test, fixes the code when the code is wrong or the test when its expectation is wrong (and says which), and keeps every other check passing.
why: A failure log points at the exact place; the fix is usually small. A local worker that can run the suite itself sees at once whether its fix worked, which a Claude agent in accept-edits mode cannot.
# local_ok: yes
# tier: worker
mode: acceptEdits
model:
budget_usd: 2
files: {files}
tests: python arch_selftest_{suite}.py
footer: common
source: brief_fix1.md
slot.files: the modules and tests it may change (comma-separated)
slot.suite: the self-test's short name
slot.failures: the failing check names and the error text
example.files: arch_agentlib.py, arch_selftest_agentlib.py
example.suite: agentlib
example.failures: template_library: fill in folder (ValueError from arch_agentlib._values while the test fills every template)
---
You are a fixer for "{app}". YOUR FILES: {files}.
These checks fail: {failures}
Read the modules and the tests fully before changing anything. Fix the code if the code is wrong, the test if the test's
expectation is wrong, and say which and why. Run the suite (arch_selftest_{suite}.py) after each change. Keep every other check
passing; do not weaken a check to make it pass.
