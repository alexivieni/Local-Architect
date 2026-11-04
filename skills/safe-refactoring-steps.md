---
name: safe-refactoring-steps
description: Restructure code in small steps that keep behaviour identical and tests green
when: refactor, refactoring, extract function, extract method, split module, move function, rename function, inline, dead code, without changing behaviour
---
# Refactoring safely

A refactor changes structure and nothing else. If behaviour changes, it is not a refactor.

1. **Tests first.** Run the suite before you touch anything and note the result. If the code you are changing has no tests,
   write a characterisation test that pins down what it does now, even the odd parts.
2. **One move per step.** Extract a function, or rename, or move, or inline. Not all four. Run the tests after each step.
3. **Keep old names working** when other code depends on them: leave a thin alias or wrapper that calls the new one, and
   remove it in a later change after `grep` shows no callers.
4. **Move code, do not rewrite it.** Copy the body exactly into its new home, then adjust. Rewriting from memory introduces bugs.
5. **Find every caller** with `grep` before renaming or changing a signature, including tests, strings and config files.
6. **Do not mix in fixes or features.** If you spot a bug, note it and fix it in a separate change with its own test.
7. **Delete dead code only after proving it is dead**: no callers, no dynamic lookup by name, no entry in config.

If a test fails mid-refactor, undo the last step rather than debugging forward.
