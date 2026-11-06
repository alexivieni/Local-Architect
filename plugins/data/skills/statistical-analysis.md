---
name: statistical-analysis
description: Apply statistics: descriptive stats, trends, outliers, hypothesis tests, and when to be cautious
when: statistics, significance, p-value, outlier, correlation, a/b test, confidence interval, distribution, forecast
---
# Statistical analysis

Compute with the stdlib `statistics` module (mean, median, stdev, quantiles) or pandas/numpy if installed. Never estimate numbers by eye.

## Descriptive statistics
| Situation | Use |
|---|---|
| Symmetric, no outliers | Mean |
| Skewed | Median |
| Categorical/ordinal | Mode |
| Highly skewed (revenue per user) | Report mean and median; the gap shows skew |
Always report mean AND median for business metrics. Spread: standard deviation (normal data), IQR = p75 - p25 (skewed), coefficient of variation = StdDev / Mean (compare scales), range. Percentiles: p1, p5, p25, p50, p75, p90, p95, p99. Describe every distribution by shape, center, spread, outliers, bounds.

## Trends
Moving averages (7-day for daily data, 28-day for monthly patterns). Period comparisons: WoW, MoM, YoY (best for seasonal data). Growth: simple = (current - previous) / previous; CAGR = (ending / beginning)^(1/years) - 1; log growth = ln(current / previous). Seasonality: look at the raw series, day-of-week and month-of-year averages; compare YoY. Forecasts: naive (tomorrow = today), seasonal naive, linear trend, moving average. Always give a range ("10K-12K signups"), never a point. Escalate non-linear trends, multiple seasonalities, external drivers.

## Outliers
Z-score: |z| > 3 (normal data). IQR: outside Q1 - 1.5 x IQR or Q3 + 1.5 x IQR. Percentile: below p1 or above p99. Do NOT auto-remove: investigate (data error -> fix/remove; genuine extreme -> keep, use median; different population -> segment). Report it ("excluded 47 records (0.3%) above $50K"). Time series: expected value vs actual, flag deviations over 2-3 standard deviations, separate point anomalies from change points.

## Hypothesis testing
H0 (no difference), H1 (difference), alpha (usually 0.05), compute the test statistic and p-value; p < alpha means reject H0.
| Scenario | Test |
|---|---|
| Two group means | independent t-test |
| Two proportions | z-test for proportions |
| Paired before/after | paired t-test |
| 3+ group means | ANOVA |
| Non-normal, two groups | Mann-Whitney U |
| Two categorical variables | chi-squared |
Report effect size, confidence interval and business impact, not just p. Statistical significance is not practical significance. Small samples: at least 30 events per group, more for small effects; say when power is limited.

## Be cautious
- Correlation is not causation: reverse causation, confounders, coincidence.
- Multiple comparisons: 20 tests at 0.05 gives about 1 false hit; Bonferroni = alpha / number of tests.
- Simpson's paradox: check key segments.
- Survivorship bias: who is missing?
- Ecological fallacy: group trends are not individual trends.
- False precision: say "4-6%", not "4.73%".

## Output
Method used and why; the numbers; interpretation in plain words; caveats; next step.
