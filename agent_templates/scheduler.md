---
name: scheduler
title: Scheduled runs
purpose: Adds timed, repeatable jobs to an existing module: add, list, remove, a tick that starts what is due exactly once per slot, a background thread and a run log.
why: Overnight work is only useful if it runs once per slot, never twice, and leaves a record the morning view can read. A `starter=` parameter lets the test drive tick() without starting anything real; self-tests never start real runs.
mode: acceptEdits
model:
budget_usd: 2
files: {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_crew.md
slot.module: the existing module the schedule goes into, without .py
slot.suite: the self-test's short name
slot.job: what one scheduled job starts
slot.fields: what a schedule entry holds
slot.store: the file the schedule is kept in, under data_dir()
example.module: arch_widget
example.suite: widget_schedule
example.job: a session of one of the person's agents with a fixed message
example.fields: agent_id, message, at (HH:MM), repeat (daily or once), last_run, last_status
example.store: widget_schedule.json
---
You are the scheduling agent for "{app}". YOUR FILES: {module}.py and NEW arch_selftest_{suite}.py.
Read {module}.py fully first. Add scheduled runs; one job starts {job}.
- schedule_add(...) -> id, schedule_list() -> entries ({fields}), schedule_remove(id).
- tick(now=None, starter=None) -> started ids: every entry whose time has come and that has not run for that slot yet; once ->
  removed after it starts, daily -> runs again the next day. Stored in data_dir()/{store}, written atomically (temp + os.replace).
- start_scheduler(): a daemon thread calling tick() every 60 s; a second call does nothing.
- A run log line (final status and a short summary) appended to a .jsonl in data_dir() when a started job ends.
Validate inputs (ids, HH:MM, repeat values). Never start real runs during self-tests except through an injected starter.
arch_selftest_{suite}.py: add/list/remove, a due job runs once (not twice in the same slot), daily re-arms, once is removed,
bad inputs refused, the log line written -- with a fake starter, no network.
