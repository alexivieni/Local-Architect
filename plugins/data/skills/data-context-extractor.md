---
name: data-context-extractor
description: Generate or improve a company-specific data analysis skill by extracting knowledge about schemas, metrics and terminology
when: data context, data skill, warehouse knowledge, metric definitions, terminology, schema documentation
---
# Data context extractor

Builds a company-specific data skill (a local `skills/<company>-data-analyst.md` plus reference notes). Two modes.

## Bootstrap mode (new skill)
**Phase 1 discover.** Ask which database (BigQuery, Snowflake, PostgreSQL/Redshift, Databricks, SQLite, files). Local: read schema files, migrations, `.schema` of SQLite, CSV headers. A live warehouse is "not available locally": ask for a schema export. Ask "which 3-5 tables do analysts query most?" and pull their columns.
**Phase 2 ask** (a few at a time, conversationally):
- Entity disambiguation: what does "user" or "customer" mean; types; relationships (1:1, 1:many); linking IDs.
- Primary identifiers: main ID per entity; multiple IDs; legacy ID systems.
- Key metrics: the 2-3 most asked, exact formulas (ARR = monthly_revenue x 12), source columns, period conventions.
- Data hygiene: what is always filtered out (is_test, is_internal, is_fraud, status = 'deleted').
- Gotchas: confusing columns, timezones, NULL quirks, historical vs current tables.
**Phase 3 generate** this structure:
```
[company]-data-analyst/
  SKILL.md
  references/
    entities.md
    metrics.md
    tables/[domain].md
    dashboards.json   (optional)
```
Write SKILL.md with frontmatter (name, description), an entity disambiguation section, terminology, standard filters, and a "Knowledge Base Navigation" list linking the reference files, plus the SQL dialect notes (see sql-queries).
**Phase 4 deliver:** create the files in the project folder (not zipped unless asked) and summarize what was captured.

## Iteration mode (improve an existing skill)
1. Read the existing skill and references.
2. Ask which domain or topic needs context and which queries fail or return wrong results (new domain, missing metric, undocumented relationship, new terminology).
3. Explore the domain's tables, ask domain questions, write `references/[domain].md`.
4. Update the Knowledge Base Navigation section.

## Reference file standards
- **Table:** Location (full path); Description; Primary Key; Update Frequency; Key Columns (name, type, description, notes); Relationships; 2-3 sample queries.
- **Metric:** Name; Definition; Formula with column references; Source tables; Caveats.
- **Entity:** Name; Definition; Primary table; ID fields; Relationships; Common filters.

## Quality checklist
- [ ] SKILL.md has complete frontmatter (name, description)
- [ ] Entity disambiguation is clear
- [ ] Key terminology defined
- [ ] Standard filters/exclusions documented
- [ ] 2-3 sample queries per domain
- [ ] SQL uses the correct dialect
- [ ] Reference files linked from the navigation section
Do not invent facts about the company: unknown means ask.
