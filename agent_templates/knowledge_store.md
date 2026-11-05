---
name: knowledge_store
title: Experience store
purpose: Ingests everything the agents and models did into one local SQLite database, incrementally, and turns it into search, playbooks and statistics.
why: Work that is not kept cannot be learned from. Incremental ingest (remembered sizes and offsets) makes it cheap to run often; one database with full-text search answers "have we done something like this before, and what worked?"; a by_claude flag on every row lets retrieval use everything while training never sees Claude-made content.
mode: acceptEdits
model:
budget_usd: 3
files: {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_learn.md
slot.store_name: the store's name in the app's own voice
slot.module: the module's name, without .py
slot.suite: the self-test's short name
slot.sources: every folder and file format to ingest
slot.db: the database file under data_dir()
example.store_name: Logbook
example.module: arch_almanac
example.suite: almanac
example.sources: data_dir()/cc_runs/*.jsonl and *.meta.json, data_dir()/fleet/*.json, project .arch/runs/*/trace.jsonl
example.db: almanac.db
---
You are the learning agent for "{app}". YOUR FILES: NEW {module}.py and NEW arch_selftest_{suite}.py.
Read first (read only): arch_memory.py, arch_fts.py, arch_dataset.read_full(), arch_core.data_dir().
Build the "{store_name}":
1. ingest(extra_dirs=None): incremental (remember file sizes and offsets) from {sources}; into one SQLite db at
   data_dir()/{db}: tasks, steps (message | tool | result | edit | check) and an FTS5 table over prompts and step text.
2. similar(text, k=5) -> past tasks most like this one, with outcome, cost and the tool sequence that worked.
3. playbooks() -> a Markdown page per kind of task: what usually worked, average cost, success rate, common failures.
4. stats() -> counts, cost by source and model, success rate, top tools.
5. Every row keeps `source` and `by_claude`; exclude_from_training() -> the ids the training export must skip.
6. CLI: python {module}.py ingest | stats | similar "text" | playbooks.
arch_selftest_{suite}.py: a fixture per source; prove the second ingest adds nothing, similar() ranking, the playbooks file,
stats numbers and the by_claude flags.
