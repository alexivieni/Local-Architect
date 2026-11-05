# __NAME__

A defensive scanner for code you own. It reads files; it never runs them and never connects anywhere.

    python -m __PKG__ samples                 # text report, exit 1 if anything new is found
    python -m __PKG__ . --json > findings.json
    python -m __PKG__ . --write-baseline .scan-baseline.json   # accept today's findings
    python -m __PKG__ . --baseline .scan-baseline.json         # CI: fail only on NEW findings
    python -m pytest -q

## Rules

| id | what |
|----|------|
| SEC001 | known key shapes (AWS access key id, GitHub token, private key block, Slack token) |
| SEC002 | `password = "..."` style assignments with a literal |
| SEC003 | high-entropy string literal assigned to a name that sounds secret |
| PY001 | `eval` / `exec` |
| PY002 | `subprocess.*(..., shell=True)` / `os.system` |
| PY003 | `pickle.load(s)` / `yaml.load` without SafeLoader |
| PY004 | `hashlib.md5` / `sha1` |
| PY005 | `verify=False` |

Add a rule: a regex in `secrets.py` or a function in `pyrules.py`, plus a test with one hit and one near miss.
The `samples/` folder holds deliberately bad code for the tests; it is not imported.
