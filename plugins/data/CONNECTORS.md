# Connectors (local)

The original plugin uses `~~category` placeholders for MCP servers. Here everything runs on this machine:

| Category | Original placeholder | Local equivalent |
|----------|---------------------|------------------|
| Data warehouse | `~~data warehouse` | Files in the project (CSV, TSV, JSON, `.sql`, SQLite dumps) read with `ls/grep/read`; profile a CSV with Python's stdlib `csv`/`sqlite3`. Running SQL against a live warehouse: **not available locally** - the query is written for you to run, then paste or save the results. |
| Notebook | `~~notebook` | Not available locally. Code (Python, SQL) is written into the answer or a file you own. |
| Product analytics | `~~product analytics` | Not available locally. Export to CSV and point the command at the file. |
| Project tracker | `~~project tracker` | Not available locally. |
