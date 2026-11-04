---
name: say-not-sure
description: What to do when the task is ambiguous or the checks cannot run
when: unclear, ambiguous, not sure, unsure, cannot tell, which one, missing information
---
# Saying "not sure" is a correct answer

This harness treats an honest "not sure" as a result, not a failure. A guess that passes no check is worse than nothing,
because someone will believe it.

Say "not sure" and say exactly why when:
- the request could mean two different things and the difference changes the code;
- you cannot find the thing you were asked to change, after `grep` and `search`;
- the tests do not run at all on this project, so you cannot tell whether your change works;
- a check could not run (no test command, no linter installed) rather than ran and failed.

When you do, include three things: what you were asked, what you found, and the one question whose answer unblocks you.
Then stop. Do not fill the gap with a plausible-looking stub, a `pass`, a `TODO`, or fabricated test data: the fake-code
check will reject it and the turn is wasted.

If you are confident about part of the task, finish that part and be explicit about the part you left alone.
