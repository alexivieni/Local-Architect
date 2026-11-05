---
name: eval_family
title: An eval family judged by hidden tests
purpose: Writes an evals/families/*.py file that expands task templates into many tasks, each with fixture files, hidden acceptance tests, a reference solution and a deliberately wrong one, plus a document on how to run it.
why: Verified traces only come from tasks that code can judge; a reference and a wrong solution prove the judge both ways. Designing deterministic tasks with good hidden tests is judgment work.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 4
files: evals/families/{family}.py, docs/{doc}.md
tests:
footer: common
source: brief_traces.md, brief_hybrid.md
slot.family: the family's file name, without .py
slot.doc: the document's file name under docs/, without .md
slot.tasks: what the tasks cover and how many
example.family: distributed
example.doc: training_plan
example.tasks: at least 12 templates expanded to 60 tasks: rate limiters, retries with backoff, idempotent handlers, a leased work queue, consistent hashing, circuit breaker
---
You are the eval-family agent for "{app}". YOUR FILES: NEW evals/families/{family}.py, NEW docs/{doc}.md.
Read first: CLAUDE.md (evals), evals/families/e2e.py and two other families (the exact format the loader expects), arch_eval.py,
arch_dataset.py (only verified local work becomes training data).
Tasks: {tasks}. Each task: goal text, fixture files, hidden acceptance tests (stdlib only, deterministic), a reference solution and a
wrong one. docs/{doc}.md: the exact commands to run it, what each number means, and how the traces flow into training.
