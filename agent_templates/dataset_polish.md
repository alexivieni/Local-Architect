---
name: dataset_polish
title: Improve an existing module, item by item
purpose: Makes a numbered list of small improvements to one existing module, checking first whether each already exists, each proven by a fixture.
why: Existing modules often already do half of what is asked; "first check whether each item already exists" saves money and avoids a second copy of a rule. Numbered items with one fixture each make the report checkable line by line.
mode: acceptEdits
model:
budget_usd: 2
files: {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_data.md
slot.module: the module being improved, without .py
slot.suite: the self-test's short name
slot.rules: the CLAUDE.md rules this module must keep (name the sections)
slot.items: the numbered improvements
example.module: arch_dataset
example.suite: data2
example.rules: three outcomes (not_sure is never training data); Claude-made replies are excluded from training
example.items: 1. drop near-duplicate prompts 2. a 0-1 quality score from code-side evidence 3. a stats report of what was dropped and why
---
You are the improvement agent for "{app}". YOUR FILES: {module}.py and NEW arch_selftest_{suite}.py.
Read {module}.py fully, and these rules in CLAUDE.md: {rules}.
Make these improvements, small and verified -- first check whether each item already exists, and say so in the report:
{items}
arch_selftest_{suite}.py: one fixture per item proving it, plus one proving the rules above still hold.
