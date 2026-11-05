"""Numbers from hits. Percentiles are nearest-rank (a value that actually occurred)."""
import math
from collections import Counter


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def per_minute(hits):
    return Counter(h.when.strftime("%Y-%m-%d %H:%M") for h in hits)


def spikes(hits, factor=3.0):
    """Minutes with more than `factor` times the median per-minute traffic."""
    pm = per_minute(hits)
    if len(pm) < 3:
        return []
    med = percentile(list(pm.values()), 50)
    return sorted(m for m, n in pm.items() if n > factor * med)


def summarize(hits, bad=0, top=10):
    classes = Counter(f"{h.status // 100}xx" for h in hits)
    times = [h.seconds for h in hits if h.seconds is not None]
    total = len(hits)
    return {
        "requests": total,
        "malformed_lines": bad,
        "status": dict(sorted(classes.items())),
        "error_rate": round(classes.get("5xx", 0) / total, 4) if total else 0.0,
        "top_paths": Counter(h.path for h in hits).most_common(top),
        "top_clients": Counter(h.ip for h in hits).most_common(top),
        "bytes": sum(h.size for h in hits),
        "latency_s": {p: percentile(times, int(p[1:])) for p in ("p50", "p95", "p99")},
        "spike_minutes": spikes(hits),
    }
