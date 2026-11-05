---
name: lead-and-workers
description: How to split a large change into parallel agent jobs that do not collide, and how to check their work
when: split, parallel, agents, fleet, orchestrate, delegate, plan the work, many files, big change, workers, subagents
files: *.py, *.yaml, *.md, flows/*, agent_templates/*
---
# Lead and workers

Learned from 44 agents that built this app in one day (hub_data/learning/METHOD.md, ledger.jsonl).

- **One owner per file.** Give every job the exact files it may write. Two jobs never write the same file; work on one shared file
  goes in a queue (UI build, then UX review, then visual pass). Collisions were zero all day because of this rule.
- **Fix the interfaces first.** When job A calls code job B is writing, name the function and its arguments in BOTH briefs before
  either starts (e.g. `arch_costs.summary(days=7) -> {...}`).
- **Say what the worker cannot do.** Workers that could not run commands tried 65 times anyway. The brief says: "you cannot run
  commands; the lead runs the tests."
- **Workers edit, the lead verifies.** Every job ends with a report (what existed, what changed, the test command). The lead runs the
  tests and keeps only what passes. One worker overwrote an older test suite; verification caught it.
- **Cap every job.** A spend or step budget per job; real use was 10-45% of the cap.
- **Smallest capable worker.** Mechanical jobs (wiring tests, docs) go to the small model; judgment (design, review, architecture)
  to the strongest. Locally: the 4B worker for mechanical steps, the heavy tier or a council for judgment.
