# __NAME__

One HTML file with inline SVG charts, built from JSON. Open it anywhere; it loads nothing from the internet.

    python -m __PKG__ samples/metrics.json --out dashboard.html
    python -m pytest -q

## Input

    {"title": "...", "kpis": [{"label": "Signups", "value": 1240, "previous": 1100, "unit": ""}],
     "series": {"labels": ["Mon", ...], "lines": {"Visits": [..], "Signups": [..]}},
     "bars": {"Chrome": 61, "Safari": 24}}

## Layout

    __PKG__/scale.py   nice ticks (1-2-5 steps) and linear scales
    __PKG__/charts.py  line, bar and sparkline SVG; every value is HTML-escaped
    __PKG__/page.py    the page: theme tokens in CSS variables, light and dark
