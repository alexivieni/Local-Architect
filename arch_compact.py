"""Auto-compaction of a run's conversation: when the next prompt would pass X% of the role's window (default 80), the OLDEST turns are
replaced by one structured digest (decisions, files touched, failures, open todos). The digest is built by code first (extractive,
deterministic: the same turns always give the same bytes); the router model may only shorten it, and its answer is used only when it is
shorter and non-empty. The stable prefix (the first `keep_prefix` messages: system text, the task) is returned as the SAME objects, so
the server's prompt-prefix cache stays valid; the newest `keep_tail` messages are kept as they are.

    msgs, info = compact(messages, budget_tokens)          # messages: [{"role": ..., "content": str}, ...]
    turns, info = compact_turns(turns, budget_tokens)      # the squad loop's list of "[step n] tool(args)\\nRESULT:..." strings

`info` is the compaction event: {compacted, before_tokens, after_tokens, removed, method, ...}. `record(e, info)` writes it to the run
(`e.compactions`, a trace line when the engine has one, a telemetry counter). Nothing here calls a model unless `router=` is given,
and nothing leaves the machine. Settings (arch_params / DEFAULTS, owned elsewhere): see `settings()`.
"""
import re
import time

MARK = "[compacted history"
CHARS_PER_TOKEN = 3.5
SECTIONS = (("decisions", "Decisions"), ("files", "Files touched"), ("failures", "Failures"), ("todos", "Open todos"))
CAPS = {"decisions": 8, "files": 20, "failures": 8, "todos": 8}
LINE_CAP = 160

_DECISION = re.compile(r"\b(decid\w*|decision|chose|chosen|we will|i will|plan:|conclusion|agreed|going with|settled on)\b", re.I)
_FAILURE = re.compile(r"\b(refused|failed|failure|error|not applied|traceback|exception|cannot|could not|FAIL)\b", re.I)
_TODO = re.compile(r"(\bTODO\b|^\s*[-*]\s*\[ \]|\bnext:|\bstill need|\bnot yet\b|\bremaining:|\bopen item)", re.I)
_PATH = r"[\w./\\-]+\.(?:py|js|ts|tsx|jsx|md|json|ya?ml|html|css|txt|toml|sh|bat|cfg|ini)"
_WRITE = [re.compile(r"\bchanged:\s*(.+)"), re.compile(r"^(?:FILE|EDIT|DEF):\s*(" + _PATH + ")", re.M),
          re.compile(r"\bedit_lines\(\{[^}]*?\"path\":\s*\"(" + _PATH + ")\""), re.compile(r"\bwritten:\s*(" + _PATH + ")"),
          re.compile(r"\[\+\]\s*(" + _PATH + r"):\s*written")]
_FILE_RX = re.compile(_PATH)


def settings():
    """The settings this module reads (cfg['compact']); the other agent adds them to arch_params.SETTINGS and arch_core.DEFAULTS."""
    return {"compact.enabled": ("bool", True, "Replace the oldest turns of a long run with a short digest"),
            "compact.pct": ("int 30..95", 80, "Compact when the next prompt would pass this % of the role's window"),
            "compact.keep_tail": ("int 1..20", 4, "Newest messages always kept verbatim"),
            "compact.use_router": ("bool", False, "Let the router model shorten the digest (code builds it first)")}


def conf(cfg):
    c = (cfg or {}).get("compact") or {}

    def num(k, d, lo, hi):
        try:
            return max(lo, min(hi, int(c.get(k, d))))
        except (TypeError, ValueError):
            return d
    return {"enabled": str(c.get("enabled", True)).lower() not in ("false", "off", "0", "no"), "pct": num("pct", 80, 30, 95),
            "keep_tail": num("keep_tail", 4, 1, 20),
            "use_router": str(c.get("use_router", False)).lower() in ("true", "on", "1", "yes")}


def tokens(text):
    return int(len(text or "") / CHARS_PER_TOKEN + 0.999)


def _text(m):
    c = m.get("content") if isinstance(m, dict) else m
    return c if isinstance(c, str) else str(c or "")


def messages_tokens(messages):
    return sum(tokens(_text(m)) + 4 for m in messages)


def should_compact(messages, budget_tokens, pct=80):
    return messages_tokens(messages) > int(budget_tokens) * pct / 100.0


def window_tokens(cfg, role="", e=None):
    """The window a role's prompt must fit: the served window when the engine knows it, else limits.num_ctx; the role profile's
    ctx_max can only lower it."""
    win = 0
    try:
        if e is not None and hasattr(e, "_served_ctx"):
            m = e.model_for(role) if role and hasattr(e, "model_for") else ""
            win = int(e._served_ctx(m) or 0)
    except Exception:
        win = 0
    if not win:
        win = int(((cfg or {}).get("limits") or {}).get("num_ctx") or 8192)
    try:
        import arch_roleprofile
        cm = (((cfg or {}).get("role_profiles") or {}).get(role) or {}).get("ctx_max") or (arch_roleprofile.shipped().get(role) or {}).get("ctx_max")
        if cm:
            win = min(win, int(cm))
    except Exception:
        pass
    return win


# ------------------------------------------------------------------ the extractive digest
def _clip(s, n=LINE_CAP):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 3] + "..."


def _add(lists, key, item):
    item = _clip(item)
    if item and item not in lists[key]:
        lists[key].append(item)


def _parse_digest(text, lists):
    """Fold an earlier digest back in, so compacting twice loses nothing the first digest kept."""
    cur = None
    for ln in text.splitlines():
        for key, title in SECTIONS:
            if ln.startswith(title + ":"):
                cur = key
                break
        else:
            if cur and ln.startswith("- "):
                _add(lists, cur, ln[2:])


