---
name: python-import-traps
description: The import mistakes that waste a whole turn in Python projects
when: import, module, package, circular, ModuleNotFoundError, ImportError, conftest, sys.path
files: *.py, tests/*, conftest.py
---
# Python imports

Most "it works for me" failures in Python are import failures.

- **Tests cannot find the package.** If `import myapp` fails inside `tests/`, the project needs a `conftest.py` at its root
  (even an empty one) or a `pyproject.toml` that makes it installable. Add the `conftest.py`; it is one line of nothing.
- **Circular imports.** If A imports B at module level and B imports A, one of them must move its import inside the function
  that uses it. Do that rather than restructuring both modules.
- **A name that shadows a standard module.** A file called `json.py`, `types.py` or `queue.py` next to your code will be
  imported instead of the real one. Check for this before debugging anything stranger.
- **Relative vs absolute.** Inside a package use the style the rest of the package already uses. Mixing them breaks when the
  entry point changes.
- Add the import you used. A function that calls `os.path.join` in a file with no `import os` fails at the first call, and a
  missing import is the single most common reason a generated file does not run.
