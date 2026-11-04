---
name: dependency-hygiene
description: Keep third-party packages few, pinned, used and safe
when: dependency, dependencies, requirements.txt, pyproject.toml, package.json, lock file, lockfile, pin version, pip install, npm install, upgrade package, unused package
files: requirements*.txt, pyproject.toml, setup.py, setup.cfg, package.json, *.lock, Pipfile
---
# Dependency hygiene

Every dependency is code you did not write but must ship, update and trust.

- **Before adding one, check the standard library.** `json`, `csv`, `pathlib`, `urllib`, `sqlite3`, `argparse`, `dataclasses` cover
  a lot. Adding a package for one function is rarely worth it. This harness often runs offline: a new package may not be installable.
- **Every import has a declared dependency, and every declared dependency is imported.** `grep` the import names against the
  requirements file. Remove unused ones; add missing ones.
- **Pin what you deploy.** Applications pin exact versions in a lock file or `==` pins. Libraries declare ranges (`>=2.1,<3`), not pins.
- **Separate dev from runtime.** Test and lint tools go in a dev extra or `requirements-dev.txt`, not the runtime list.
- **Upgrade one package at a time** and run the tests after each. Read the changelog for major versions.
- **Check for known vulnerabilities** with `pip-audit` or `npm audit` if available, and say if it is not installed.
- **Watch the name.** A misspelled package name can be a malicious lookalike. Confirm the exact name before installing.
- **Note the license** of anything new if the project is distributed.
