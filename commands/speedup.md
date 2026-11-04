---
name: speedup
description: Measure what is slow, make it faster, and prove it
flow: optimize
args: a file, function, or an operation that feels slow
---
Make {args} faster.

Measure first and write the number down. Then change one thing. Then measure again the same way. If it is not faster, put it back.
Do not trade correctness for speed: the tests must still pass, unchanged. Report the before and after numbers and what you changed,
and say what you did NOT do because the gain was not worth the complexity.
