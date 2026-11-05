---
name: migration
title: Migration
purpose: Moves one piece of the app from its old shape to a new one while keeping its behaviour, with a test that proves old and new agree.
why: A migration is judged by "nothing changed that should not have", so the brief asks for an equivalence test first and a way back (the old path kept behind a switch, or an undo). It owns both the old and the new files so no one else edits either half mid-move.
mode: acceptEdits
model:
budget_usd: 3
files: {old_file}, {new_file}, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: CLAUDE.md conventions (flows moving from Python classes to flows/*.yaml with flows.engine legacy|data|off)
slot.what: what is being moved
slot.old_file: where it lives now
slot.new_file: where it goes
slot.suite: the self-test's short name
slot.keep: behaviour that must stay exactly the same
slot.switch: how the old path stays reachable (a setting, a fallback) until the new one is proven
example.what: the bugfix flow's read-only test rule
example.old_file: arch_flows.py
example.new_file: flows/bugfix.yaml
example.suite: bugfix_yaml
example.keep: existing tests stay read-only; the reproduction must be a new file
example.switch: flows.engine = legacy keeps the Python class in charge
---
You are the migration agent for "{app}". YOUR FILES: {old_file}, {new_file}, NEW arch_selftest_{suite}.py.
Move {what} from {old_file} to {new_file}.
Must stay exactly the same: {keep}.
Until the new path is proven, the old one stays reachable: {switch}.
Steps: read the old code and every caller first; write the equivalence test (same inputs through old and new, same results,
including the "could not run" outcome); then move the code; then point callers at the new place only where the switch says so.
arch_selftest_{suite}.py proves old and new agree on ordinary, empty and failing inputs, and that the switch picks the path.
