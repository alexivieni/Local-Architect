"""Markdown memory the model reads and writes: what this project is, what was decided, what went wrong before.

Per project, in `.arch/memory/`:
  project.md    written by `scan` (what the code is, how it is tested). Read into every prompt's stable prefix.
  decisions.md  one line per decision the team took, with the run that took it.
  lessons.md    one line per thing that worked ("tests import from the package root; add conftest.py first").
  gotchas.md    one line per trap ("EDIT blocks on legacy/*.py fail: CRLF line endings").
Per machine, in `hub_data/memory/`: machine.md, tuning.md (what tune measured and chose), models.md, lessons.md.

Rules that keep it honest and small:
  * the model may only APPEND one line at a time, through the `memory_append` tool, and only to the files above;
  * a line is dated and carries the run id, is capped at 300 characters, and never contains a secret-looking value;
  * a file is capped (400 lines): the oldest unconfirmed lines are dropped first;
  * a lesson carries `(confirmed N)`. `confirm()` raises it when a run that had the line in its prompt passed its gates; a line that
    is never confirmed after `PRUNE_AFTER` uses is dropped. Memory grows from evidence, not from the model's opinion of itself.
"""
import os
import re
import time

import arch_core as core

FILES = ("project.md", "decisions.md", "lessons.md", "gotchas.md", "notes.md")
MACHINE_FILES = ("machine.md", "tuning.md", "models.md", "lessons.md")
MAX_LINES = 400
MAX_LEN = 300
PRUNE_AFTER = 5
TOP_LINES = 12
SECRET_RX = re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|private[_-]?key)\b\s*[:=]\s*\S{8,}")
_LINE = re.compile(r"^- \[(?P<date>[\d-]+)(?: (?P<run>[\w.:-]+))?\] (?P<text>.*?)(?: \(used (?P<used>\d+), confirmed (?P<conf>\d+)\))?$")


def project_dir(root):
    return os.path.join(root, ".arch", "memory")


def machine_dir():
    return os.path.join(core.data_dir(), "memory")


def path_for(root, name):
    """A memory file's path, or None when the name is not one of ours (nothing else is ever written). 'gotchas', 'Gotchas.md' and
    'gotchas.md' all mean the same file: a model should not lose a lesson over a file extension."""
    name = (name or "").strip().strip("`'\"").replace("\\", "/").lower()
    if name.startswith("machine/"):
        fname = _match(name.split("/", 1)[1], MACHINE_FILES)
        return os.path.join(machine_dir(), fname) if fname else None
    fname = _match(name, FILES)
    return os.path.join(project_dir(root), fname) if fname else None


def _match(name, known):
    name = name.strip("/")
    for k in known:
        if name == k or name == os.path.splitext(k)[0]:
            return k
    return None


def read(root, name, limit=20000):
    p = path_for(root, name)
    if not p or not os.path.isfile(p):
        return ""
    try:
        return core.read_text(p, limit)[0]
    except OSError:
        return ""


def lines(root, name):
    """-> [{"raw", "text", "date", "run", "used", "confirmed"}] for the bullet lines of a memory file."""
    out = []
    for raw in read(root, name).splitlines():
        m = _LINE.match(raw.strip())
        if m:
            out.append({"raw": raw.strip(), "text": m.group("text"), "date": m.group("date"), "run": m.group("run"),
                        "used": int(m.group("used") or 0), "confirmed": int(m.group("conf") or 0)})
    return out


def append(root, name, text, run_id="", say=None, max_lines=None):
    """Add one dated line. -> {"ok", "path", "line"} or {"ok": False, "why"}. Never edits or deletes an existing line."""
    p = path_for(root, name)
    if not p:
        return {"ok": False, "why": f"'{name}' is not a memory file; use one of: {', '.join(FILES)} or machine/<name>"}
    text = " ".join(str(text or "").split())[:MAX_LEN].strip(" -")
    if len(text) < 8:
        return {"ok": False, "why": "that is too short to be worth remembering"}
    if SECRET_RX.search(text):
        return {"ok": False, "why": "that line looks like it contains a credential; memory never stores secrets"}
    have = lines(root, name)
    if any(l["text"].lower() == text.lower() for l in have):
        return {"ok": True, "path": p, "line": text, "duplicate": True}
    line = f"- [{time.strftime('%Y-%m-%d')}{(' ' + run_id) if run_id else ''}] {text} (used 0, confirmed 0)"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    body = read(root, name)
    if not body.strip():
        body = f"# {os.path.splitext(os.path.basename(p))[0].replace('_', ' ').title()}\n\n"
    body = body.rstrip("\n") + "\n" + line + "\n"
    body = _trim(body, max_lines)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    if say:
        say(f"    [memory] {name}: {text[:80]}")
    return {"ok": True, "path": p, "line": line}


