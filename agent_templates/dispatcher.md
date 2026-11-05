---
name: dispatcher
title: Dispatcher
purpose: Builds an orchestrator that checks a plan, starts several workers at once, watches them, and then verifies the result itself.
why: Workers in accept-edits mode cannot run commands, so the proof has to belong to the orchestrator. Refusing a bad plan (shared files, empty briefs, budgets past the cap) before anything is spent is the cheapest check there is; injected starter= and runner= make the whole thing testable with no SDK, network or subprocess.
mode: acceptEdits
model:
budget_usd: 4
files: {module}.py, arch_selftest_{suite}.py, {runs_module}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_fleet.md
slot.module: the dispatcher module, without .py
slot.suite: the self-test's short name
slot.runs_module: the existing module that starts one worker (it also gets an on-disk run log)
slot.worker: what one worker is
slot.allowed: the allow-list of test command prefixes
example.module: arch_crewboss
example.suite: crewboss
example.runs_module: arch_claudecode
example.worker: one Claude Code agent run through the Agent SDK, billed to the API key
example.allowed: python arch_, py -3.12 arch_, node --check
---
You are the dispatcher agent for "{app}". YOUR FILES: NEW {module}.py, NEW arch_selftest_{suite}.py, and {runs_module}.py.
Read {runs_module}.py fully first. One worker is {worker}.
1. {runs_module}.py: persist every run on disk (one JSON line per event plus a meta file); add list_runs() and load_run(id).
   Keep the in-memory behaviour unchanged.
2. {module}.py:
   - plan_check(plan, total_cap_usd) -> problems in plain words: overlapping files, empty briefs, a total budget above the cap.
   - launch(plan, root, total_cap_usd, tests) -> id: starts every worker at once with its brief plus a footer naming its files
     and forbidding all others; state in data_dir().
   - a watcher thread: when all workers are done, the orchestrator runs the test commands itself (timeout, captured output,
     below-normal priority) and records pass / fail / could-not-run per command plus `git diff --stat`.
   - status(id), list_all(). Only test commands starting with: {allowed}; shell punctuation refused.
3. arch_selftest_{suite}.py: plan_check refusals, launch with a fake starter and runner, state written, tests recorded,
   could-not-run kept as its own outcome, the allow-list refusing other commands, the run log read back.
