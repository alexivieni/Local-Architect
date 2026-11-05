---
name: template_library
title: Library of working briefs
purpose: Turns briefs that worked into reusable templates with a loader, a filler, a plan builder for the orchestrator, and a document explaining the method.
why: A brief that produced good work once is the best starting point for the next one. Keeping each as a file with a header (owned files, budget, mode, tests) and slots for what changes means a new job is a fill-in, and the plan builder refuses anything the orchestrator would refuse before a cent is spent.
mode: acceptEdits
model:
budget_usd: 3
files: {templates_dir}, {module}.py, arch_selftest_{suite}.py, docs/{doc}.md
tests: python arch_selftest_{suite}.py
footer: common
source: brief_library.md
slot.templates_dir: the new templates folder
slot.module: the library module, without .py
slot.suite: the self-test's short name
slot.doc: the method document under docs/, without .md
slot.material: the real briefs and notes to read first
slot.orchestrator: the module whose plan format and plan check the library must satisfy
example.templates_dir: agent_templates
example.module: arch_agentlib
example.suite: agentlib
example.doc: agent_method
example.material: hub_data/learning/METHOD.md, hub_data/learning/briefs/brief_*.md, briefs/launch.py
example.orchestrator: arch_fleet.py (plan_check, launch)
---
You are the library agent for "{app}". YOUR FILES: NEW folder {templates_dir}/, NEW {module}.py, NEW arch_selftest_{suite}.py,
NEW docs/{doc}.md.
Read the real material first: {material}, and {orchestrator}.
1. {templates_dir}/: one template per brief, each with a small header (name, purpose, files it owns as slots, default budget, model,
   mode, tests to run after) and slots for the parts that change; the general patterns too, and the shared footer on its own.
2. {module}.py: list_templates(), load(name), fill(name, **values) -> brief text, to_fleet_plan([...]) -> a plan the orchestrator
   accepts (checked by its own plan check), explain(name) -> plain words.
3. docs/{doc}.md: the method for the owner, with measured costs and lessons, and which parts are public vendor guidance and which
   are this project's own practice.
4. arch_selftest_{suite}.py: every template loads, fills without leftover slots, and becomes a clean plan.