def _trim(body, max_lines=None):
    """Keep the file small: drop the oldest never-confirmed lines first, then the oldest lines."""
    MAX = int(max_lines or MAX_LINES)
    rows = body.splitlines()
    if len(rows) <= MAX:
        return "\n".join(rows) + "\n"
    head = [r for r in rows if not r.strip().startswith("- ")]
    bullets = [r for r in rows if r.strip().startswith("- ")]
    keep = []
    for r in bullets:
        m = _LINE.match(r.strip())
        used = int((m.group("used") if m else 0) or 0)
        conf = int((m.group("conf") if m else 0) or 0)
        keep.append((conf > 0, -used if conf == 0 else 0, r))
    keep.sort(key=lambda x: (not x[0], x[1]))                     # confirmed first, then the least used
    bullets = [r for _, _, r in keep[: MAX - len(head)]]
    return "\n".join(head + bullets) + "\n"


def mark_used(root, names, used_texts):
    """Count that these lines were put in front of the model. Called when a prompt included them."""
    for name in names:
        rows = lines(root, name)
        if not rows:
            continue
        changed = False
        body = read(root, name)
        for l in rows:
            if l["text"] in used_texts:
                new = _rewrite(l, used=l["used"] + 1)
                body = body.replace(l["raw"], new)
                changed = True
        if changed:
            p = path_for(root, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body if body.endswith("\n") else body + "\n")


def confirm(root, names, used_texts, say=None, prune_after=None):
    """A run that had these lines in its prompt passed its gates: the lines earned a confirmation. Lines never confirmed after
    PRUNE_AFTER uses are dropped -- memory shrinks when it is not helping."""
    for name in names:
        rows = lines(root, name)
        if not rows:
            continue
        body = read(root, name)
        for l in rows:
            if l["text"] in used_texts:
                body = body.replace(l["raw"], _rewrite(l, confirmed=l["confirmed"] + 1))
            elif l["confirmed"] == 0 and l["used"] >= int(prune_after or PRUNE_AFTER):
                body = body.replace(l["raw"] + "\n", "")
                if say:
                    say(f"    [memory] dropped a line that never helped: {l['text'][:60]}")
        p = path_for(root, name)
        if p and os.path.isfile(p):
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body if body.endswith("\n") else body + "\n")


def _rewrite(l, used=None, confirmed=None):
    u = l["used"] if used is None else used
    c = l["confirmed"] if confirmed is None else confirmed
    base = re.sub(r" \(used \d+, confirmed \d+\)$", "", l["raw"])
    return f"{base} (used {u}, confirmed {c})"


ARCHITECT_MD = "ARCHITECT.md"
ARCHITECT_MD_CHARS = 1500
_PROPOSED_HEAD = "# Proposed for ARCHITECT.md (accept one to move it there)\n\n"


def architect_md(root, limit=ARCHITECT_MD_CHARS):
    """The project memory file the person owns: the top `limit` chars of <root>/ARCHITECT.md ("" when absent)."""
    p = os.path.join(root, ARCHITECT_MD)
    if not os.path.isfile(p):
        return ""
    try:
        return core.read_text(p, 200000)[0].strip()[:limit]
    except OSError:
        return ""


def _proposed_path(root):
    return os.path.join(project_dir(root), "proposed.md")


def proposed(root):
    """-> the proposed learned lines (strings), oldest first."""
    try:
        with open(_proposed_path(root), encoding="utf-8") as f:
            return [l[2:].strip() for l in f.read().splitlines() if l.startswith("- ") and l[2:].strip()]
    except OSError:
        return []


