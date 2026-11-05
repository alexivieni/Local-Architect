"""Parse Common/Combined Log Format lines (plus an optional trailing request time)."""
import gzip
import re
from dataclasses import dataclass
from datetime import datetime

LINE = re.compile(
    r'(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+) [^"]*" '
    r'(?P<status>\d{3}) (?P<size>\d+|-)(?: "[^"]*" "[^"]*")?(?: (?P<rt>\d+(?:\.\d+)?))?\s*$')


@dataclass
class Hit:
    ip: str
    when: datetime
    method: str
    path: str
    status: int
    size: int
    seconds: float = None


def parse_line(line):
    """-> Hit, or None when the line is not a log entry."""
    m = LINE.match(line.strip())
    if not m:
        return None
    try:
        when = datetime.strptime(m["ts"], "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None
    return Hit(m["ip"], when, m["method"], m["path"].split("?", 1)[0], int(m["status"]),
               0 if m["size"] == "-" else int(m["size"]), float(m["rt"]) if m["rt"] else None)


def open_log(path):
    opener = gzip.open if path.endswith(".gz") else open
    return opener(path, "rt", encoding="utf-8", errors="replace")


def read(lines):
    """-> (hits, number of malformed lines)."""
    hits, bad = [], 0
    for line in lines:
        if not line.strip():
            continue
        h = parse_line(line)
        if h is None:
            bad += 1
        else:
            hits.append(h)
    return hits, bad
