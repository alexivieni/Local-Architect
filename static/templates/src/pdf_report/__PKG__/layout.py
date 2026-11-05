"""Flow content down pages: a cursor that starts a new page when the next block does not fit."""
from .pdf import PAGE_H, PAGE_W, Document

MARGIN = 50
AVG_CHAR = 0.5          # Helvetica average glyph width, as a fraction of the font size


def text_width(s, size):
    return len(s) * size * AVG_CHAR


def wrap(text, size, width):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if text_width(trial, size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


class Flow:
    def __init__(self, title="Report"):
        self.doc = Document(title)
        self.page = self.doc.new_page()
        self.y = PAGE_H - MARGIN
        self.width = PAGE_W - 2 * MARGIN

    def need(self, height):
        if self.y - height < MARGIN:
            self.page = self.doc.new_page()
            self.y = PAGE_H - MARGIN

    def heading(self, s, size=18):
        self.need(size * 2)
        self.y -= size
        self.page.text(MARGIN, self.y, s, size, bold=True)
        self.y -= size * 0.6

    def paragraph(self, s, size=11):
        for line in wrap(s, size, self.width):
            self.need(size * 1.4)
            self.y -= size * 1.4
            self.page.text(MARGIN, self.y, line, size)
        self.y -= size * 0.6

    def table(self, header, rows, widths=None, size=10):
        widths = widths or [self.width / len(header)] * len(header)
        row_h = size * 1.8
        for i, row in enumerate([header] + rows):
            self.need(row_h)
            self.y -= row_h
            if i == 0:
                self.page.rect(MARGIN, self.y - 4, sum(widths), row_h, gray=0.9)
            x = MARGIN
            for cell, w in zip(row, widths):
                self.page.text(x + 4, self.y + 2, str(cell), size, bold=(i == 0))
                x += w
            self.page.line(MARGIN, self.y - 4, MARGIN + sum(widths), self.y - 4, 0.3)
        self.y -= size

    def bar_chart(self, pairs, height=160, size=9):
        """pairs: [(label, value)]. Bars scaled to the largest value."""
        self.need(height + 30)
        top = max((v for _, v in pairs), default=0) or 1
        base = self.y - height
        slot = self.width / max(len(pairs), 1)
        for i, (label, value) in enumerate(pairs):
            h = (height - 20) * value / top
            x = MARGIN + i * slot + slot * 0.15
            self.page.rect(x, base, slot * 0.7, h, gray=0.35)
            self.page.text(x, base + h + 4, f"{value:g}", size)
            self.page.text(x, base - size - 4, label[:14], size)
        self.page.line(MARGIN, base, MARGIN + self.width, base)
        self.y = base - size * 3
