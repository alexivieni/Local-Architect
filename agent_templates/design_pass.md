---
name: design_pass
title: One visual system for the whole hub
purpose: A design pass over hub_ui.html (tokens, components, icons, themes, accessibility or flagship polish) that keeps every JS behaviour and records the system in one document.
why: Many agents added pages; only a pass over the whole file makes it one product. hub_ui.html is one large shared file (queue it, never run two passes in parallel), and taste plus accessibility rules need a strong model.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 4
files: hub_ui.html, docs/{doc}.md
tests: python arch_hub_check.py
footer: common
source: brief_design.md, brief_art.md, brief_polish.md
slot.role: the designer's role
slot.doc: the document under docs/ that records the system, without .md
slot.focus: what this pass does
example.role: design-system and accessibility agent
example.doc: design_system
example.focus: one documented token set (colour, type, spacing, radius, elevation, motion), a hidden Components page in both themes, keyboard reach, focus order, aria labels, live regions, reduced motion
---
You are the {role} for "{app}". YOUR FILES: hub_ui.html, NEW docs/{doc}.md.
Build on what earlier design passes did; do not fight them. This pass: {focus}.
Every colour from tokens with AA contrast in dark and light; works at 375 px; honours prefers-reduced-motion; no external images or
downloads. Keep every JS behaviour. docs/{doc}.md records the system. List every change by page.
