# __NAME__

Generates PDF reports with nothing but the standard library.

    python -m __PKG__ samples/sales.csv --out report.pdf --title "Sales, November"
    python -m pytest -q

## Layout

    __PKG__/pdf.py      the PDF writer: objects, streams, xref table, built-in Helvetica (WinAnsi)
    __PKG__/layout.py   a cursor that flows headings, paragraphs (wrapped), tables and bar charts onto pages
    __PKG__/report.py   CSV -> totals -> document
    __PKG__/__main__.py command line

Fonts are the 14 standard PDF fonts (no embedding), so text is limited to Latin-1. Widths use average
character metrics, which is close enough for wrapping; swap in a real metrics table if you need exact layout.
