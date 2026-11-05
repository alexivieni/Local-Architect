---
name: project_starters
title: More project starters
purpose: Adds project templates registered the same way as arch_templates_more.py, each with working files, passing tests and a README, stdlib only.
why: A starter follows an existing pattern exactly, and its own tests say whether it works, which a local worker can run itself.
# local_ok: yes
# tier: worker
mode: acceptEdits
model:
budget_usd: 3
files: arch_templates_extra.py, arch_selftest_templates_extra.py, arch_templates.py
tests: python arch_selftest_templates_extra.py
footer: common
source: brief_templates.md
slot.starters: the starters to add
example.starters: a local LLM tool server, a job queue with leases and retries, a rate-limited API gateway, a small RAG over SQLite FTS5
---
You are the templates agent for "{app}". YOUR FILES: NEW arch_templates_extra.py, NEW arch_selftest_templates_extra.py, and in
arch_templates.py only the one import line that registers the new module (read how arch_templates_more.py is registered).
Add these starters: {starters}. Each: files that work, passing tests, a README, stdlib only, the same structure as the existing
templates. Tests: each renders and its own tests pass.
