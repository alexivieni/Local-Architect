"""arch_ourtasks: "OUR data" -- what the person kept asking for while building this app with Claude, turned into training tasks.

Legitimate source = the PERSON'S OWN words: their messages in the Claude Code transcripts of this project and the goals of their hub
runs. Never an assistant reply, never a tool result, never a pasted block (anything with <...> tags), never a system reminder.
`mine()` reads those messages, strips what must not travel (profanity-only lines, paths with the user name, URLs, secrets via
arch_dataset.has_secret) and clusters them into recurring intents. `tasks()` hands back the training tasks of evals/families/ours.py
for those intents: fixture project + the request in a casual voice + hidden code judge + a code-generated reference solution.
No sentence of any transcript, and nothing Claude wrote, is ever copied into a goal or a solution.

    python arch_ourtasks.py            # counts, intents, example goals
"""
import glob
import importlib.util
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# intent id -> (plain label, regex over the lower-cased message). A message may belong to several intents.
INTENTS = {
    "ui_clearer": ("make the UI clearer", r"\b(ui|ux|gui|clearer|hard to use|beautiful|flagship|design|visual|layout|label|readable|confusing)\b"),
    "cache_more": ("cache more", r"\bcach(e|ed|es|ing)\b"),
    "measure_speed": ("measure speed", r"\b(speed|tok/s|how fast|slow|benchmark|measure|latency|faster|tokens per)\b"),
    "gpu_placement": ("fix GPU placement", r"\b(gpu|vram|igpu|dgpu|offload|layers|on the card)\b"),
    "add_tests": ("add tests", r"\b(tests?|testing|tested|selftest)\b"),
    "add_command": ("add a plugin / command / flag", r"\b(plugins?|commands?|slash|cli|flags?)\b"),
    "plain_errors": ("say errors in plain words", r"\b(plain words|plain english|simple words|error messages?|explain (it )?briefly|errors?)\b"),
}

PROFANITY = {"fuck", "fucking", "fuciking", "fucked", "shit", "ass", "bitch", "damn", "dammit", "wtf", "bro", "lol", "aids", "crap", "bullshit", "fk", "fck"}
_TAG = re.compile(r"<[A-Za-z/!?][^>]{0,300}>")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'`]+|/(?:Users|home)/[^\s\"'`]+|~[\\/][^\s\"'`]+|@\"[^\"]*\")")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)
_WORD = re.compile(r"[A-Za-z']+")
MAX_LEN = 1500                  # a longer "message" is a pasted document, not a request


