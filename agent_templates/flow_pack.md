---
name: flow_pack
title: New data flows with their prompts
purpose: Writes new flows/<id>.yaml files and their prompts that encode a working pattern, each with budgets and gates, passing the flow validator, prompts prefix-stable.
why: A new flow is data plus prompts, no Python. The validator's rules (reserved step ids, gate names, budgets) are strict and the patterns need design choices, so a strong model writes them; the reviewer runs the validator.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 4
files: {flow_files}
tests:
footer: common
source: brief_flowtpl.md
slot.flow_files: the new flow files and prompt folders (comma-separated)
slot.patterns: each flow id and the pattern it encodes
example.flow_files: flows/fanout_review.yaml, prompts/fanout_review, flows/scout_then_build.yaml, prompts/scout_then_build
example.patterns: fanout_review = several reviewer passes, then a judge keeps findings that survive a second check; scout_then_build = a cheap local step summarises the relevant code into a brief that the build steps use
---
You are the flow-template agent for "{app}". YOUR FILES: NEW files only: {flow_files}.
Read first: CLAUDE.md (flows are data), arch_dataflow.py (step kinds, validation rules, the reserved step-id check), arch_gates.py
(gate names), three existing flows/*.yaml with their prompts.
Write these flows, each with budgets and gates: {patterns}.
Each must pass the validator (you cannot run it: the reviewer runs `python architect.py flows --validate`). Prompts are
prefix-stable: the stable part first, the per-try feedback last.