def propose(root, text, run_id=""):
    """A learned line goes to .arch/memory/proposed.md only; ARCHITECT.md never changes until accept(). -> {"ok", ...}."""
    text = " ".join(str(text or "").split())[:MAX_LEN].strip(" -")
    if len(text) < 8:
        return {"ok": False, "why": "that is too short to be worth remembering"}
    if SECRET_RX.search(text):
        return {"ok": False, "why": "that line looks like it contains a credential; memory never stores secrets"}
    if text.lower() in (x.lower() for x in proposed(root)):
        return {"ok": True, "duplicate": True, "line": text}
    p = _proposed_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    new = not os.path.isfile(p)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write(_PROPOSED_HEAD)
        f.write(f"- {text}\n")
    return {"ok": True, "line": text, "path": p}


def accept(root, which):
    """The explicit step: move one proposed line (its text or 1-based number) into ARCHITECT.md under '## Learned'. -> {"ok", ...}."""
    rows = proposed(root)
    if isinstance(which, int) or str(which).strip().isdigit():
        i = int(which) - 1
        text = rows[i] if 0 <= i < len(rows) else None
    else:
        text = next((r for r in rows if r.lower() == str(which).strip().lower()), None)
    if not text:
        return {"ok": False, "why": "no such proposed line"}
    p = os.path.join(root, ARCHITECT_MD)
    body = core.read_text(p, 200000)[0] if os.path.isfile(p) else ""
    if "## Learned" not in body:
        body = (body.rstrip("\n") + "\n\n" if body.strip() else "# Project memory\n\n") + "## Learned\n"
    body = body.rstrip("\n") + f"\n- {text}\n"
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    left = [r for r in rows if r != text]
    with open(_proposed_path(root), "w", encoding="utf-8", newline="\n") as f:
        f.write(_PROPOSED_HEAD + "".join(f"- {r}\n" for r in left))
    return {"ok": True, "line": text, "path": p}


def for_prompt(root, budget=1800, top=None, architect_md=True):
    """What goes into every prompt's stable prefix: the project note, then the best-earned lessons and gotchas.
    -> (text, [the line texts it used]) so the engine can count them as used and later confirm them."""
    parts, used = [], []
    amd = globals()["architect_md"](root) if architect_md else ""   # the person's own file leads the stable prefix
    proj = read(root, "project.md") or _legacy_profile(root)
    if proj.strip():
        parts.append(proj.strip()[:budget])
    left = max(0, budget - len(parts[0] if parts else ""))
    if amd:
        parts.insert(0, "Project notes (ARCHITECT.md):" + chr(10) + amd)
    for name, title in (("lessons.md", "What worked here before"), ("gotchas.md", "Traps in this project")):
        rows = sorted(lines(root, name), key=lambda l: (-l["confirmed"], -l["used"]))[:int(top or TOP_LINES)]
        if not rows or left < 120:
            continue
        block = [f"{title}:"]
        for l in rows:
            line = f"- {l['text']}" + (f" (confirmed {l['confirmed']}x)" if l["confirmed"] else "")
            if sum(len(x) for x in block) + len(line) > left:
                break
            block.append(line)
            used.append(l["text"])
        if len(block) > 1:
            parts.append("\n".join(block))
            left -= sum(len(x) for x in block)
    return ("\n\n".join(parts), used)


def _legacy_profile(root):
    p = os.path.join(root, ".arch", "profile.md")                 # written by `scan` before v13; still read
    try:
        return core.read_text(p, 20000)[0] if os.path.isfile(p) else ""
    except OSError:
        return ""


def summary(root):
    """For the hub and `status`: how much memory this project and this machine carry."""
    out = {"project": {}, "machine": {}}
    for n in FILES:
        rows = lines(root, n) if root else []
        if rows or (root and os.path.isfile(path_for(root, n) or "")):
            out["project"][n] = {"lines": len(rows), "confirmed": sum(1 for l in rows if l["confirmed"])}
    for n in MACHINE_FILES:
        p = os.path.join(machine_dir(), n)
        if os.path.isfile(p):
            out["machine"][n] = {"bytes": os.path.getsize(p)}
    return out


# ----------------------------------------------------------------------------------------------- consolidation (nightly-safe)
CONSOLIDATE_FILES = ("lessons.md", "gotchas.md", "decisions.md", "notes.md")
SIMILAR = 0.8                 # word-set Jaccard at or above which two lines are the same lesson
PROPOSE_AT = 2                # a lesson confirmed this many times is worth proposing for ARCHITECT.md
_WORD = re.compile(r"[a-z0-9_]+")
_STOP = frozenset("a an the to of in on for and or is are be it this that with as at by from use using".split())


