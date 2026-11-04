Write a micro-benchmark for the code described below, in the NEW file {target}. Rules:
- import the code under test and call the operation that matters with REALISTIC, deterministic input (seed any randomness), big enough that one run takes at least 0.05 seconds;
- time it with time.perf_counter(); if the operation is destructive, rebuild the input inside the timed loop setup;
- print, as the LAST line of output and nothing after it, JSON like {{"seconds": 0.123}} (seconds per run);
- standard library only; no network; do not change application code.

WHAT TO MEASURE: {goal}
FILES: {files}

CODE:
{context}
