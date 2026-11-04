Add type hints to the public functions listed below. Annotate every parameter and the return value with the narrowest honest
type; use `X | None` only where None really occurs, and `-> None` for functions that return nothing. Add `from __future__ import annotations`
at the top of a file if a hint would otherwise need a forward reference. Do NOT change any behaviour, rename anything, or reformat
unrelated code. The code must still run on Python 3.10 and every test must still pass.

FUNCTIONS MISSING HINTS:
{findings}

CODE:
{context}

{feedback}
