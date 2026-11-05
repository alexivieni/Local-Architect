---
name: local_twin
title: The same job on local models
purpose: Makes something the cloud agents already do possible with the local models too, scheduled around the one graphics card.
why: On a single 6 GB card two local writers would evict each other's models, so local work that writes is serialised on the card while planning-only steps (the CPU router) and cloud agents run beside it. Recording who waited and why makes the queue visible instead of mysterious.
mode: acceptEdits
model:
budget_usd: 3
files: {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_localorch.md
slot.module: the existing module to extend, without .py
slot.suite: the self-test's short name
slot.cloud_side: what the module does today with cloud agents
slot.local_run: how one local task runs
slot.pattern: the mixed pattern to offer
example.module: arch_widget
example.suite: widget_local
example.cloud_side: runs several Claude Code agents in parallel, each with its own files and budget, then runs tests
example.local_run: architect.py run "goal" --flow X --root R as a subprocess
example.pattern: Claude plans and reviews, local models write
---
You are the local-orchestration agent for "{app}". YOUR FILES: {module}.py and NEW arch_selftest_{suite}.py.
Read first: CLAUDE.md, {module}.py ({cloud_side}), arch_supervisor.py, arch_heavy.py, and how {local_run} works.
1. Local agents: an entry of kind "local" runs {local_run} on the local models (no cost), with the same file-ownership rule.
   ONE graphics card: local agents that write run one at a time on the GPU worker; planning-only steps on the CPU router and
   cloud agents may run beside them. Record in the state which agents waited, for whom and why.
2. A helper for the mixed pattern: {pattern}, each part on its own files.
3. Everything a local agent does is persisted like the cloud runs, so the experience store can learn from both.
arch_selftest_{suite}.py: with an injected runner (no subprocess, no GPU), prove two local writers never overlap, cloud and CPU
agents overlap with a local one, overlapping files are refused, the helper's shape, and the state on disk.
