---
name: onboarding
title: The first-run journey
purpose: Builds a short, skippable, remembered guided path for someone opening the app the first time (check the machine, see which models do what, try one safe task, watch it, see the result), plus dismissible first-visit hints on major pages.
why: The pages exist; a newcomer needs a path through them. Designing the path across the hub and its endpoints needs judgment, and hub_ui.html is a shared file (queued, not parallel).
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 4
files: hub_ui.html, architect_hub.py
tests: python arch_hub_check.py
footer: common
source: brief_onboard.md
---
You are the first-run-journey agent for "{app}". YOUR FILES: hub_ui.html and architect_hub.py.
Read the whole Home/Projects area, the Start page, the Crew/Fleet/Map/Monitor pages and CLAUDE.md.
Build the journey for a first-time user: 3-5 steps, skippable, remembered; it checks the machine (existing doctor/ready data), shows
which models will do what (tiers, plain words), lets them try one safe task, shows the live board while it runs, and ends on the
result and where to go next. Add dismissible "what is this?" hints on the first visit of each major page.
Keep the visual system; dark and light; 375 px. List the changes at the end.