def _arch_dataset():
    spec = importlib.util.spec_from_file_location("ourtasks_arch_dataset", os.path.join(HERE, "arch_dataset.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _usernames():
    out = {os.path.basename(os.path.expanduser("~")).lower(), (os.environ.get("USERNAME") or os.environ.get("USER") or "").lower()}
    return {u for u in out if len(u) >= 3}


def clean(text):
    """-> the cleaned message, or None when nothing worth keeping is left (tags, secrets, profanity-only, paths-only...)."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t or len(t) > MAX_LEN or _TAG.search(t) or "pasted_content" in t or t.startswith("[Request interrupted"):
        return None
    if _arch_has_secret(t):
        return None
    t = _PATH.sub(" ", t)
    t = _URL.sub(" ", t)
    for u in _usernames():
        t = re.sub(re.escape(u), " ", t, flags=re.I)
    lines = []
    for ln in t.splitlines():
        words = _WORD.findall(ln)
        real = [w for w in words if w.lower() not in PROFANITY]
        if len(real) < 2:                                         # profanity-only (or a lone word) line
            continue
        lines.append(" ".join(ln.split()))
    out = " ".join(lines).strip()
    return out if len(out.split()) >= 3 else None


_DS = None


def _arch_has_secret(t):
    global _DS
    if _DS is None:
        _DS = _arch_dataset()
    return _DS.has_secret(t)


def transcript_dir(project=None):
    """~/.claude/projects/<project path with separators and colons turned into dashes>."""
    root = os.path.abspath(project or os.path.dirname(HERE))
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    # the person's project is the folder above this app (architect_v12); Claude Code names the folder after it
    for cand in (root, os.path.dirname(root)):
        slug = re.sub(r"[^A-Za-z0-9]", "-", cand)
        if os.path.isdir(os.path.join(base, slug)):
            return os.path.join(base, slug)
    return os.path.join(base, re.sub(r"[^A-Za-z0-9]", "-", os.path.dirname(root)))


def person_messages(tdir):
    """-> (raw string messages written by the person, entries seen). Only type 'user', string content, not meta, not a sidechain."""
    msgs, seen = [], 0
    for f in sorted(glob.glob(os.path.join(tdir, "*.jsonl"))):
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "user":
                    continue
                seen += 1
                if d.get("isMeta") or d.get("isSidechain") or d.get("toolUseResult") is not None:
                    continue
                c = (d.get("message") or {}).get("content")
                if isinstance(c, str):
                    msgs.append(c)
    return msgs, seen


def run_goals(roots):
    out = []
    for root in roots or ():
        for p in glob.glob(os.path.join(root, ".arch", "runs", "*", "run.json")):
            try:
                with open(p, encoding="utf-8") as fh:
                    g = json.load(fh).get("goal")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(g, str) and g.strip():
                out.append(g)
    return out


def cluster(messages):
    """-> {intent: {label, count, examples}} over cleaned messages."""
    out = {k: {"label": v[0], "count": 0, "examples": []} for k, v in INTENTS.items()}
    for m in messages:
        low = m.lower()
        for k, (label, rx) in INTENTS.items():
            if re.search(rx, low):
                out[k]["count"] += 1
                if len(out[k]["examples"]) < 3:
                    sent = next((s for s in re.split(r"(?<=[.!?])\s+", m) if re.search(rx, s.lower())), m)
                    out[k]["examples"].append(sent[:140])
    return out


def mine(project=None, transcripts=None, run_roots=None):
    """-> {messages_seen, messages_kept, goals_kept, intents:{...}, kept:[cleaned texts]}. Reads only the person's own words."""
    tdir = transcripts or transcript_dir(project)
    raw, seen = person_messages(tdir) if os.path.isdir(tdir) else ([], 0)
    goals = run_goals(run_roots if run_roots is not None else [project or HERE])
    kept, dup = [], set()
    n_goals = 0
    for i, m in enumerate(raw + goals):
        c = clean(m)
        if not c or c.lower() in dup:
            continue
        dup.add(c.lower())
        kept.append(c)
        n_goals += 1 if i >= len(raw) else 0
    return {"messages_seen": seen, "messages_string": len(raw), "messages_kept": len(kept) - n_goals, "goals_kept": n_goals,
            "intents": cluster(kept), "kept": kept}


def family():
    spec = importlib.util.spec_from_file_location("ours_family", os.path.join(HERE, "evals", "families", "ours.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tasks(seed=0, mined=None, only_seen=False, include_holdout=True):
    """The ours-family tasks; with `mined`+only_seen just those whose intent the person actually voiced."""
    out = family().all_tasks(seed)
    if only_seen and mined:
        seen = {k for k, v in mined["intents"].items() if v["count"]}
        out = [t for t in out if t["intent"] in seen]
    if not include_holdout:
        out = [t for t in out if "holdout" not in t["tags"]]
    return out


def main():
    m = mine()
    ts = tasks(mined=m)
    print(f"user entries {m['messages_seen']}, string messages {m['messages_string']}, kept {m['messages_kept']} (+{m['goals_kept']} run goals)")
    for k, v in sorted(m["intents"].items(), key=lambda kv: -kv[1]["count"]):
        print(f"  {k:15} {v['count']:4}  {v['label']}")
    print(f"{len(ts)} tasks, {sum('holdout' in t['tags'] for t in ts)} held out")
    for t in ts[::5]:
        print("  -", t["goal"][:150])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
