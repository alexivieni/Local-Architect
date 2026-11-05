"""The whole page. Theme tokens live in :root and are redefined for dark mode."""
from html import escape

from .charts import bar_chart, line_chart, sparkline

CSS = """
:root { --bg:#f7f7f8; --card:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea;
        --good:#1a7f37; --bad:#c62828; --series-0:#2f6fde; --series-1:#d9822b; --series-2:#3d9970; --series-3:#8e44ad; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111214; --card:#1c1d20; --fg:#f2f2f4; --muted:#a1a1a8; --line:#2c2d31;
          --good:#4cc26d; --bad:#ff6b6b; --series-0:#6ea0ff; --series-1:#f0a85a; --series-2:#5fc49a; --series-3:#c28ae0; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; }
main { max-width: 980px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 16px; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.kpi .label { color:var(--muted); font-size:13px; } .kpi .value { font-size:26px; font-weight:600; }
.delta.up { color:var(--good); } .delta.down { color:var(--bad); }
.spark { width:100%; height:28px; color:var(--series-0); }
svg { width:100%; height:auto; display:block; }
.grid { stroke:var(--line); } .axis { fill:var(--muted); font-size:11px; }
.legend span { display:inline-block; margin-right:12px; font-size:13px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }
"""


def delta(value, previous):
    if not previous:
        return "", ""
    pct = (value - previous) / abs(previous) * 100
    return ("up" if pct >= 0 else "down"), f"{'+' if pct >= 0 else ''}{pct:.1f}%"


def build(data):
    title = escape(str(data.get("title", "Dashboard")))
    series = data.get("series", {})
    lines = series.get("lines", {})
    tiles = []
    for i, k in enumerate(data.get("kpis", [])):
        cls, text = delta(k["value"], k.get("previous"))
        spark = sparkline(list(lines.values())[i]) if i < len(lines) else ""
        tiles.append(f'<div class="card kpi"><div class="label">{escape(k["label"])}</div>'
                     f'<div class="value">{k["value"]:,}{escape(k.get("unit", ""))}</div>'
                     f'<div class="delta {cls}">{text} vs previous</div>{spark}</div>')
    legend = "".join(f'<span><i style="background:var(--series-{n % 4})"></i>{escape(name)}</span>' for n, name in enumerate(lines))
    body = [f"<h1>{title}</h1>", f'<section class="kpis">{"".join(tiles)}</section>']
    if lines:
        body.append(f'<section class="card"><div class="legend">{legend}</div>{line_chart(series.get("labels", []), lines)}</section>')
    if data.get("bars"):
        body.append(f'<section class="card" style="margin-top:12px">{bar_chart(list(data["bars"].items()))}</section>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title}</title><style>{CSS}</style></head><body><main>{''.join(body)}</main></body></html>")
