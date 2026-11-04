---
name: read-before-writing
description: Look at the real code and its callers before changing anything
when: change, edit, fix, modify, update, refactor, rename, add to
---
# Read before writing

Before you change a file, read it. Before you change a function, find who calls it.

1. `outline` the file first. It is cheaper than reading the whole thing and tells you where the function lives.
2. `read` only the part you are about to change, plus roughly 20 lines either side.
3. `grep` the function's name across the project. Every caller is a place your change can break.
4. If a name appears in a test, read that test. It tells you what the function is promising.

Why this matters more on a small model: you cannot hold a whole file in your head, and a change written from memory
of what the file "probably" looks like will not apply. An EDIT block that does not match the file exactly is rejected,
so guessing costs you a whole turn.

Do not reformat code you were not asked to change. A diff that touches 200 lines to fix one is a diff nobody can review.
