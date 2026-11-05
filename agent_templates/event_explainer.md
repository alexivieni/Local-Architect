---
name: event_explainer
title: Every event in one plain sentence
purpose: Builds explain_event, explain_run and a glossary that turn trace records into sentences a newcomer understands, from the records' own fields (no model calls).
why: The hub shows what the machine did; a newcomer needs to know why. Sentences built in code from fields are always true to the record. Covering every record type well needs a careful reader of many modules.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 3
files: arch_why.py, arch_selftest_why.py
tests: python arch_selftest_why.py
footer: common
source: brief_why.md
slot.records: the trace record types to cover
example.records: call, step, check, verdict, escalation, tool, todo
---
You are the explainability agent for "{app}". YOUR FILES: NEW arch_why.py, NEW arch_selftest_why.py.
Read CLAUDE.md, arch_flows.route, arch_progress.py, arch_cloud.py, arch_core.emit_event and the trace record types ({records}),
arch_receipt.py.
Build explain_event(ev) -> one plain sentence for any record; explain_run(run_dir) -> the run as 5-12 sentences in order;
glossary(term) for the app's words (tier, gate, not sure, escalation, council, crew, fast lane, fleet). Sentences come from the
record's fields only (no model calls); an unknown record gets an honest generic sentence, never a guess.
Tests over fixture records for every record type.
