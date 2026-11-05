---
name: setting_switch
title: A new behaviour behind a setting
purpose: Adds one setting (default off) that changes a named behaviour, with "off is byte-identical to today" proven by a test.
why: A setting must exist in both arch_params.SETTINGS and arch_core.DEFAULTS (a check compares them), and the hub draws it from the schema. Proving that off changes nothing means the owner can turn it on without fear and off to get back exactly what they had.
mode: acceptEdits
model:
budget_usd: 3
files: {module}.py, arch_core.py, arch_params.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_fastlane.md, brief_scout.md
slot.setting: the setting's key and values
slot.module: the module whose behaviour changes, without .py
slot.suite: the self-test's short name
slot.read_first: what to read first (CLAUDE.md sections, functions)
slot.when_on: exactly what changes when it is on
slot.never: what it must never do
example.setting: server.fast_lane = off | on (default off)
example.module: arch_server
example.suite: fastlane
example.read_first: CLAUDE.md notes on llama-server; arch_server.plan(), _ngl and the flag caps
example.when_on: the worker gets every layer and its cache on the card, n-gram drafting, above-normal priority
example.never: change Windows power plans or other system settings
---
You are the setting agent for "{app}". YOUR FILES: {module}.py, arch_core.py (DEFAULTS and the one place the behaviour is
chosen only), arch_params.py, NEW arch_selftest_{suite}.py.
Read first: {read_first}.
Add the setting {setting}: name, type, range and a plain description in arch_params.SETTINGS, the default in arch_core.DEFAULTS.
When it is on: {when_on}. It must never: {never}.
Anything the setting reports to the owner (plans, status) says in plain words what it changed.
arch_selftest_{suite}.py: with fake inputs (no GPU, no network), prove on vs off changes exactly the listed things and nothing
else, and that off is byte-identical to today's output.
