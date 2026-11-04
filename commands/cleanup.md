---
name: cleanup
description: Remove dead code and duplication without changing behaviour
flow: refactor
args: a file or folder
---
Clean up {args} without changing what it does.

Remove code nothing calls, collapse duplication that is genuinely the same thing, and give unclear names better ones. Keep every public
signature unless changing it is the point. The tests must pass before and after, unchanged: if a test needs editing to keep passing, you
have changed behaviour, so stop and say so instead.
