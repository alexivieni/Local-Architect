"""CSV rows -> totals -> a laid-out document."""
import csv
from collections import defaultdict

from .layout import Flow


def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["units"], r["revenue"] = int(r["units"]), float(r["revenue"])
    return rows


def totals(rows, key):
    t = defaultdict(float)
    for r in rows:
        t[r[key]] += r["revenue"]
    return sorted(t.items(), key=lambda kv: -kv[1])


def build(rows, title="Report"):
    flow = Flow(title)
    flow.heading(title)
    total = sum(r["revenue"] for r in rows)
    flow.paragraph(f"{len(rows)} lines, {sum(r['units'] for r in rows)} units, revenue {total:,.2f}. "
                   "Figures come straight from the CSV; nothing is estimated.")
    flow.heading("Revenue by region", 14)
    by_region = totals(rows, "region")
    flow.bar_chart(by_region)
    flow.heading("Detail", 14)
    flow.table(["Region", "Product", "Units", "Revenue"],
               [[r["region"], r["product"], r["units"], f"{r['revenue']:,.2f}"] for r in rows], widths=[140, 140, 100, 115])
    return flow.doc
