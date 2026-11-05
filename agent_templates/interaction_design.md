---
name: interaction_design
title: Interaction design pass
purpose: Makes the workflow easy to grasp by direct manipulation: drag tasks into the queue or a fleet, drag templates into plans, one-click next steps, a persistent action bar, keyboard alternatives.
why: Features only help when the next action is obvious. Direct manipulation plus a "most likely next step" button turns a set of pages into one workflow, and keyboard equivalents keep it accessible.
mode: acceptEdits
model:
budget_usd: 6
files: {ui_file}
tests: python arch_hub_check.py
footer: common
source: brief_interact.md
slot.ui_file: the hub's single UI file
example.ui_file: hub_ui.html
---
You are the interaction designer for "{app}". YOUR FILES: {ui_file}. Read the design system and art direction docs and the pages as
they are now. Add direct manipulation: drag a task onto the Queue or a Fleet lane; drag agent templates from the Library into a Fleet
plan; reorder the Queue by drag; one-click next steps after every result; a persistent bottom action bar on work pages (project, mode,
model, the one most likely next action); keyboard alternatives for every drag. Keep the design system; dark/light; 375 px. List every
change.
