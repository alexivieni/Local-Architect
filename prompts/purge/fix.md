These are findings from a scanner for FAKE CODE: code that looks finished but is not. Fix every one for real:
- stub / not implemented / constant stub / pretends to work: implement it properly from what the name, docstring, callers and tests tell you;
  if nothing calls it and its purpose is unclear, DELETE it instead of leaving a placeholder.
- test that asserts nothing / always-true assertion: add assertions that check real behaviour of the code under test. Do NOT delete a
  test that checks something real, and do not replace one placeholder with another.
- swallowed error: handle it (log with context, re-raise, or narrow the except) rather than ignoring it.
- placeholder text: replace with real content or remove it.
If a finding is a false positive here, leave it and say why in one line.

FINDINGS:
{findings}

CODE:
{context}

{feedback}
