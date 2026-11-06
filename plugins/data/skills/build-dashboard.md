---
name: build-dashboard
description: Build an interactive self-contained HTML dashboard with KPI cards, charts, filters and a table
when: dashboard, kpi, html dashboard, executive overview, filters, report page
---
# Build dashboard (one self-contained HTML file)

## 1. Requirements
Purpose (executive overview, operations, deep dive, team report); audience; key metrics; dimensions to filter by; data source.

## 2. Data
Local file (CSV/JSON/SQLite) parsed with the stdlib and embedded as JSON in the HTML. Warehouse = "not available locally". With only a description, make a realistic sample dataset, label the page "sample data" and explain how to swap in real data.

## 3. Layout
```
+--------------------------------------------------+
|  Dashboard Title                    [Filters v]  |
+------------+------------+------------+-----------+
|  KPI Card  |  KPI Card  |  KPI Card  | KPI Card  |
+------------+------------+------------+-----------+
|    Primary Chart        |   Secondary Chart      |
|    (largest area)       |                        |
+-------------------------+------------------------+
|    Detail Table (sortable, scrollable)           |
+--------------------------------------------------+
```
2-4 KPI cards; 1-3 charts; optional detail table; filters in header or sidebar.

## 4. Build
- **HTML:** semantic, responsive CSS Grid or Flexbox, filter controls, KPI cards, chart containers, sortable table headers.
- **CSS:** clean white/grey with accent colors, cards with light shadows, system fonts, responsive, print-friendly.
- **JS:** Chart.js for charts; filters update every chart and the table; sortable columns; hover tooltips; number formatting (commas, currency, percentages).
- **Data:** all data embedded as JS variables, no fetches. Chart.js comes from a CDN, which needs internet; offline, either inline a local copy of the library file or draw simple SVG/canvas charts by hand and say which you did.

## 5. Chart types (Chart.js)
Line = time series; bar = category comparison; doughnut = composition under 6 categories; stacked bar = composition over time; mixed bar+line = volume with a rate. Filter change: update datasets then `chart.update('none')`.

## 6. Size and performance
| Data size | Approach |
|---|---|
| under 1,000 rows | embed directly |
| 1,000-10,000 | embed; pre-aggregate for charts |
| 10,000-100,000 | embed only aggregated data (monthly_revenue, top_products, kpis) |
| over 100,000 | not suitable; use a BI tool or paginate |
Line charts under 500 points per series; bar charts under 50 categories; scatter at most 1,000 points; table 50-200 visible rows with pagination; animation off for many charts.

## 7. Save
Save as a descriptive `*.html` (e.g. `sales_dashboard.html`), tell the person to open it in a browser, and explain how to update the data. Dashboards are point-in-time snapshots.
