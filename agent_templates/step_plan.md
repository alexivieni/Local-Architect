---
name: step_plan
title: An ordered plan with commands
purpose: Turns a multi-step job the owner does by hand into one ordered plan: plain words, the command for each step, what each needs, and a dry run.
why: A long loop (export, train, convert, compare, promote) fails at the step whose need was not met. Saying what each step needs (a free GPU, disk, a time estimate from measured numbers) and ending with "only on no regression" makes the loop safe to follow; a dry run shows what would happen without a GPU.
mode: acceptEdits
model:
budget_usd: 3
files: {modules}, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_tune.md
slot.modules: the files this agent owns (comma-separated)
slot.suite: the self-test's short name
slot.job: the job, in plain words
slot.steps: the steps in order
slot.guard: the condition that must hold before the last step
slot.never: data or actions that must never enter the loop
example.modules: arch_autotune.py, train_lora.py
example.suite: tune2
example.job: improve my worker model from my own verified work
example.steps: export the dataset, train a LoRA, convert to GGUF, eval --compare against the baseline, promote-model
example.guard: no regression in the comparison
example.never: samples written by Claude (transport claude or by_claude)
---
You are the planning agent for "{app}". YOUR FILES: {modules}, NEW arch_selftest_{suite}.py.
Read CLAUDE.md first, then your files.
The job: {job}.
1. A single plan function -> ordered steps, each with plain words, the command, and what it needs (GPU free, disk space, a time
   estimate from measured numbers): {steps}. The last step only when: {guard}.
2. A dry-run mode that prints what each step would do and on what data, without a GPU.
3. Never include: {never}.
4. A summary the hub can show: the best settings this machine has measured for the job.
arch_selftest_{suite}.py: fixtures proving the order and needs, the filtering in a dry run, and the summary shape.
