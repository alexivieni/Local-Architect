---
name: hub_builder
title: Hub pages
purpose: Builds new pages in the one-file hub and the endpoints behind them, calling backend functions other agents fixed by name.
why: The hub is one HTML file, so only one page builder may hold it at a time; others queue after it. Calling other agents' functions by exact names agreed up front, each guarded so the hub still starts if one is missing, lets the pages be built while the backend is still being written.
mode: acceptEdits
model:
budget_usd: 5
files: {ui_file}, {server_file}
tests: python arch_hub_check.py
footer: common
source: brief_ui.md, brief_viz.md
slot.ui_file: the hub's HTML file
slot.server_file: the hub's server module
slot.read_first: the parts of the UI to read before adding anything (helpers, page registry, CSS tokens)
slot.earlier: what earlier agents built that these pages must match
slot.pages: the pages to build, in order
slot.backend: backend functions to call, by exact name, with their return shapes
example.ui_file: hub_ui.html
example.server_file: architect_hub.py
example.read_first: TABS/PAGES/render(), the h()/api()/go() helpers, the CSS tokens in :root
example.earlier: Council, Crew, Blueprints and a UX pass
example.pages: 1. a Fleet page (plan table, check, launch, watch) 2. a Logbook page (stats, search, playbooks)
example.backend: arch_fleet.plan_check/launch/status, arch_learn.stats/similar/playbooks
---
You are the UI agent for "{app}". YOUR FILES: {ui_file} and {server_file} only.
Read first in {ui_file}: {read_first}. Earlier agents built {earlier}: read their code and keep their style.
Build, in this order (each must work in dark and light themes and at 375 px width):
{pages}
Call these by their exact names (other agents may still be writing them); guard every call with try/except so the hub still
starts if one is missing: {backend}. Add the endpoints in {server_file}.
Keep the existing visual system; no external libraries. List what you built at the end.
