"""WCAG 2.1 contrast arithmetic (stdlib). ratio("#767676", "#fff") -> 4.54; passes(fg, bg, large=False, ui=False) -> bool."""


def _rgb(c):
    c = c.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        raise ValueError("colour must be #rgb or #rrggbb: %r" % c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def luminance(c):
    def lin(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in _rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


def passes(fg, bg, large=False, ui=False):
    need = 3.0 if (large or ui) else 4.5
    return ratio(fg, bg) >= need


if __name__ == "__main__":
    import sys
    f, b = sys.argv[1:3]
    print("%s:1 %s" % (ratio(f, b), "pass" if passes(f, b) else "fail (normal text)"))
