---
name: smallest-change
description: Prefer the smallest change that solves the stated problem
when: refactor, clean up, tidy, simplify, improve, restructure, rewrite
---
# The smallest change that works

Scope creep is the most common way a good change becomes an unmergeable one.

- Solve the problem that was stated. If you notice three other problems, say so at the end; do not fix them in the same change.
- Keep the public shape of a function unless changing it is the task. Callers depend on names, argument order and return types.
- Do not rename things on your way past. A rename touches every caller and hides the real change in the noise.
- Do not add a dependency. This project runs offline on the standard library; a new import may simply not be there.
- Do not delete a test to make a suite pass. If a test is genuinely wrong, say which one and why.

A reviewer should be able to read your diff and say what it does in one sentence. If they cannot, it is too big.
