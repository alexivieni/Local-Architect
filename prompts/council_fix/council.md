A smaller model tried to fix this bug and could not make the tests pass. You are one member of a council: several answers are written, each is applied on its own and run against the full test suite, and only answers that pass count. Among those, the smallest change is kept. Write the smallest correct fix.

Rules:
- Change application code only. The tests, conftest.py and the test settings files (pytest.ini, tox.ini, setup.cfg, pyproject.toml) are read-only; a change to them is refused.
- The code below is the ORIGINAL code: the earlier failed tries were undone.
- Find the cause first: read the failing test, follow the call into the code, and fix the line that is wrong, not the symptom.
- To change one function, send a DEF block (DEF: path.py::name or DEF: path.py::Class.method) with that whole definition.

BUG REPORT:
{goal}

CODE (most relevant first):
{context}

REPRODUCTION TEST ({repro_path}) CURRENTLY FAILS WITH:
{repro_failure}

WHAT THE LOCAL TRIES RAN INTO ({tries} tries; do not repeat their mistake):
{last_failure}
