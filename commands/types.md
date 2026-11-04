---
name: types
description: Add precise type hints to public functions without changing behaviour
flow: types
args: a module, file or folder
---
Add type hints to {args}.

Annotate every parameter and the return value of each public function with the narrowest honest type. Use `X | None` only where None
really occurs. Do not change behaviour, rename anything, or touch private helpers unless they need it for the public ones to type-check.
Keep it working on Python 3.10, and keep every test passing.
