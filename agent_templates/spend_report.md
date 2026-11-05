---
name: spend_report
title: Spend and results report
purpose: Builds a summary of what runs cost and how many finished, read from the run traces, plus a cheaper path for work that can wait.
why: "What does a finished task cost me?" is the number the owner decides with, so it is computed from the traces the runs already write (never estimated), per passed run, with the local share beside it. Work that can wait goes through a half-price queue behind an injectable client.
mode: acceptEdits
model:
budget_usd: 2.5
files: {summary_module}.py, {queue_module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_costs.md
slot.summary_module: the summary module's name, without .py
slot.queue_module: the deferred-work module's name, without .py
slot.suite: the self-test's short name
slot.window: how far back the summary reads
slot.fields: what the summary returns
example.summary_module: arch_spend
example.queue_module: arch_later
example.suite: spend
example.window: 7 days, newest first, stop reading after that
example.fields: runs, passed, cost_usd, per_task_usd, local_share, by_model, by_day
---
You are the cost agent for "{app}". YOUR FILES: NEW {summary_module}.py, NEW {queue_module}.py, NEW arch_selftest_{suite}.py.
Read first (read only): arch_receipt.py (totals() reads a run's trace), arch_core.data_dir(), arch_cloud.py (cost(), allowed(),
_testing()), arch_supervisor.py.
1. {summary_module}.summary(days, roots=None) -> {fields}. Read .arch/runs/*/trace.jsonl for the project roots (the `call` records
   carry model, prompt_tokens, gen_tokens, usd, transport); a run counts as passed when its verdict record says pass. Window: {window}.
   per_task_usd = cost / passed runs.
2. {queue_module}.py: submit(items) -> id stored in data_dir(), poll(id) -> status and results keyed by custom id (they arrive in any
   order); a `client=` parameter for tests; lazy SDK import; refused when arch_cloud.allowed() says no.
3. arch_selftest_{suite}.py: two fake runs in a temp folder (one passed, one failed, one paid call) prove summary(); a fake client
   proves submit and poll.
