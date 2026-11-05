---
name: monitor
title: Monitor
purpose: Watches logs or run records incrementally and turns them into a current picture, a timeline and plain-language lessons.
why: Reading the real files on disk before coding keeps the parser honest about the formats; remembering offsets makes a rescan cheap enough to run every 30 seconds; and flagging content written by Claude keeps it out of weight training while still using it for harness learning.
mode: acceptEdits
model:
budget_usd: 3
files: {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_overseer.md
slot.what: what is being watched, in a few words
slot.module: the new module's name, without .py
slot.suite: the self-test's short name
slot.sources: the files and folders to read, with their line formats
slot.signals: what to pull out of them (status, cost, stalls, conflicts, refusals ...)
slot.out_dir: where the state and the timeline go
example.what: every agent running on this project
example.module: arch_watchtower
example.suite: watchtower
example.sources: hub_data/learning/agent_logs/*.log (lines "[type] text", a final "=== STATUS" line)
example.signals: status, cost, turns, tool calls by name, files edited, stalls over 120 s, two live agents on one file
example.out_dir: hub_data/learning/watchtower
---
You are the monitoring agent for "{app}". YOUR FILES: NEW {module}.py, NEW arch_selftest_{suite}.py.
It watches {what}. Sources (read their formats from real files on disk before coding): {sources}.
1. scan(), incremental (remember offsets): per agent or session pull out {signals}.
2. Write {out_dir}/state.json (the current picture, written atomically) and append one line per new observation to
   {out_dir}/timeline.jsonl (t, agent, kind, detail).
3. summary() -> agents, totals, conflicts, stalls, top tools, busiest files.
4. lessons() -> plain-language findings from the data, one dated line each.
5. CLI: python {module}.py watch | summary | lessons.
Anything written by Claude is flagged by_claude so it never enters weight training.
arch_selftest_{suite}.py: fixtures for each source; prove the incremental scan (a second scan adds nothing), each signal, and the
summary and lessons shapes.