def _words(text):
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOP)


def _similar(a, b):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / len(wa | wb) >= SIMILAR


def _write_atomic(path, body):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, path)


class _Lock:
    """One consolidation at a time (a nightly job and a person must not interleave). A lock older than 10 minutes is stale."""

    def __init__(self, root):
        self.path = os.path.join(project_dir(root), ".consolidate.lock")
        self.held = False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 600:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                return self
        return self

    def __exit__(self, *a):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass


def consolidate(root, dry_run=False, prune_after=None, propose_at=None, files=None):
    """Tidy the project's learned lines. Safe to run every night: it is idempotent, takes a lock, writes each file atomically and
    never edits ARCHITECT.md (it only PROPOSES lines into .arch/memory/proposed.md, for the person to accept).
      1. merge duplicate lessons (same words, or Jaccard >= SIMILAR): the better-earned line survives, `used` and `confirmed` are summed;
      2. drop lines that were never confirmed after PRUNE_AFTER uses (the same rule `confirm` applies);
      3. cut a file over MAX_LINES the usual way (`_trim`);
      4. propose lessons confirmed >= PROPOSE_AT times that ARCHITECT.md and the proposals do not already say.
    -> {"ok", "merged", "dropped", "trimmed", "proposed", "files": {name: {"before", "after"}}, "skipped"}. dry_run changes nothing."""
    rep = {"ok": True, "merged": 0, "dropped": 0, "trimmed": 0, "proposed": 0, "files": {}, "skipped": "", "dry_run": bool(dry_run), "proposed_lines": []}
    if not root or not os.path.isdir(project_dir(root)):
        rep["skipped"] = "no project memory yet"
        return rep
    limit = int(prune_after or PRUNE_AFTER)
    with _Lock(root) as lk:
        if not lk.held:
            rep.update(ok=False, skipped="another consolidation is running")
            return rep
        for name in files or CONSOLIDATE_FILES:
            p = path_for(root, name)
            if not p or not os.path.isfile(p):
                continue
            body = read(root, name)
            rows = lines(root, name)
            if not rows:
                continue
            kept = []                                               # [row dict], order of first appearance
            for l in rows:
                twin = next((k for k in kept if _similar(k["text"], l["text"])), None)
                if twin is None:
                    kept.append(dict(l))
                    continue
                rep["merged"] += 1
                a, b = (twin, l) if (twin["confirmed"], twin["used"]) >= (l["confirmed"], l["used"]) else (l, twin)
                a = dict(a, used=twin["used"] + l["used"], confirmed=twin["confirmed"] + l["confirmed"])
                kept[kept.index(twin)] = a
            survivors = []
            for k in kept:
                if k["confirmed"] == 0 and k["used"] >= limit:
                    rep["dropped"] += 1
                else:
                    survivors.append(k)
            head = [r for r in body.splitlines() if not r.strip().startswith("- ")]
            new_rows = [_rewrite(k, used=k["used"], confirmed=k["confirmed"]) for k in survivors]
            new_body = _trim("\n".join(head).rstrip("\n") + "\n" + "\n".join(new_rows) + "\n", None)
            after = sum(1 for r in new_body.splitlines() if r.strip().startswith("- "))
            rep["trimmed"] += max(0, len(new_rows) - after)
            rep["files"][name] = {"before": len(rows), "after": after}
            if new_body != (body if body.endswith("\n") else body + "\n") and not dry_run:
                _write_atomic(p, new_body)
        # proposals: well-confirmed lessons that ARCHITECT.md does not say yet
        amd = architect_md(root, 200000)
        have = list(proposed(root))
        for l in sorted(lines(root, "lessons.md") + lines(root, "gotchas.md"), key=lambda l: -l["confirmed"]):
            if l["confirmed"] < int(propose_at or PROPOSE_AT):
                continue
            if l["text"].lower() in amd.lower():
                continue
            if any(_similar(l["text"], x) for x in have) or any(_similar(l["text"], x.lstrip("- ")) for x in amd.splitlines() if x.strip()):
                continue
            rep["proposed_lines"].append(l["text"])
            have.append(l["text"])
            if not dry_run:
                propose(root, l["text"])
        rep["proposed"] = len(rep["proposed_lines"])
    return rep