def extract(chunks):
    """-> {"decisions": [], "files": [], "failures": [], "todos": []} from message texts. Deterministic."""
    lists = {k: [] for k, _ in SECTIONS}
    for text in chunks:
        if text.startswith(MARK):
            _parse_digest(text, lists)
            continue
        for rx in _WRITE:
            for m in rx.finditer(text):
                for f in _FILE_RX.findall(m.group(1)):
                    _add(lists, "files", f.replace("\\", "/"))
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("RESULT:"):
                continue
            if _FAILURE.search(s):
                _add(lists, "failures", s)
            elif _TODO.search(s):
                _add(lists, "todos", s)
            elif _DECISION.search(s):
                _add(lists, "decisions", s)
    return lists


def render(lists, removed, max_chars):
    head = f"{MARK}: {removed} earlier message(s) replaced by this digest]"
    out = [head]
    for key, title in SECTIONS:
        items = lists[key][-CAPS[key]:] if key != "files" else lists[key][:CAPS[key]]
        out.append(f"{title}: " + ("" if items else "none recorded"))
        out.extend("- " + i for i in items)
    text = "\n".join(out)
    while len(text) > max_chars and any(len(lists[k]) > 1 for k, _ in SECTIONS):   # drop the oldest item of the longest list
        k = max(lists, key=lambda x: len(lists[x]))
        lists[k] = lists[k][1:] if k != "files" else lists[k][:-1]
        return render(lists, removed, max_chars)
    return text[:max_chars]


def digest(messages, max_chars=3000, router=None):
    """-> (text, method). Code first; the router model may only replace it with something shorter."""
    chunks = [_text(m) for m in messages]
    text = render(extract(chunks), len(messages), max_chars)
    if router is not None:
        try:
            ask = ("Shorten this digest of an agent's earlier work. Keep every file name, every failure and every open todo; "
                   "keep the four headings. Answer with the digest only.\n\n" + text)
            got = str(router(ask) or "").strip()
            if got and len(got) < len(text) and got.startswith(MARK[:10]):
                return got[:max_chars], "router"
        except Exception:
            pass
    return text, "extractive"


# ------------------------------------------------------------------ the API
def compact(messages, budget_tokens, pct=80, keep_prefix=1, keep_tail=4, router=None, force=False, max_digest_tokens=None, events=None):
    """-> (messages, info). Unchanged (the same list contents) when the prompt is under pct% of budget_tokens or there is nothing
    to fold. Otherwise [prefix..., digest, tail...]: prefix and tail are the original objects; the digest is a user message."""
    messages = list(messages)
    before = messages_tokens(messages)
    limit = int(budget_tokens * pct / 100.0)
    info = {"compacted": False, "before_tokens": before, "after_tokens": before, "removed": 0, "budget_tokens": int(budget_tokens),
            "pct": pct, "limit_tokens": limit, "method": None, "kept_prefix": min(keep_prefix, len(messages)), "kept_tail": 0, "t": time.time()}
    if not force and before <= limit:
        return messages, info
    keep_prefix = max(0, min(keep_prefix, len(messages)))
    prefix = messages[:keep_prefix]
    rest = messages[keep_prefix:]
    max_chars = int((max_digest_tokens or max(120, min(1200, int(budget_tokens * 0.08)))) * CHARS_PER_TOKEN)
    tail_n = max(0, min(keep_tail, len(rest)))
    while True:
        middle, tail = rest[:len(rest) - tail_n], rest[len(rest) - tail_n:]
        if not middle:
            return messages, info                     # nothing old enough to fold
        text, method = digest(middle, max_chars, router)
        new = prefix + [{"role": "user", "content": text}] + tail
        after = messages_tokens(new)
        if after <= limit or tail_n <= 1 or force:
            break
        tail_n -= 1                                   # still too big: keep fewer verbatim turns
    if after >= before:                               # a digest that does not shrink the prompt is not worth a break in the history
        return messages, info
    info.update(compacted=True, after_tokens=after, removed=len(middle), method=method, kept_tail=len(tail), digest_chars=len(text))
    if events is not None:
        events.append(info)
    return new, info


def compact_turns(turns, budget_tokens, pct=80, keep_tail=4, router=None, force=False, events=None):
    """The squad tool loop keeps its history as strings. -> (turns, info); a digest becomes the first string."""
    msgs = [{"role": "user", "content": t} for t in turns]
    new, info = compact(msgs, budget_tokens, pct=pct, keep_prefix=0, keep_tail=keep_tail, router=router, force=force, events=events)
    return [m["content"] for m in new], info


def router_from_engine(e):
    """A callable(prompt) -> text on the router tier of an Engine, or None when the engine has no way to ask."""
    if not hasattr(e, "call"):
        return None

    def ask(prompt):
        r = e.call("router", prompt, "You shorten notes. Plain text only.")
        return r.get("text", "") if isinstance(r, dict) else ""
    return ask


def record(e, info):
    """Write the compaction event where the run can see it. Never raises."""
    if not info.get("compacted"):
        return
    try:
        lst = getattr(e, "compactions", None)
        if lst is None:
            lst = e.compactions = []
        lst.append(info)
        if hasattr(e, "_trace"):
            e._trace({"type": "compaction", "t": info["t"], "before_tokens": info["before_tokens"], "after_tokens": info["after_tokens"],
                      "removed": info["removed"], "method": info["method"]})
        if hasattr(e, "say"):
            e.say(f"[i] compacted {info['removed']} earlier message(s): {info['before_tokens']} -> {info['after_tokens']} tokens")
    except Exception:
        pass
    try:
        import arch_telemetry
        arch_telemetry.emit("compact", before=info["before_tokens"], after=info["after_tokens"], removed=info["removed"], method=info["method"])
    except Exception:
        pass
