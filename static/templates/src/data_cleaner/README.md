# __NAME__

A rule-driven CSV cleaner. You say what each column should be; it fixes what it can, rejects what it cannot,
and tells you exactly what it did.

    python -m __PKG__ samples/messy.csv --rules samples/rules.json --out clean.csv --audit audit.json
    python -m pytest -q

## Rules file

    {"columns": {"name": {"type": "text", "case": "title", "required": true},
                 "joined": {"type": "date"},
                 "spend": {"type": "number"},
                 "active": {"type": "bool"}},
     "key": ["email"]}

Types: `text`, `number`, `int`, `date` (to YYYY-MM-DD), `bool` (true/false), `email` (lower-cased, shape-checked).
Rows with a required value missing or a value that cannot be converted go to the audit's `rejected` list.
