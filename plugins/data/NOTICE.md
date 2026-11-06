# NOTICE

This plugin is adapted from the **data** plugin (version 1.1.0) of Anthropic's `knowledge-work-plugins`
(https://github.com/anthropics/knowledge-work-plugins), Copyright Anthropic, licensed under the Apache License 2.0.
A copy of that license is in `LICENSE`.

## What was changed
- Each `skills/<name>/SKILL.md` became `skills/<name>.md` (Architect skill format: `name`, `description`, `when`) and was
  shortened to fit Architect's 6000-character skill limit. Frameworks, section headings, tables, checklists, thresholds
  (null-rate 5%/20%, completeness score bands, 3-level confidence assessment) and output formats were kept.
- Each skill also became a slash command (`commands/<name>.md`, same name and arguments) and a local agent workflow
  (`agents/*.yaml`, `workflows/<name>.yaml`) that splits the work across small local models, with JSON output schemas
  that the harness validates; each skill page states the original output format.
- MCP connectors (`~~data warehouse`, `~~notebook`, `~~product analytics`, `~~project tracker`) were replaced by local
  equivalents: files in the project (CSV/JSON/SQL) read with Architect's read-only tools, (profiling is done with Python's stdlib csv/sqlite3). Where nothing local exists the skills say "not available locally" (see `CONNECTORS.md`).
- `.mcp.json` and the `package_data_skill.py` script were not carried over. No agent uses a Claude model.
