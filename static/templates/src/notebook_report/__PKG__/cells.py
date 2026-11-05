"""Split a source file into cells and run them, capturing stdout, rich outputs and errors."""
import contextlib
import io
import traceback
from dataclasses import dataclass, field
from html import escape


@dataclass
class Cell:
    kind: str                  # "code" or "markdown"
    source: str
    stdout: str = ""
    rich: list = field(default_factory=list)      # HTML fragments
    error: str = ""


def split(text):
    cells, kind, buf = [], None, []

    def flush():
        src = "\n".join(buf).strip("\n")
        if kind and src.strip():
            cells.append(Cell(kind, src))

    for line in text.splitlines():
        if line.startswith("# %%"):
            flush()
            kind, buf = ("markdown" if "[markdown]" in line else "code"), []
        else:
            if kind is None:
                kind = "code"
            buf.append(line)
    flush()
    return cells


class SVG(str):
    """Marks a string as ready-made SVG markup for show()."""


def bar(mapping, width=480, height=180):
    top = max(mapping.values(), default=0) or 1
    slot = width / max(len(mapping), 1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">']
    for i, (k, v) in enumerate(mapping.items()):
        h = (height - 30) * v / top
        x = i * slot + slot * 0.15
        parts.append(f'<rect x="{x:.1f}" y="{height - 20 - h:.1f}" width="{slot * 0.7:.1f}" height="{h:.1f}" fill="currentColor" opacity="0.8"/>'
                     f'<text x="{x + slot * 0.35:.1f}" y="{height - 6}" text-anchor="middle" font-size="11">{escape(str(k))}</text>')
    parts.append("</svg>")
    return SVG("".join(parts))


def to_html(obj):
    if isinstance(obj, SVG):
        return str(obj)
    if isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
        cols = list(obj[0])
        head = "".join(f"<th>{escape(str(c))}</th>" for c in cols)
        rows = "".join("<tr>" + "".join(f"<td>{escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>" for r in obj)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    return f"<pre>{escape(repr(obj))}</pre>"


def run(cells):
    """Execute code cells in one namespace. Stops after the first failing cell. -> True if all ran."""
    ns = {"__name__": "__report__", "bar": bar}
    for cell in cells:
        if cell.kind != "code":
            continue
        ns["show"] = lambda obj, c=cell: c.rich.append(to_html(obj))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(cell.source, "<cell>", "exec"), ns)       # the person's own analysis file, run on purpose
        except Exception:
            cell.error = traceback.format_exc(limit=-1)
            cell.stdout = out.getvalue()
            return False
        cell.stdout = out.getvalue()
    return True
