"""Axis maths: 'nice' tick steps (1, 2, 5 x 10^n) and a linear scale."""
import math


def nice_ticks(lo, hi, count=5):
    """-> list of tick values covering [lo, hi], evenly spaced on a 1-2-5 step."""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / max(count, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while True:
        ticks.append(round(v, 10))
        if v >= hi - step * 1e-9:
            break
        v += step
    return ticks


def linear(d0, d1, r0, r1):
    span = (d1 - d0) or 1
    return lambda v: r0 + (v - d0) * (r1 - r0) / span
