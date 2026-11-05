---
name: clean-python
description: Python habits that keep small changes correct
when: python, lint, pyflakes, unused import, mutable default, except
files: *.py
---
# Clean Python

- No mutable default arguments: use `None` and create the list or dict inside the function.
- Never write a bare `except:`; catch the exception you expect and let the rest propagate.
- Open files with `with` so they are closed on every path, and pass `encoding="utf-8"` explicitly.
- Remove an import only after grepping that nothing else in the module uses it.
