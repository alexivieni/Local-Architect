---
name: analyze
description: Answer data questions from a quick lookup to a full analysis or formal report
when: analyze, how many, what's driving, trend, drop in, compare segments, data report, metric
---
# Analyze: answer a data question

## 1. Understand the question
Pick the complexity level: **Quick answer** (single metric, simple filter, e.g. "how many signups last week"), **Full analysis** (multi-dimensional, trend, comparison, e.g. "what is driving the drop in conversion"), **Formal report** (methodology, caveats, recommendations, e.g. "quarterly review of subscription metrics"). Note the data needed (tables, metrics, dimensions, time range) and the output format (number, table, chart, narrative).

## 2. Gather data (local only)
- Look for CSV/JSON/SQLite/SQL files in the project (`ls`, `grep`). SQLite: run `sqlite3` read-only or Python `sqlite3` with `SELECT` only. CSV: profile with the stdlib `csv` module.
- A data warehouse connection is "not available locally". Then ask the person to paste results or give a file, or describe the schema so you write queries for them to run (see the sql-queries skill).
- If a query fails, check column names, table names and dialect, then retry once.

## 3. Analyze
Calculate metrics and comparisons, find patterns, trends, outliers; compare across periods, segments and categories. Break big questions into sub-questions.

## 4. Validate before presenting
- Row count sanity: does the number of records make sense?
- Null check: unexpected nulls that skew results?
- Magnitude check: numbers in a reasonable range?
- Trend continuity: gaps in time series?
- Aggregation logic: do subtotals sum to totals?
Note any concern as a caveat.

## 5. Present
- **Quick answer:** state the answer directly with context; show the query used in a code block.
- **Full analysis:** lead with the key finding; support with tables or charts; note methodology and caveats; suggest follow-up questions.
- **Formal report:** Executive summary; Methodology (approach and data sources); Detailed findings with evidence; Caveats, limitations and data quality notes; Recommendations and next steps.

## 6. Visualize where helpful
If a chart says it better than a table, follow the data-visualization skill (chart type, then Python or an HTML dashboard).

Rule: never invent numbers. Every number in the answer comes from a query or file you ran.
