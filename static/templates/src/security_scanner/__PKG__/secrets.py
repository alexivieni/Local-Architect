"""Secret detection over plain text. Reported values are masked: the report must not leak what it found."""
import math
import re
from collections import Counter

from .findings import Finding

KEY_SHAPES = [
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]
ASSIGN = re.compile(r"""(?i)\b(\w*(?:password|passwd|secret|token|api_?key)\w*)\s*[:=]\s*["']([^"']{4,})["']""")


def entropy(s):
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def mask(value):
    return value[:2] + "*" * max(len(value) - 4, 2) + value[-2:] if len(value) > 6 else "****"


def scan_text(path, text):
    out = []
    for no, line in enumerate(text.splitlines(), start=1):
        for label, rx in KEY_SHAPES:
            m = rx.search(line)
            if m:
                out.append(Finding("SEC001", "high", path, no, f"{label}: {mask(m.group(0))}"))
        for m in ASSIGN.finditer(line):
            name, value = m.group(1), m.group(2)
            if len(value) >= 20 and entropy(value) >= 4.0:
                out.append(Finding("SEC003", "high", path, no, f"high-entropy literal in {name}: {mask(value)}"))
            else:
                out.append(Finding("SEC002", "medium", path, no, f"literal assigned to {name}"))
    return out
