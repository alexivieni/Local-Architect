"""Cells -> one HTML page, or -> an nbformat 4 notebook."""
import json
from html import escape

CSS = """:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--code:#f5f5f7;--accent:#2f6fde;--bad:#c62828}
@media (prefers-color-scheme: dark){:root{--bg:#111214;--fg:#f2f2f4;--muted:#a1a1a8;--line:#2c2d31;--code:#1c1d20;--accent:#6ea0ff;--bad:#ff6b6b}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto}pre{background:var(--code);padding:10px;border-radius:6px;overflow:auto}
.src{color:var(--muted)}.err{color:var(--bad)}svg{max-width:100%;color:var(--accent)}
table{border-collapse:collapse}td,th{border-bottom:1px solid var(--line);padding:4px 10px;text-align:left}"""


def markdown_html(src):
    out = []
    for line in src.splitlines():
        t = line.lstrip("#").strip() if line.startswith("# ") or line == "#" else line
        raw = line[2:] if line.startswith("# ") else ""
        level = len(raw) - len(raw.lstrip("#"))
        if level and raw[level:level + 1] == " ":
            out.append(f"<h{min(level + 1, 6)}>{escape(raw[level:].strip())}</h{min(level + 1, 6)}>")
        elif t:
            out.append(f"<p>{escape(t)}</p>")
    return "".join(out)


def to_html(cells, title="Report", show_source=True):
    parts = []
    for c in cells:
        if c.kind == "markdown":
            parts.append(markdown_html(c.source))
            continue
        if show_source:
            parts.append(f'<pre class="src">{escape(c.source)}</pre>')
        if c.stdout:
            parts.append(f"<pre>{escape(c.stdout)}</pre>")
        parts += c.rich
        if c.error:
            parts.append(f'<pre class="err">{escape(c.error)}</pre>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")


def to_ipynb(cells):
    nb_cells = []
    for c in cells:
        lines = c.source.splitlines(keepends=True)
        if c.kind == "markdown":
            nb_cells.append({"cell_type": "markdown", "metadata": {},
                             "source": [ln[2:] if ln.startswith("# ") else ln.lstrip("#") for ln in lines]})
            continue
        outputs = []
        if c.stdout:
            outputs.append({"output_type": "stream", "name": "stdout", "text": c.stdout.splitlines(keepends=True)})
        for html in c.rich:
            outputs.append({"output_type": "display_data", "metadata": {}, "data": {"text/html": [html]}})
        if c.error:
            outputs.append({"output_type": "error", "ename": "Error", "evalue": c.error.strip().splitlines()[-1], "traceback": c.error.splitlines()})
        nb_cells.append({"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": outputs, "source": lines})
    return json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {"language_info": {"name": "python"}}, "cells": nb_cells}, indent=1)
