---
name: online_option
title: An optional cloud feature with a local helper
purpose: Adds a cloud capability behind a setting (default off) and, where it saves money, a free local step that shrinks what is sent.
why: Nothing remote is on the default path, so every online feature is a switch the owner turns on. A local model reading the project first and sending a short brief instead of the whole context is the cheapest token there is; when it fails or times out, the full path must still work, and the saving is recorded so it is visible.
mode: acceptEdits
model:
budget_usd: 3
files: {cloud_module}.py, {chat_module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_scout.md
slot.cloud_module: the module that calls the cloud, without .py
slot.chat_module: the module that builds the prompt, without .py
slot.suite: the self-test's short name
slot.feature: the cloud capability and its setting
slot.local_step: the local helper step and its setting
slot.record: what each call records so the saving shows
example.cloud_module: arch_cloud
example.chat_module: arch_chat
example.suite: scout
example.feature: web search for chat answers, online.web_search = off | on (default off)
example.local_step: the local worker writes a 600-word brief of the relevant code; online.local_scout = on, only above 6000 chars
example.record: searches used, scout_chars vs full_chars
---
You are the cloud-feature agent for "{app}". YOUR FILES: {cloud_module}.py, {chat_module}.py, NEW arch_selftest_{suite}.py.
Read both modules fully first.
1. {feature}. Errors from the provider's server-side tools arrive as result blocks, not exceptions: handle that.
2. {local_step}. Keep the full-context path when the local step fails or times out (60 s).
3. Record in the call record: {record}. Each new setting goes in arch_params.SETTINGS and arch_core.DEFAULTS.
arch_selftest_{suite}.py: with a fake cloud client and a fake local call, prove the feature is added only when on (and only for
models that support it), the local step replaces the big context, its failure falls back, and the numbers are recorded.
