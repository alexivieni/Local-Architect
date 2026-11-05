---
name: api_review
title: API review plus helper
purpose: Reads official API pages, compares them with how the app calls the API today, writes the review, and builds one small helper module the review calls for.
why: Research alone tends to stay on paper. Pairing the review with one small, tested helper (lazy SDK import, refused when cloud use is off, an offline fallback) gives the owner something usable the same night, while changes to files other agents own stay proposals in the document.
mode: acceptEdits
model:
budget_usd: 3
files: docs/{doc}.md, {module}.py, arch_selftest_{suite}.py
tests: python arch_selftest_{suite}.py
footer: common
source: brief_apidocs.md
slot.doc: the review's file name under docs/, without .md
slot.pages: the official pages to read
slot.app_side: the app's files that call this API today
slot.cover: topics the review must cover
slot.module: the helper module's name, without .py
slot.suite: the self-test's short name
slot.helper: what the helper does (functions and arguments)
slot.hands_off: files another agent is editing: propose changes for them in the review instead
example.doc: claude_api_review
example.pages: https://platform.claude.com/docs/en/build-with-claude/token-counting
example.app_side: arch_cloud.py, arch_chat.py, arch_claudecode.py
example.cover: cache minimums per model, token counting before a call
example.module: arch_tokens
example.suite: tokens
example.helper: count(model, system, messages) via the official SDK; estimate_cost(...); a chars/3.5 fallback offline
example.hands_off: arch_cloud.py
---
You are the API-research agent for "{app}". YOUR FILES: NEW docs/{doc}.md, NEW {module}.py, NEW arch_selftest_{suite}.py.
Read these pages with your web fetch tool (if fetching is refused, say so and work from your own knowledge, marking it as such):
{pages}.
Then read how the app calls the API today: {app_side}.
1. docs/{doc}.md: for each page, what it offers, what the app already does, what it should do next, the expected saving with
   numbers, and risks. Cover at least: {cover}.
2. {module}.py: {helper}. Lazy-import the SDK; refuse when arch_cloud.allowed() says no; work offline with a local estimate.
3. arch_selftest_{suite}.py: a fake client; prove each function and the offline fallback. Self-tests never reach a paid model.
Do not edit {hands_off}; propose the exact changes for it in the review instead.
