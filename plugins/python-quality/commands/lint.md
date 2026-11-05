---
name: lint
description: Find the Python problems a linter would flag, and fix only those
flow: fix
args: a file or folder
---
Find the problems in {args} that pyflakes or a strict linter would flag: unused imports, undefined names, shadowed builtins,
mutable default arguments, bare except clauses. Fix only those, with the smallest change each, and keep every test passing.
