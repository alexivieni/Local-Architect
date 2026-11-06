---
name: explore-data
description: Profile and explore a dataset: shape, quality, patterns, dimensions and metrics
when: explore data, profile, data quality, null rate, distribution, new table, duplicates, csv
---
# Explore data: profile a table or file

## 1. Access
Local file (CSV/JSON/SQLite): read it with the stdlib (csv, json, sqlite3 read-only) or the profiler tool if the project has one. Warehouse tables are "not available locally": ask for a file or an export. If a schema is only described, say what profiling queries to run.

## 2. Structure
Table level: rows and columns; grain (one row per what); primary key and uniqueness; last updated; how far back it goes.
Classify each column as **Identifier**, **Dimension**, **Metric**, **Temporal**, **Text**, **Boolean** or **Structural** (JSON/arrays).

## 3. Profile
- Table: row count, column types breakdown, date range.
- Every column: null count and rate; distinct count and cardinality ratio (distinct / total); top 5-10 values; bottom 5 values.
- Numeric: min, max, mean, median, std dev, p1 p5 p25 p75 p95 p99, zero count, negative count.
- String: min/max/avg length, empty strings, format pattern, case consistency, leading/trailing whitespace.
- Date: min, max, nulls, future dates, distribution by month/week, gaps.
- Boolean: true, false, null counts, true rate.
Present as a summary table grouped by column type.

## 4. Quality issues (flag with severity)
High nulls: >5% warn, >20% alert. Cardinality surprises (a user_id with 50 distinct values). Suspicious values: negatives, future dates, placeholders ("N/A", "TBD", "test", 999999). Duplicates on the natural key. Skew. Encoding: mixed case, whitespace, inconsistent formats.

**Completeness score per column:** Complete (>99% non-null), Mostly complete (95-99%, investigate), Incomplete (80-95%, understand why), Sparse (<80%, may be unusable).
Also check consistency (same concept written differently: "USA", "US"), referential integrity, business rules (end before start), accuracy red flags (placeholders, one dominant default, stale updated_at, impossible values, round-number bias) and timeliness.

## 5. Relationships and patterns
Foreign key candidates, hierarchies (country > state > city), correlations (|r| > 0.7 flag; correlation is not causation), derived and redundant columns. Distribution shape: normal, skewed right/left, bimodal, power law, uniform. Time patterns: trend, seasonality, day-of-week, holidays, change points, anomalies.

## 6. Suggest
Best dimensions (3-50 distinct values), key metrics, time columns, join keys. Then 3-5 specific follow-up analyses ("trend of [metric] by [time] grouped by [dimension]").

## Output format
```
## Data Profile: [table_name]

### Overview
- Rows: 2,340,891
- Columns: 23 (8 dimensions, 6 metrics, 4 dates, 5 IDs)
- Date range: 2021-03-15 to 2024-01-22

### Column Details
[summary table]

### Data Quality Issues
[flagged issues with severity]

### Recommended Explorations
[numbered list of suggested follow-up analyses]
```
Quality flags are heuristics; say so. Sample very large files and say you sampled.
