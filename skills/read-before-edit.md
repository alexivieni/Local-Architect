---
name: read-before-edit
description: Find the exact lines before changing anything, especially in big files; never guess the text to replace
when: edit, change, rename, label, replace, fix, update, big file, html, ui, hub_ui
files: *.html, *.py, *.js, *.css
---
# Read before you edit

Seen today: a strong model was shown the first 2% of a 245 KB file, could not see the line it was asked to change, and rightly
refused to guess. After the harness started showing the lines the request points at, the same model made the exact one-line edit on
its first try.

- **Search first.** Grep for a rare word from the request (an id, a value, a label) and read 5-10 lines around each hit.
- **Copy the search text exactly** from what you read, character for character. A SEARCH that does not match is a wasted try.
- **Outline big files** (function and section names) instead of reading them whole; agents that re-read the same big file 11-14
  times wasted a third of their turns.
- **Say when you cannot see it.** If the line is not in what you were shown, ask for it or search; do not invent it.
- **Change only what was asked.** Problems that were already in the file are not yours to fix in this step.
