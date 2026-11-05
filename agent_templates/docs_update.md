---
name: docs_update
title: Bring README and CLAUDE.md up to date
purpose: Adds what is new to README.md (user-facing, plain words) and CLAUDE.md (developer rules and the Layout list), reading the new modules' docstrings, without rewriting either file.
why: The docs drift every time agents ship in parallel. The facts are in the module docstrings; the work is careful summarising in the files' existing tone, which suits a local worker that reads with tools.
# local_ok: yes
# tier: worker
mode: acceptEdits
model:
budget_usd: 2
files: README.md, CLAUDE.md
tests:
footer: common
source: brief_docs.md
slot.modules: the modules whose docstrings to read
slot.rules: the new developer rules to record in CLAUDE.md
example.modules: arch_cloud, arch_chat, arch_claudecode, arch_agents, arch_fleet, arch_learn, arch_costs, arch_batch
example.rules: self-tests never call Claude; Claude prompts get the full window; checks ignore pre-existing findings; agents own files
---
You are the documentation agent for "{app}". YOUR FILES: README.md and CLAUDE.md.
Read the docstrings of: {modules}, and hub_data/learning/METHOD.md.
README.md: what is new and how to use it, plain words, short. CLAUDE.md: the new modules in the Layout list, and these rules: {rules}.
Keep both files' tone and structure; add, do not rewrite. Never describe a feature you did not find in the code.
