"""One converter per type. Each takes a raw string and returns the clean string, or raises ValueError.
An empty result means 'no value' (the caller decides whether that is allowed)."""
import re
from datetime import datetime

EMPTY = {"", "na", "n/a", "null", "none", "-", "?"}
TRUE = {"true", "t", "yes", "y", "1"}
FALSE = {"false", "f", "no", "n", "0"}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y")


def squash(raw):
    return " ".join((raw or "").split())


def is_empty(raw):
    return squash(raw).lower() in EMPTY


def text(raw, case=None):
    s = squash(raw)
    return {"title": s.title(), "lower": s.lower(), "upper": s.upper()}.get(case, s)


def number(raw):
    s = squash(raw).replace(",", "").replace(" ", "")
    pct = s.endswith("%")
    v = float(s.rstrip("%"))
    v = v / 100 if pct else v
    return f"{v:.10g}"


def integer(raw):
    v = float(number(raw))
    if v != int(v):
        raise ValueError(f"{raw!r} is not a whole number")
    return str(int(v))


def date(raw):
    s = squash(raw)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"{raw!r} is not a date I know")


def boolean(raw):
    s = squash(raw).lower()
    if s in TRUE:
        return "true"
    if s in FALSE:
        return "false"
    raise ValueError(f"{raw!r} is not yes/no")


def email(raw):
    s = squash(raw).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", s):
        raise ValueError(f"{raw!r} is not an email address")
    return s


CONVERTERS = {"text": text, "number": number, "int": integer, "date": date, "bool": boolean, "email": email}
