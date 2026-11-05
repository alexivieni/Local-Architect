---
name: decision_module
title: A module that decides from measured facts
purpose: Builds one module that makes a named decision (where a job runs, which flow fits, how a request is sharpened) from measured data, explains it in plain words, logs it as data, and is tested on fake inputs for every branch.
why: The broker, placement, route-learning and sharpening agents had one shape: facts in, a decision with its reason out, a log so decisions become data, a plain explain(). Choosing the policy and its edge cases is judgment work.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 3
files: {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_broker.md, brief_placement.md, brief_routelearn.md, brief_augment.md
slot.role: the agent's role name
slot.module: the new module, without .py
slot.suite: the self-test's short name
slot.read_first: what to read first
slot.decides: the main function and what it returns
slot.policy: the rules the decision must follow
example.role: workload-broker
example.module: arch_broker
example.suite: broker
example.read_first: CLAUDE.md, arch_hw.live(), arch_server.serving and busy, arch_cloud (allowed, cost)
example.decides: route(job) -> target (gpu_worker, heavy, cpu_router, ecores, claude, wait), why, eta_s, cost_usd
example.policy: local first; Claude only when quality or the deadline needs it and the caps allow; measured numbers over guesses
---
You are the {role} agent for "{app}". YOUR FILES: NEW {module}.py, NEW arch_selftest_{suite}.py.
Read first: {read_first}.
Build {decides}. Policy: {policy}.
Every decision carries a reason in plain words (explain(decision)) and says which numbers were measured and which are estimates.
Keep a small decision log under the data folder so decisions become data. Never change a setting or move a file by itself.
arch_selftest_{suite}.py: fake inputs (no GPU, no network, no model) covering every branch, including "not enough data: no opinion".
