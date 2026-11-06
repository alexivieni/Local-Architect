---
name: sql-queries
description: Write correct, performant SQL across dialects: dialect reference, window functions, CTEs, cohorts, funnels, dedup, debugging
when: sql, window function, cte, cohort retention, funnel, dedupe, dialect, bigquery, snowflake, postgres, sqlite
---
# SQL queries

Local default is SQLite or DuckDB-style SQL on project files. Warehouses are "not available locally": write the query for the person to run.

## Dialect cheatsheet
| Task | PostgreSQL | Snowflake | BigQuery | Redshift | Databricks | SQLite |
|---|---|---|---|---|---|---|
| Add days | `d + INTERVAL '7 days'` | `DATEADD(day,7,d)` | `DATE_ADD(d, INTERVAL 7 DAY)` | `DATEADD(day,7,d)` | `DATE_ADD(d,7)` | `date(d,'+7 days')` |
| Date diff | `d2 - d1` | `DATEDIFF(day,d1,d2)` | `DATE_DIFF(d2,d1,DAY)` | `DATEDIFF(day,d1,d2)` | `DATEDIFF(d2,d1)` | `julianday(d2)-julianday(d1)` |
| Truncate month | `DATE_TRUNC('month',t)` | `DATE_TRUNC('month',t)` | `DATE_TRUNC(t, MONTH)` | `DATE_TRUNC('month',t)` | `DATE_TRUNC('MONTH',t)` | `strftime('%Y-%m-01',t)` |
| Case-insensitive match | `ILIKE` | `ILIKE` | `LOWER(c) LIKE` | `ILIKE` | `ILIKE` | `LIKE` (ASCII) |
| JSON field | `data->>'k'` | `data:k::string` | `JSON_VALUE(data,'$.k')` | `JSON_EXTRACT_PATH_TEXT` | `data:k` | `json_extract(data,'$.k')` |
| Array explode | `UNNEST` | `LATERAL FLATTEN` | `UNNEST` | - | `EXPLODE` | `json_each` |
Performance per dialect: PostgreSQL `EXPLAIN ANALYZE`, indexes on filter/join columns, partial indexes. Snowflake clustering keys, right-size warehouse, `RESULT_SCAN`. BigQuery filter on partition columns, avoid `SELECT *` (billed per byte), `APPROX_COUNT_DISTINCT`, dry run. Redshift DISTKEY/SORTKEY, `EXPLAIN`, `ANALYZE`/`VACUUM`. Databricks `OPTIMIZE` + `ZORDER`, Delta time travel. SQLite indexes + `EXPLAIN QUERY PLAN`.

## Common patterns
```sql
-- Window functions
ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC)
RANK() OVER (PARTITION BY category ORDER BY revenue DESC)
SUM(revenue) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total
AVG(revenue) OVER (ORDER BY d ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS moving_avg_7d
LAG(value, 1) OVER (PARTITION BY entity ORDER BY d) AS prev_value
revenue / SUM(revenue) OVER () AS pct_of_total

-- CTE structure: one step per CTE
WITH base_users AS (SELECT user_id, plan_type FROM users WHERE status = 'active'),
user_metrics AS (
  SELECT u.user_id, u.plan_type, COUNT(DISTINCT e.session_id) AS sessions
  FROM base_users u LEFT JOIN events e ON u.user_id = e.user_id
  GROUP BY u.user_id, u.plan_type)
SELECT plan_type, COUNT(*) AS users, AVG(sessions) AS avg_sessions FROM user_metrics GROUP BY plan_type;

-- Cohort retention: cohort_month, cohort_size, then COUNT(DISTINCT CASE WHEN activity_month = cohort_month + N months THEN user_id END) for month_0, month_1, month_3

-- Funnel: per user MAX(CASE WHEN event='x' THEN 1 ELSE 0 END) per step, then SUM per step and ROUND(100.0*SUM(next)/NULLIF(SUM(prev),0),1) conversion

-- Dedup: keep the latest per key
SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY updated_at DESC) AS rn FROM source_table) t WHERE rn = 1;
```

## When a query fails
1. Syntax: dialect mismatch (`ILIKE` is not in BigQuery or SQLite; `SAFE_DIVIDE` is BigQuery only).
2. Column not found: check the schema, typos, case sensitivity of quoted identifiers.
3. Type mismatch: cast explicitly (`CAST(col AS DATE)`).
4. Division by zero: `NULLIF(denominator, 0)`.
5. Ambiguous column: qualify with the table alias.
6. GROUP BY: every non-aggregated column must be listed (BigQuery allows alias).
Rules: no `SELECT *`, filter early, CTE per step, say the dialect you wrote for.
