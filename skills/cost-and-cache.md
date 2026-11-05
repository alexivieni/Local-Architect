---
name: cost-and-cache
description: Keep prompts cache-friendly and spend the least model time for a verified result
when: cache, caching, cost, tokens, prompt, budget, cheaper, faster, model choice, escalate, claude, effort, context size
files: arch_core.py, arch_cloud.py, arch_chat.py, prompts/*, flows/*
---
# Cost and cache

Measured over today's 44 agents: about 107 million input tokens were served from cache against a few thousand uncached ones. The
cache is what made the work affordable, and it only works when the start of every prompt stays byte-identical.

- **Stable first, variable last.** Instructions and project context at the start; the per-try feedback, failure text and the
  question at the end. One changed byte early in the prompt makes everything after it full price.
- **Share one prefix across a team.** Several agents answering the same request share the same system prompt and context; only the
  role line at the end differs. The first call writes the cache, the rest read it at about a tenth of the price.
- **Send less.** Context budgets must be real (outlines count). A local scout's 600-word brief beats 16k tokens of files.
- **Cheapest step that is verified.** Local first; escalate on evidence (a stalled step), not by default. The tests decide, so a
  cheaper model that passes the tests is the right model.
- **Effort per job.** Low for chat and routing, medium for writing code, high only for planning and stuck steps.
- **Reuse answers.** The same question at temperature 0 is answered from the local reply cache for free.
