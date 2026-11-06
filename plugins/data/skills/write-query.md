---
name: write-query
description: Write optimized SQL for a dialect with best practices
when: write sql, query, sql for, cte, join, aggregate, optimize query, cohort, top n
---
# Write query: natural language to SQL

## 1. Parse the request
Output columns; filters (time range, segments, status); aggregations; joins; ordering; limit or top-N.

## 2. Dialect
If unknown, ask: PostgreSQL, Snowflake, BigQuery, Redshift, Databricks SQL, MySQL, SQL Server, DuckDB, SQLite, other. Local default when a .db/.sqlite file is present: SQLite. Remember the dialect for the session. Dialect details are in the sql-queries skill.

## 3. Schema
Local: read CREATE TABLE statements, `.schema` of a SQLite file, CSV headers, or migration files. A live warehouse is "not available locally": ask for the schema. Never invent table or column names; if unsure, say so and list assumptions.

## 4. Write it
- **Structure:** CTEs for multi-step logic; one CTE per logical step; descriptive names (`daily_signups`, `active_users`).
- **Performance:** no `SELECT *`; filter early; use partition filters when available; prefer EXISTS over IN for large subqueries; right JOIN type (no LEFT JOIN when INNER is correct); avoid correlated subqueries when a join or window works; watch for exploding many-to-many joins.
- **Readability:** comment the "why"; consistent indentation; meaningful aliases; one major clause per line.
- **Dialect:** use the dialect's own date, string and window syntax.

## 5. Present
1. The complete query in an SQL code block.
2. A brief explanation of each CTE or section.
3. Performance notes if relevant.
4. Modification suggestions (other time range, granularity, extra filters).

## 6. Execute
If a SQLite or CSV source exists locally, offer to run a read-only SELECT and show results. Otherwise the query is ready to copy-paste. Mention if it needs parameters for date ranges or must be idempotent.
