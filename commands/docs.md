---
name: docs
description: Write the missing docstrings for a module, from what its code actually does
flow: document
args: a module, file or folder
---
Document {args}.

Write a docstring for every public module, class and function that has none: what it takes, what it returns, what it raises and when.
Write it from the code, not from the names. Change no behaviour and reformat nothing. The existing tests must still pass. If something
is too unclear to document honestly, say so in the report instead of guessing.
