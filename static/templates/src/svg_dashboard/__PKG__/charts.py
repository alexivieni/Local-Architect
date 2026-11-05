"""SVG charts. Colours are CSS variables (var(--series-N)), so the page theme decides them."""
from html import escape

from .scale import linear, nice_ticks


def _fmt(v):
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:g}"


def line_chart(labels, lines, width=640, height=260, title="Trend"):
    pad_l, pad_r, pad_t, pad_b = 48, 16, 16, 28
    values = [v for vs in lines.values() for v in vs] or [0]
    ticks = nice_ticks(min(0, min(values)), max(values))
    y = linear(ticks[0], ticks[-1], height - pad_b, pad_t)
    x = linear(0, max(len(labels) - 1, 1), pad_l, width - pad_r)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="t-line"><title id="t-line">{escape(title)}</title>']
    for t in ticks:
        out.append(f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="axis" x="{pad_l - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{_fmt(t)}</text>')
    for i, lab in enumerate(labels):
        out.append(f'<text class="axis" x="{x(i):.1f}" y="{height - 8}" text-anchor="middle">{escape(str(lab))}</text>')
    for n, (name, vs) in enumerate(lines.items()):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vs))
        out.append(f'<polyline fill="none" stroke="var(--series-{n % 4})" stroke-width="2" points="{pts}">'
                   f'<title>{escape(name)}</title></polyline>')
    out.append("</svg>")
    return "".join(out)


def bar_chart(pairs, width=640, height=220, title="Breakdown"):
    pad_l, pad_t, pad_b = 16, 16, 28
    top = nice_ticks(0, max((v for _, v in pairs), default=1))[-1]
    y = linear(0, top, height - pad_b, pad_t)
    slot = (width - 2 * pad_l) / max(len(pairs), 1)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="t-bar"><title id="t-bar">{escape(title)}</title>']
    for i, (label, v) in enumerate(pairs):
        bx, by = pad_l + i * slot + slot * 0.15, y(v)
        out.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{slot * 0.7:.1f}" height="{height - pad_b - by:.1f}" rx="3" '
                   f'fill="var(--series-0)"><title>{escape(label)}: {_fmt(v)}</title></rect>'
                   f'<text class="axis" x="{bx + slot * 0.35:.1f}" y="{by - 4:.1f}" text-anchor="middle">{_fmt(v)}</text>'
                   f'<text class="axis" x="{bx + slot * 0.35:.1f}" y="{height - 8}" text-anchor="middle">{escape(label)}</text>')
    out.append("</svg>")
    return "".join(out)


def sparkline(values, width=120, height=28):
    if len(values) < 2:
        return ""
    y = linear(min(values), max(values), height - 2, 2)
    x = linear(0, len(values) - 1, 1, width - 1)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" aria-hidden="true">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/></svg>')
