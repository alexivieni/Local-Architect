# NOTICE

This folder is a local adaptation of the "design" plugin (version 1.2.0) published by Anthropic under the
Apache License, Version 2.0 (see LICENSE).

## What changed (modifications, per Apache-2.0 section 4)

- Each SKILL.md became a skill page in `skills/` plus a slash command in `commands/` with the same name and arguments.
  The frameworks and output formats (sections, severity tables, spec tables) are kept as written.
- Connector steps (Figma, project tracker, user feedback, knowledge base, analytics) are replaced by local sources:
  files in the project, screenshots, `hub_ui.html`, or "not available locally".
- Prompts are shortened for small local models; agents (`agents/`) and workflows (`workflows/`) are new and use local tiers only.
- `tools/contrast.py` is new (WCAG contrast arithmetic in stdlib Python).

The original is not redistributed here. All credit for the frameworks belongs to its authors.
