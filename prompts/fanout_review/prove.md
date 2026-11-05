Below are review findings that a second reader confirmed. PROVE the ones marked "kind: bug" with failing tests; leave the others alone.

For each bug create ONE file tests/hunt/test_<short_name>.py. Its FIRST line must be a docstring: """BUG: <title> | <file>:<function> | <why, one sentence>"""
The test calls the real code with a concrete input and asserts the CORRECT behaviour, so it FAILS today because of the bug. It must not fail from an import, syntax or name error. Do not edit application code or existing tests. At most {n} tests, most severe first.
A test that passes today is deleted by the harness: the finding was not a bug.

CODE:
{context}

CONFIRMED FINDINGS:
{kept_findings}
