Review this project's dependencies. Below are the FACTS from code (imports that are not installed), then the project files.

Report, as short Markdown sections:
1. Missing: imports the code uses that are not installed or not declared.
2. Unused: packages declared (requirements.txt, pyproject.toml, setup.py) that no file imports.
3. Unpinned: declared without a version, or with an open-ended range, where a pin would prevent surprises.
4. Risky: anything with a known history of problems, or a heavy dependency used for one small thing the standard library already does.
For each item: the package, the file that declares or imports it, and one recommended action. Do not invent packages that are not there.

FACTS:
{facts}

PROJECT:
{context}
