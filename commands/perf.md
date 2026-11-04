---
name: perf
description: Find the slowest path with a measurement first
flow: optimize
args: the operation or file that feels slow
---
Find what makes {args} slow.

Measure before you change anything: reproduce it with a realistic input, time it several times, and profile it. Report the top few
functions by time with their numbers. Then change the single biggest cost, measure again the same way, and keep the change only if it is
clearly faster. The tests must still pass unchanged. Report before and after numbers. Do not optimise code the profile did not point at.
