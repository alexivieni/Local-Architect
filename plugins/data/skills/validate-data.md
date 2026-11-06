---
name: validate-data
description: QA an analysis before sharing: methodology, accuracy and bias checks with a confidence rating
when: validate, check my analysis, review analysis, qa, sanity check, before sharing, spot-check
---
# Validate data: QA an analysis before sharing

Input can be a report, file, notebook, SQL with results, charts with data, or a description of method and findings.

## Steps
1. **Methodology:** question framing; data selection and time range; population definition (unintended exclusions); metric definitions; fair baseline and comparison.
2. **Pre-delivery QA checklist** (below).
3. **Pitfalls** (below).
4. **Verify calculations:** recompute a few key numbers independently from the raw data; subtotals sum to totals; percentages sum to about 100%; YoY/MoM use the correct base periods; filters consistent across metrics. Run a second, different calculation for each key metric and compare.
5. **Visualizations:** axes start at zero for bars; consistent scales; titles match content; truncated axes, odd intervals, 3D effects.
6. **Narrative:** conclusions supported by data; alternatives acknowledged; uncertainty stated; recommendations follow; confidence matches evidence.
7. **Improvements:** extra analyses, caveats, better framings, missing context.
8. **Confidence:** **Ready to share** (sound, verified, minor suggestions), **Share with noted caveats** (list required caveats), **Needs revision** (list required changes in priority order).

## Pre-delivery QA checklist
Data quality: [ ] source verification [ ] freshness ("as of" date) [ ] completeness (no gaps) [ ] null handling [ ] deduplication (no double counting) [ ] filters correct.
Calculation: [ ] aggregation logic (GROUP BY, grain) [ ] denominator correct and non-zero [ ] date alignment (same length, partial periods excluded) [ ] join types and no many-to-many inflation [ ] metric definitions match stakeholders [ ] subtotals sum.
Reasonableness: [ ] magnitude (no negative revenue, % in 0-100) [ ] trend continuity [ ] cross-reference to other sources [ ] order of magnitude [ ] edge cases.
Presentation: [ ] chart accuracy [ ] number formatting [ ] insight titles with date ranges [ ] caveats stated [ ] reproducible.

## Pitfalls
- **Join explosion:** row count rises after a join; count with COUNT(DISTINCT id).
- **Survivorship bias:** ask who is NOT in the data.
- **Incomplete period:** compare complete periods or same number of days.
- **Denominator shifting:** same definitions across periods.
- **Average of averages:** use the weighted average (100 users at $50 and 10 at $200 = $63.64, not $125).
- **Timezone mismatch:** standardize on UTC.
- **Selection bias:** segments must not be defined by the outcome.
- Others: Simpson's paradox, correlation as causation, small samples, outliers moving averages, multiple testing, look-ahead bias, cherry-picked ranges.
Red flags: a metric changing more than 50% period over period, exact round numbers, rates at exactly 0% or 100%, identical values across periods, results that perfectly confirm the hypothesis.

## Output format
```
## Validation Report

### Overall Assessment: [Ready to share | Share with caveats | Needs revision]

### Methodology Review
[Findings about approach, data selection, definitions]

### Issues Found
1. [Severity: High/Medium/Low] [Issue description and impact]

### Calculation Spot-Checks
- [Metric]: [Verified / Discrepancy found]

### Visualization Review
[Any issues with charts or visual presentation]

### Suggested Improvements
1. [Improvement and why it matters]

### Required Caveats for Stakeholders
- [Caveat that must be communicated]
```
