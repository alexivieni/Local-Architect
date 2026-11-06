---
name: create-viz
description: Create publication-quality visualizations with Python from data
when: chart, plot, graph, visualize, create viz, matplotlib, plotly, bar chart, line chart
---
# Create viz

## 1. Understand
Data source (query results, pasted data, CSV file); chart type (given or recommend one); purpose (exploration, presentation, report, dashboard part); audience.

## 2. Get the data
Local only: a CSV/JSON file read with the stdlib `csv`/`json` (or pandas if installed), a SQLite file via `sqlite3`, or pasted data. A warehouse is "not available locally". If the library (matplotlib, seaborn, plotly) is not installed say so; give the code and the `pip install` line, do not run installs.

## 3. Pick the chart (when not specified)
| Data relationship | Chart |
|---|---|
| Trend over time | Line chart |
| Comparison across categories | Bar chart (horizontal if many categories) |
| Part-to-whole | Stacked bar or area (pie only under 6 categories) |
| Distribution | Histogram or box plot |
| Correlation of two variables | Scatter plot |
| Two variables over time | Dual-axis line or grouped bar |
| Geographic | Choropleth map |
| Ranking | Horizontal bar |
| Flow or process | Sankey |
| Matrix of relationships | Heatmap |
Explain the choice in one sentence.

## 4. Code
matplotlib + seaborn is the default (static); plotly when "interactive" is asked.
```python
import matplotlib.pyplot as plt
import seaborn as sns
plt.style.use('seaborn-v0_8-whitegrid')
sns.set_palette("husl")
fig, ax = plt.subplots(figsize=(10, 6))
# [chart-specific code]
ax.set_title('Clear, Descriptive Title', fontsize=14, fontweight='bold')
ax.set_xlabel('X-Axis Label', fontsize=11)
ax.set_ylabel('Y-Axis Label', fontsize=11)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
plt.tight_layout()
plt.savefig('chart_name.png', dpi=150, bbox_inches='tight')
```
Number formats: '45.2%' not '0.452'; '$1.2M' not '1200000'; '2.3K' not '2300'.

## 5. Design rules
- **Color:** colorblind-friendly palette; color means something; highlight the key point, grey the rest.
- **Text:** the title states the insight ("Revenue grew 23% YoY"); readable axis labels; labels on key points.
- **Layout:** whitespace; legend not covering data; sort categories by value unless order is natural.
- **Accuracy:** bar charts start at zero; no unmarked axis breaks; same scales across panels; sensible precision.

## 6. Save and present
Save a PNG with a descriptive name, give the code, suggest 1-2 variations (other chart type, grouping, zoom). Never plot numbers you did not read from the data.
