---
name: flow_improver
title: Improve a flow from evidence, never automatically
purpose: Builds diagnose / propose / trial / adopt for data flows: find where runs fail or cost most, offer validated YAML edits with their evidence, trial each as a new flow id against the eval set, adopt only on no regression.
why: Flows are data, so they can be tuned like data, but only with the regression gate in the loop and a person deciding. Reading failure evidence and proposing sensible edits is judgment work.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 3
files: arch_iterate.py, arch_selftest_iterate.py
tests: python arch_selftest_iterate.py
footer: common
source: brief_iterate.md
---
You are the workflow-iterator agent for "{app}". YOUR FILES: NEW arch_iterate.py, NEW arch_selftest_iterate.py.
Read arch_dataflow.py (flows as data, validation), flows/*.yaml, arch_eval.py (eval --compare), arch_learn.py and arch_overseer.py
(outcomes), CLAUDE.md (the no-regression rule).
- diagnose(flow_id) -> where runs of this flow fail or cost most: step, gate, retries, time, typical failure text.
- propose(flow_id) -> candidate YAML edits (retries, budgets, step order, an added scout or judge step, a tier change), each with the
  evidence behind it; every candidate passes the flow validator before it is offered.
- trial(flow_id, candidate, quick=True) -> writes the candidate as a NEW flow id, runs the eval set for both (one at a time, local
  models) and returns the comparison; adopt(candidate) only on no regression, keeping the old file as a backup. Nothing is adopted
  automatically.
Tests with fixture traces and a fake eval runner.
