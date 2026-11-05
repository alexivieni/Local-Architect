"""Reply budgets learned from what each kind of call really produced.

The fixed cap (1536 tokens for structured answers, 3072 for code) is right for the longest replies and far too loose for most: a
model stuck repeating itself writes until the cap. Per (role, step, structured) this keeps the last 200 generated-token counts and
proposes cap = p95 * 2 + 128, never below 1024 (512 for structured replies) and never above the fixed cap. It only proposes a cap after MIN_SAMPLES replies,
and proposes NOTHING (the fixed cap stands) when any of the last 20 replies hit its limit: a truncated reply is the one mistake a
budget must not cause. Off in benchmarks, the self-tests, and when behavior.reply_budget = off.
"""
import json
import os
import re
import threading

KEEP, MIN_SAMPLES, RECENT = 200, 30, 20
_LOCK = threading.Lock()
_MEMO = {"data": None, "path": None}


def _path():
    import arch_core
    return os.path.join(arch_core.data_dir(), "reply_budget.json")


def _load():
    p = _path()
    if _MEMO["path"] != p or _MEMO["data"] is None:
        try:
            with open(p, encoding="utf-8") as f:
                _MEMO["data"] = json.load(f)
        except (OSError, ValueError):
            _MEMO["data"] = {}
        _MEMO["path"] = p
    return _MEMO["data"]


def key(role, label, structured):
    step = re.sub(r"[-_]?(?:try|cand|sample)\d+$", "", str(label or role or ""))
    step = re.sub(r"\d+$", "", step)
    return f"{role}|{step}|{'json' if structured else 'text'}"


def off(cfg=None):
    if os.environ.get("ARCHITECT_SELFTEST") == "1" or os.environ.get("ARCHITECT_EVAL") == "1":
        return True              # benchmarks and self-tests measure the fixed limits
    return str(((cfg or {}).get("behavior") or {}).get("reply_budget", "auto")) == "off"


def record(k, gen_tokens, truncated):
    """After a call: remember how long the reply was, and whether it hit its limit."""
    if gen_tokens is None:
        return
    with _LOCK:
        d = _load()
        e = d.setdefault(k, {"n": [], "cut": []})
        e["n"] = (e["n"] + [int(gen_tokens)])[-KEEP:]
        e["cut"] = (e["cut"] + [1 if truncated else 0])[-KEEP:]
        try:
            tmp = _path() + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, _path())
        except OSError:
            pass


def cap(k, fixed):
    """-> a learned cap (int) below `fixed`, or None to keep the fixed cap."""
    e = _load().get(k) or {}
    n = e.get("n") or []
    if len(n) < MIN_SAMPLES or any((e.get("cut") or [])[-RECENT:]):
        return None
    s = sorted(n)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    floor = 512 if k.endswith("|json") else 1024
    c = max(floor, int(p95 * 2.0) + 128)
    return c if c < fixed else None


def report():
    rows = []
    for k, e in sorted(_load().items()):
        n = e.get("n") or []
        if n:
            s = sorted(n)
            rows.append(f"{k:<40} {len(n):>4} replies  p50 {s[len(s) // 2]:>5}  p95 {s[int(0.95 * (len(s) - 1))]:>5}  "
                        f"cut {sum(e.get('cut') or [])}  learned cap {cap(k, 10 ** 9) or '-'}")
    return "\n".join(rows) or "No replies recorded yet."


if __name__ == "__main__":
    print(report())
