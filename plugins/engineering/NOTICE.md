# NOTICE

This plugin is adapted from the **engineering** plugin (version 1.2.0) of Anthropic's `knowledge-work-plugins`
(https://github.com/anthropics/knowledge-work-plugins), Copyright Anthropic, licensed under the Apache License 2.0.
A copy of that license is in `LICENSE`. This adaptation is distributed under the same license.

## What was changed
- Each `skills/<name>/SKILL.md` became `skills/<name>.md` (Architect skill format: `name`, `description`, `when`) and was
  rewritten shorter for small local models: explicit numbered steps, an exact output template, and "report only what you saw in
  the files" rules. Frameworks, section headings, tables, checklists and formulas were kept: the ADR format, the code-review
  tables and verdicts, the debug report, the deploy checklist, SEV1-4 table, status update and postmortem with 5 Whys, the
  standup format, the tech-debt categories and `Priority = (Impact + Risk) x (6 - Effort)`, the testing pyramid.
- Each skill also became a slash command (`commands/<name>.md`, same name and arguments). `standup`, `deploy-checklist` and
  `tech-debt` take an optional argument that the command text does not forward.
- Workflows (`agents/eng-*.yaml`, `workflows/eng-*.yaml`) split the work across small local models (code review: parallel
  correctness / security / performance / maintainability lenses, deduplicate, verify, write; debug; tech debt; and the rest).
  `tools/generate.py` regenerates them; `schemas.json` holds the JSON output schemas.
- Connectors (`~~source control`, `~~project tracker`, `~~knowledge base`, `~~monitoring`, `~~CI/CD`, `~~chat`, `~~incident
  management`) were replaced by local equivalents (`git log`, `git diff`, project files, running the project's tests). Where nothing
  local exists the skills say "not available locally". `.mcp.json` was not carried over. No agent uses a Claude model.
