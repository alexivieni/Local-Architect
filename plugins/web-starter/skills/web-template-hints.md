---
name: web-template-hints
description: How to start a small web page that stays maintainable
when: html, css, web page, template, landing, frontend, ui
files: *.html, *.css
---
# Starting a web page

- Define colours as CSS custom properties on `:root` and a dark set under `prefers-color-scheme: dark`.
- Use real elements: `button` for actions, `a` for navigation, `label` tied to every input.
- Lay out with flex or grid and test at 375 px wide with no horizontal scroll.
- Keep scripts and styles local; do not load anything from the internet in a page that must work offline.
