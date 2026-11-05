---
name: harness_pack
title: Compose a harness pack from existing parts
purpose: Builds a composer that lists every part (tiers, flows, step kinds, gates, tools, Claude modes, templates, budgets) and turns a spec into a validated pack folder with its flow, settings overlay, README and run command.
why: Every part already exists; a pack only chooses and validates. Getting the validation right for flows and settings overlays, and writing sensible example packs, needs a strong model.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 4
files: arch_harness.py, arch_selftest_harness.py, harnesses
tests: python arch_selftest_harness.py
footer: common
source: brief_harness.md
slot.examples: the example packs to ship
example.examples: local-only fixer; Claude-plans local-builds feature team; overnight trace factory
---
You are the harness-builder agent for "{app}". YOUR FILES: NEW arch_harness.py, NEW arch_selftest_harness.py, NEW harnesses/ folder.
Read CLAUDE.md, arch_roles.py, arch_gates.py (GATES), arch_tools.py (TOOLS), arch_dataflow.py, arch_cloud.py (modes),
arch_agentlib.py and agent_templates/, arch_fleet.py.
parts() -> every component with a one-line purpose. compose(spec) -> harnesses/<name>/ with harness.yaml (the spec), a validated flow
YAML, a settings overlay (only keys from arch_params, validated), README.md (what it does and why each part) and a run command.
check(pack) validates all of it; run(pack, goal) applies the overlay for one run only. Ship these example packs: {examples}.
Tests: each example validates; bad parts are refused with a plain reason.
