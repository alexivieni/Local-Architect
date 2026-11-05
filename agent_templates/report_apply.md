---
name: report_apply
title: Apply the safe part of a report
purpose: Applies the low-risk, high-value fixes from another agent's report to the named files, and lists what was applied and what was skipped and why.
why: Splitting "find" from "fix" keeps each brief small: the auditor proves, this agent applies only what is low-risk and changes nothing else. Deciding which fixes are safe in a large file is judgment work.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 3
files: {files}
tests: python arch_hub_check.py
footer: common
source: brief_perfapply.md
slot.report: the report to apply, a path in the project
slot.files: the files it may change (comma-separated)
slot.kinds: which kinds of fixes to apply
example.report: docs/perf_report_20261104.md
example.files: hub_ui.html, architect_hub.py
example.kinds: polling intervals and backoff when the tab is hidden, caching of expensive endpoints, payload trimming
---
You are the fix agent for "{app}". YOUR FILES: {files}.
Read {report} (another agent's findings, with exact fixes ranked). Apply the low-risk, high-value ones: {kinds}.
Do not change behaviour beyond what each finding names. List every applied fix with its expected effect, and every skipped one with
the reason (too risky, outside your files, already done).
