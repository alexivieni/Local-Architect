---
name: measuring-performance
description: Measure before and after any speed change, with a method that gives honest numbers
when: slow, faster, performance, profile, profiler, cprofile, benchmark, timeit, latency, bottleneck, too much memory, speed up
---
# Measuring performance

An optimisation without a measurement is a guess, and most guesses about speed are wrong.

1. **Reproduce the slow case** with a realistic input size. Write the command down so you can run it again exactly.
2. **Measure the baseline.** Run it several times and take the median. `python -m timeit` for a small function,
   `time.perf_counter()` around a block, never `time.time()`.
3. **Profile to find where time goes.** `python -m cProfile -s cumtime script.py` and read the top 10 lines. Optimise the
   function at the top, not the one you suspected.
4. **Change one thing**, then measure again the same way. If it is not clearly faster, revert it.
5. **Keep the tests passing, unchanged.** Faster and wrong is a regression.

Common real causes, in order: work repeated inside a loop, a query per item (N+1), reading a file or calling the network
repeatedly, a list used for membership tests instead of a set, quadratic string building, loading everything into memory.

Report: the input size, the before and after numbers, the machine they ran on, and what you changed. Say when the gain is
too small to justify extra complexity, and leave that code alone.
