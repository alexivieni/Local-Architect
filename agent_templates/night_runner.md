---
name: night_runner
title: One unattended night of local work
purpose: Writes a script that runs the local evals, exports the training data, fills the logbook and writes a morning summary, within a time limit and a stop file, plus a launcher and scheduling notes.
why: The local models cost nothing overnight, so the night is when verified traces are made; the script must be safe unattended (one GPU job at a time, a stop file, no Claude unless asked). Ordering the steps and their failure handling needs judgment.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 3
files: tools/overnight.py, tools/OVERNIGHT.md, tools/overnight.bat
tests:
footer: common
source: brief_overnight.md
slot.steps: what the night does, in order
example.steps: run the local eval families one at a time; export the dataset with dedup and a quality filter; ingest into the logbook and write playbooks; overseer lessons; copy cold results to the archive drive if storage.archive is set; write the morning summary
---
You are the overnight-workload agent for "{app}". YOUR FILES: NEW tools/overnight.py, tools/OVERNIGHT.md, tools/overnight.bat.
Read CLAUDE.md, arch_supervisor.py, arch_eval.py, arch_dataset.py, arch_learn.py, arch_overseer.py and arch_placement.py if present.
tools/overnight.py: one unattended night with a time limit and a stop file, at below-normal priority, one GPU job at a time:
{steps}. The morning summary says what ran, pass rates, new training samples, cost ($0 for local) and problems. No Claude calls
unless --with-claude is given. overnight.bat uses the Python that has pytest (start.py's python_for_tests). OVERNIGHT.md explains
how to schedule it with Windows Task Scheduler without changing system settings itself.
