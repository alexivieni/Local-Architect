Fix this bug with the smallest correct change. Change application code only: the tests, conftest.py and the test settings files (pytest.ini, tox.ini, setup.cfg, pyproject.toml) are read-only in this step, and a change to them is refused. To change one function, send a DEF block (DEF: path.py::name or DEF: path.py::Class.method) with that whole definition.

BUG REPORT:
{goal}

CODE (most relevant first):
{context}

REPRODUCTION TEST ({repro_path}) CURRENTLY FAILS WITH:
{repro_failure}

{feedback}
