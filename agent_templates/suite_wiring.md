---
name: suite_wiring
title: Wire new self-tests into the full suite
purpose: Adds every arch_selftest_*.py that exists but is missing from the suite list in architect.py, in the group that fits, and changes nothing else.
why: A suite that is not in the list never runs, so its checks prove nothing. The job is mechanical (compare a folder listing with a list); a small local model with ls, grep and edit_lines does it well, and can run the suite itself.
# local_ok: yes
# tier: worker
mode: acceptEdits
model:
budget_usd: 1
files: architect.py
tests:
footer: common
source: brief_register.md, brief_release.md (the suite-list check)
slot.expected: suites that should be there (a hint; the folder listing decides)
example.expected: cloud, costs, crew, fleet, localfleet, logbook, fastlane, tokens, agentlib, overseer, placement
---
You are the test-wiring agent for "{app}". YOUR FILES: architect.py only.
Find the self-test suite list (_selftest_suites or similar). List the arch_selftest_*.py files in the folder (ls) and add every one
that is missing, in the group that fits (core vs all), so `python architect.py selftest --suite full` runs them all.
Expected newcomers include: {expected}. Check each against the folder listing; do not add a name whose file does not exist.
Do not change anything else in architect.py.
