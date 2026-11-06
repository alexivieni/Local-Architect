---
name: data-visualization
description: Choose the right chart and build it with Python (matplotlib, seaborn, plotly) following design and accessibility principles
when: chart type, which chart, visualization, figure, colorblind, accessibility, heatmap, small multiples
---
# Data visualization

## Choose by data relationship
| Showing | Best | Alternatives |
|---|---|---|
| Trend over time | Line | Area (cumulative or composition) |
| Category comparison | Vertical bar | Horizontal bar, lollipop |
| Ranking | Horizontal bar | Dot plot, slope chart |
| Part-to-whole | Stacked bar | Treemap, waffle |
| Composition over time | Stacked area | 100% stacked bar |
| Distribution | Histogram | Box, violin, strip |
| Correlation (2 variables) | Scatter | Bubble (3rd as size) |
| Correlation (many) | Heatmap of matrix | Pair plot |
| Geography | Choropleth | Bubble map |
| Flow / process | Sankey | Funnel |
| Performance vs target | Bullet | Gauge (single KPI only) |
| Many KPIs | Small multiples | Dashboard |
Avoid: pie charts (unless under 6 categories), 3D charts (never), dual axes (label both, may imply correlation), stacked bars with many categories (use small multiples), donuts (single KPI at most).

## Code patterns (matplotlib; install check first, no pip runs)
```python
import matplotlib.pyplot as plt
import seaborn as sns
plt.style.use('seaborn-v0_8-whitegrid')
PALETTE = ['#4C72B0', '#DD8452', '#55A868', '#C44E52', '#8172B3', '#937860']
fig, ax = plt.subplots(figsize=(10, 6))
# line:   for label, g in df.groupby('category'): ax.plot(g['date'], g['value'], label=label, linewidth=2)
# bar:    d = df.sort_values('metric'); ax.barh(d['category'], d['metric'], color=PALETTE[0])
# hist:   ax.hist(df['value'], bins=30, color=PALETTE[0], edgecolor='white'); add mean and median axvline
# heat:   sns.heatmap(df.pivot_table(index='r', columns='c', values='m', aggfunc='sum'), annot=True, fmt=',.0f', cmap='YlOrRd')
ax.set_title('Insight as title', fontweight='bold')
ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
plt.tight_layout(); plt.savefig('chart.png', dpi=150, bbox_inches='tight')
```
Small multiples: one axes per category with `sharex=True, sharey=True`, hide empty subplots. Interactive: `plotly.express` (`px.line`, `px.scatter`) then `fig.write_html('chart.html')`. Number format helper: >=1e9 'B', >=1e6 'M', >=1e3 'K'; currency adds '$'; percent `f'{v:.1f}%'`.

## Design principles
- **Color:** color encodes data; accent for the key insight, grey the rest; sequential = one hue light to dark; diverging = two hues with neutral middle; categorical max 6-8 hues; blue/orange over red/green.
- **Typography:** title states the insight; subtitle adds date range/filters/source; readable axis labels; labels on key points; annotate.
- **Layout:** remove chart junk; sort by value; time series wide (3:1 to 2:1); white space.
- **Accuracy:** bars start at zero; line baselines may be non-zero; consistent scales across panels; show uncertainty; label axes with units.

## Accessibility
Never color alone (add line styles, patterns, direct labels); alt text with the key finding; a data table alternative; enough contrast; text at least 10pt (titles 12pt); works in black and white.
Checklist before sharing: [ ] works without color [ ] text readable [ ] title describes the insight [ ] axes labeled with units [ ] legend clear [ ] data source and date range noted
