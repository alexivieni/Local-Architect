---
name: builder
title: Builder
purpose: Writes one new module and its self-test, against an interface fixed in the brief.
why: A new file has no other owner, so it can run beside anything. Naming the functions and their arguments up front lets the UI or another agent call them while they are still being written; injectable stand-ins (client=, runner=, starter=) let the test prove it without network, GPU or subprocess.
mode: acceptEdits
model:
budget_usd: 3
files: {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_costs.md, brief_learn.md, brief_apidocs.md (the shape they share)
slot.role: what this agent is called in one or two words
slot.module: the new module's name, without .py
slot.suite: the self-test's short name (arch_selftest_<suite>.py)
slot.read_first: files to read before writing (read only)
slot.interface: the public functions with their arguments and what they return
slot.details: rules, edge cases, storage, what must be refused
slot.proof: what the self-test must prove
example.role: spend-summary
example.module: arch_widget
example.suite: widget
example.read_first: arch_receipt.py (totals()), arch_core.data_dir()
example.interface: summary(days=7) -> {days, runs, passed, cost_usd}
example.details: read run traces newest first and stop after `days`; never touch the network
example.proof: two fake runs in a temp folder (one passed, one failed) give the right totals
---
You are the {role} agent for "{app}". YOUR FILES: NEW {module}.py, NEW arch_selftest_{suite}.py.
Read first (read only): {read_first}.

1. {module}.py -- the interface (other agents and the hub call these exact names, so keep them):
   {interface}
   Details: {details}
   Lazy-import anything optional; give every outside dependency an injectable stand-in parameter for the tests.
2. arch_selftest_{suite}.py: isolate the data folder (ARCHITECT_DATA_DIR in a temp folder), no network, no GPU. Prove: {proof}.
