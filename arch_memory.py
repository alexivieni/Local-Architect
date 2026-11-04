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


def for_prompt(root, budget=1800, top=None):
    """What goes into every prompt's stable prefix: the project note, then the best-earned lessons and gotchas.
    -> (text, [the line texts it used]) so the engine can count them as used and later confirm them."""
    parts, used = [], []
    proj = read(root, "project.md") or _legacy_profile(root)
    if proj.strip():
        parts.append(proj.strip()[:budget])
    left = max(0, budget - len(parts[0] if parts else ""))
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
