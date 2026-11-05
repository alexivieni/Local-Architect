---
name: secret_scrub
title: Scrub secrets before anything is stored
purpose: Builds a scrubber that replaces keys, tokens and passwords with a marker before logs and transcripts are kept or learned from, with tests for every kind and for no false positives.
why: Logs are copied into the learning folder and may feed retrieval; a key that lands there leaks. Getting both directions right (catch every secret, leave hashes and UUIDs alone) takes care and judgment.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 3
files: arch_redact.py, arch_selftest_redact.py, hub_data/learning/keeper.py
tests: python arch_selftest_redact.py
footer: common
source: brief_redact.md
---
You are the privacy agent for "{app}". YOUR FILES: NEW arch_redact.py, NEW arch_selftest_redact.py, hub_data/learning/keeper.py.
arch_redact.scrub(text) replaces each secret with [REDACTED:kind]: provider API keys, GitHub tokens, AWS keys, bearer tokens, private
key blocks, passwords in URLs, .env-style KEY=secret lines, long high-entropy strings next to words like key/token/secret.
scrub_file(path) in place with a .bak only when changed; scrub_tree(dir, patterns) for logs and transcripts. keeper.py scrubs every
copied log on the way in. The module's __main__ does a one-time scrub of the learning folders, dry-run by default.
Tests: every kind caught; normal code untouched (hashes in tests, UUIDs, base64 of normal length); scrubbing twice changes nothing.
