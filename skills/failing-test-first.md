---
name: failing-test-first
description: Prove the bug exists with a test that fails, then fix it
when: bug, broken, fails, crash, wrong result, regression, does not work, error
---
# Make it fail first

A fix you cannot prove is a guess.

1. Write the smallest test that reproduces the report. Run it. It must FAIL, and it must fail for the reason in the report,
   not because of a typo, a missing import, or the wrong path. A test that errors out before reaching your code proves nothing.
2. Now fix the code. Change as little as possible.
3. Run the test again. It must pass.
4. Run the rest of the suite. A fix that breaks two other tests is not a fix.

If you cannot make it fail, say so plainly instead of changing code hopefully. "I could not reproduce this, here is what I
tried" is a useful answer. A silent change to code that was not broken is not.

Name the test after the behaviour, not the bug number: `test_allow_rejects_after_limit`, not `test_fix_1234`.
