Hunt for REAL bugs in the code below, then PROVE each one with a failing test.
For every suspected defect create ONE file tests/hunt/test_<short_name>.py. Its FIRST line must be a docstring: """BUG: <title> | <file>:<function> | <why, one sentence>"""
The test calls the real code with a concrete input and asserts the CORRECT behaviour, so it FAILS today because of the bug. It must not fail from import, syntax or name errors. Do not edit application code. At most {n} findings, most severe first. If you find nothing provable, write nothing and say so.

Look for: boundaries and off-by-one, empty/None/huge input, wrong error handling, state shared between calls, unvalidated input, resource leaks, time/timezone/rounding mistakes, concurrency, injection.

{hints}CODE:
{context}
