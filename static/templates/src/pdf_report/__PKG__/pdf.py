"""A minimal PDF 1.4 writer: pages of content streams, two standard fonts, a correct cross-reference table."""
import zlib

PAGE_W, PAGE_H = 595, 842          # A4 in points


def escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def encode(text):
    return text.encode("latin-1", errors="replace")


class Page:
    def __init__(self):
        self.ops = []

    def text(self, x, y, s, size=11, bold=False):
        font = "F2" if bold else "F1"
        self.ops.append(f"BT /{font} {size} Tf {x:.2f} {y:.2f} Td ({escape(s)}) Tj ET")

    def rect(self, x, y, w, h, gray=0.5, fill=True):
        self.ops.append(f"{gray:.3f} g {gray:.3f} G {x:.2f} {y:.2f} {w:.2f} {h:.2f} re {'f' if fill else 'S'} 0 g 0 G")

    def line(self, x1, y1, x2, y2, width=0.5):
        self.ops.append(f"{width} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")


class Document:
    def __init__(self, title="Report", compress=True):
        self.title, self.compress, self.pages = title, compress, []

    def new_page(self):
        p = Page()
        self.pages.append(p)
        return p

    def to_bytes(self):
        objs = []                          # index i -> object number i+1

        def add(body):
            objs.append(body)
            return len(objs)

        catalog = add(None)
        pages = add(None)
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        info = add(b"<< /Title (" + encode(escape(self.title)) + b") /Producer (__NAME__) >>")
        kids = []
        for page in self.pages or [Page()]:
            data = encode("\n".join(page.ops))
            if self.compress:
                data = zlib.compress(data)
                head = f"<< /Length {len(data)} /Filter /FlateDecode >>".encode()
            else:
                head = f"<< /Length {len(data)} >>".encode()
            content = add(head + b"\nstream\n" + data + b"\nendstream")
            kids.append(add(f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                            f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {content} 0 R >>".encode()))
        objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
        objs[pages - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self.to_bytes())
