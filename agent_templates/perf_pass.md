---
name: perf_pass
title: Performance pass
purpose: Makes one named path faster, proves the result is unchanged, and reports the measured before and after.
why: Speed claims without numbers are guesses. The brief asks for a timing harness the fleet can run (the agent cannot), for "off is byte-identical to today" where a switch is added, and for the evidence tier of every figure: measured here beats vendor card beats community.
mode: acceptEdits
model:
budget_usd: 3
files: {target}, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_fastlane.md, CLAUDE.md (measured notes on speculative decoding, threads, make room)
slot.path: what is slow, in plain words
slot.target: the file the change goes in
slot.suite: the self-test's short name
slot.baseline: the number measured today (and where it came from)
slot.limits: what must not change (results, settings, other files, system settings)
example.path: building the prompt context for a big project
example.target: arch_widget.py
example.suite: widget_speed
example.baseline: 1.9 s per call for a 400-file project (measured here, 2026-11-04)
example.limits: the same context text, byte for byte; no new setting turned on by default
---
You are the performance agent for "{app}". YOUR FILES: {target}, NEW arch_selftest_{suite}.py.
Make this faster: {path}. Today: {baseline}.
Must not change: {limits}. Never change system settings (power plans, drivers, the person's WSL/Docker) to win speed.
1. Read the path end to end; name where the time goes before changing anything.
2. Change {target}. If the change is a switch, off must be byte-identical to today's behaviour.
3. arch_selftest_{suite}.py: prove the result is unchanged on fixed inputs, and include a small timing run that prints before and
   after (the fleet runs it; you cannot). Report each figure with its evidence tier.
