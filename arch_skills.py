"""Skills: short Markdown instruction packets the harness pulls into a prompt only when the task calls for them.

A model on a laptop has a small context window and a short attention span. Everything it is told costs tokens and dilutes the rest, so
"tell it everything about the project" is the wrong answer. A skill is the right-sized unit: one page about one way of working, with a
line saying when it applies, loaded only for the tasks that match.

    skills/<name>.md            shipped with the harness (general practice)
    <project>/.arch/skills/*.md the project's own (how THIS codebase does migrations, releases, tests)

A skill file is Markdown with a small header:

    ---
    name: database-migrations
    description: How this project creates and applies schema changes
    when: migration, makemigrations, schema, alembic, new column
    files: app/models/*.py, migrations/*
    tier: worker
    ---
    Write the migration first, then the model change. ...

`when:` is the trigger list; `files:` narrows a skill to tasks that touch those paths; `tier:` offers it only to that model tier (a long
skill is wasted on a 1B router). Everything except `name` is optional, and a file with no header at all still works -- its first heading
becomes the name and its first paragraph the description, because a person should be able to drop a note in the folder and have it count.

Which skills were loaded is recorded, and a skill that was in front of the model when a run passed its gates earns a point. Ties are broken
by that score, so the skills a machine actually benefits from float to the top of their own accord. The counters live in
`<data_dir>/skills.json`; they are evidence, never the model's opinion of itself (same rule as arch_memory).
"""
import fnmatch
import json
import os
import re

import arch_core as core

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
PROJECT_REL = os.path.join(".arch", "skills")
MAX_BODY = 6000                  # a skill longer than this is a document, not a skill
MAX_IN_PROMPT = 2                # how many may be loaded at once, by default
MIN_SCORE = 1.5                  # below this it is a coincidence of common words, not a match; measured against the shipped set
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


# ---------------------------------------------------------------- reading

def _terms(text):
    """Lowercase words plus their snake_case and camelCase parts: 'makemigrations' and 'make_migrations' should both match."""
    out = set()
    for w in _WORD.findall(text or ""):
        out.add(w.lower())
        for p in w.split("_"):
            if len(p) > 2:
                out.add(p.lower())
        for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", w):
            if len(p) > 2:
                out.add(p.lower())
    return out


def parse(text, path="", scope="shipped"):
    """One skill file -> a record, or None when there is nothing usable in it. Never raises: a bad file is skipped, not fatal."""
    text = (text or "").replace("\r\n", "\n")
    head, body = {}, text
    m = _FRONT.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, v = line.split(":", 1)
                head[k.strip().lower()] = v.strip()
    body = body.strip()
    if not body:
        return None
    name = head.get("name") or ""
    if not name:
        h = re.search(r"^#+\s*(.+)$", body, re.M)
        name = (h.group(1) if h else os.path.splitext(os.path.basename(path))[0]).strip()
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", name)[:60].strip() or "untitled"
    desc = head.get("description") or ""
    if not desc:
        for para in body.split("\n\n"):
            para = para.strip()
            if para and not para.startswith("#"):
                desc = " ".join(para.split())[:200]
                break
    trig = [t.strip().lower() for t in re.split(r"[,;]", head.get("when", "")) if t.strip()]
    files = [t.strip() for t in re.split(r"[,;]", head.get("files", "")) if t.strip()]
    tiers = [t.strip().lower() for t in re.split(r"[,;]", head.get("tier", "")) if t.strip()]
    return {"name": name, "description": desc, "when": trig, "files": files, "tiers": tiers,
            "body": body[:MAX_BODY], "path": path, "scope": scope, "truncated": len(body) > MAX_BODY,
            "terms": _terms(name + " " + desc + " " + " ".join(trig))}


def load_all(root=None):
    """Every skill the harness and this project offer. A project skill of the same name replaces the shipped one."""
    out = {}
    for folder, scope in ((SKILLS_DIR, "shipped"), (os.path.join(root, PROJECT_REL) if root else None, "project")):
        if not folder or not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(".md"):
                continue
            p = os.path.join(folder, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    rec = parse(f.read(), p, scope)
            except OSError:
                continue
            if rec:
                out[rec["name"].lower()] = rec
    _add_plugin_skills(out, root)
    return out


def _add_plugin_skills(out, root):
    """Skills from enabled plugins: key = the bare name when free, else <plugin>:<name>. Shipped/project skills win a clash."""
    try:
        import arch_plugins
        dirs = arch_plugins.skill_dirs(root)
    except Exception:
        return
    for folder, plugin in dirs:
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(".md"):
                continue
            p = os.path.join(folder, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    rec = parse(f.read(), p, "plugin")
            except OSError:
                continue
            if not rec:
                continue
            rec["plugin"] = plugin
            rec["qualified"] = f"{plugin}:{rec['name']}".lower()
            key = rec["name"].lower()
            out[key if key not in out else rec["qualified"]] = rec


# ---------------------------------------------------------------- choosing

def stats_path():
    return os.path.join(core.data_dir(), "skills.json")


def stats():
    try:
        with open(stats_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_stats(d):
    try:
        os.makedirs(os.path.dirname(stats_path()), exist_ok=True)
        core._atomic_write(stats_path(), json.dumps(d, indent=1, sort_keys=True))
        return True
    except (OSError, AttributeError):
        return False


def score(skill, goal, files=(), tier=""):
    """How well this skill fits the task. 0 means do not load it. Plain counting, no model involved."""
    if skill.get("tiers") and tier and tier.lower() not in skill["tiers"]:
        return 0.0
    gterms = _terms(goal)
    low = (goal or "").lower()
    hits = 0.0
    for t in skill.get("when") or []:
        if t and t in low:                               # a trigger phrase, matched as written
            hits += 2.0
        elif _terms(t) & gterms:
            hits += 1.0
    hits += 0.5 * len(skill.get("terms", set()) & gterms)
    if skill.get("files"):
        touched = any(fnmatch.fnmatch(f.replace("\\", "/"), pat) for f in (files or []) for pat in skill["files"])
        if not touched:
            if not hits:
                return 0.0
            hits *= 0.5                                  # the words match but the files do not: offer it, weakly
        else:
            hits += 1.5
    if skill.get("scope") == "project":
        hits *= 1.25                                     # this project's own way of doing things wins a tie
    st = stats().get(skill["name"], {})
    used, helped = float(st.get("used", 0)), float(st.get("helped", 0))
    if used >= 3:
        hits *= 0.75 + 0.5 * (helped / used)             # measured usefulness breaks ties, within +-25%
    return round(hits, 3)


def relevant(goal, root=None, files=(), tier="", limit=MAX_IN_PROMPT, budget=4000, skills=None, min_score=None):
    """-> [skill] worth loading for this task, best first, inside a character budget. Empty is a normal answer."""
    have = skills if skills is not None else load_all(root)
    ranked = sorted(((score(s, goal, files, tier), s) for s in have.values()), key=lambda x: (-x[0], x[1]["name"]))
    out, spent = [], 0
    for sc, s in ranked:
        if sc < (MIN_SCORE if min_score is None else float(min_score)) or len(out) >= max(0, limit):
            break                                            # loading a skill that does not apply costs tokens and dilutes the rest
        cost = len(s["body"]) + len(s["name"]) + 40
        if spent + cost > budget and out:
            continue
        out.append(dict(s, score=sc))
        spent += cost
    return out


def for_prompt(goal, root=None, files=(), tier="", limit=MAX_IN_PROMPT, budget=4000, min_score=None):
    """-> (text, [names]) for the prompt. The text names each skill and where it came from, so the model can tell whose rule it is."""
    picked = relevant(goal, root, files, tier, limit, budget, min_score=min_score)
    if not picked:
        return ("", [])
    parts = ["=== how this project does things ==="]
    for s in picked:
        where = "this project" if s["scope"] == "project" else "general practice"
        parts.append(f"-- {s['name']} ({where}) --\n{s['body']}")
    return ("\n\n".join(parts), [s["name"] for s in picked])


# ---------------------------------------------------------------- learning which ones help

def mark_used(names):
    if not names:
        return
    d = stats()
    for n in names:
        row = d.setdefault(n, {"used": 0, "helped": 0})
        row["used"] = int(row.get("used", 0)) + 1
    _save_stats(d)


def mark_helped(names):
    """The run that was shown these skills passed its gates. That is the only thing that counts as help."""
    if not names:
        return
    d = stats()
    for n in names:
        row = d.setdefault(n, {"used": 0, "helped": 0})
        row["helped"] = int(row.get("helped", 0)) + 1
    _save_stats(d)


# ---------------------------------------------------------------- reporting and checking

def summary(root=None):
    """For `status` and the hub: every skill with where it came from and how it has done."""
    st = stats()
    rows = []
    for s in sorted(load_all(root).values(), key=lambda x: x["name"]):
        r = st.get(s["name"], {})
        used, helped = int(r.get("used", 0)), int(r.get("helped", 0))
        rows.append({"name": s["name"], "scope": s["scope"], "description": s["description"],
                     "when": s["when"], "chars": len(s["body"]), "used": used, "helped": helped,
                     "rate": (round(100.0 * helped / used) if used else None)})
    return rows


def check(root=None):
    """Problems a person should fix, in plain words. [] when the skills are in good shape."""
    probs = []
    seen = {}
    for folder, scope in ((SKILLS_DIR, "shipped"), (os.path.join(root, PROJECT_REL) if root else None, "project")):
        if not folder or not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(".md"):
                continue
            p = os.path.join(folder, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as ex:
                probs.append(f"{fn}: cannot be read ({ex})")
                continue
            rec = parse(text, p, scope)
            if not rec:
                probs.append(f"{fn}: there is no text in it")
                continue
            if not rec["description"]:
                probs.append(f"{rec['name']}: no description, so nothing explains when it applies")
            if not rec["when"] and not rec["files"]:
                probs.append(f"{rec['name']}: no 'when:' triggers and no 'files:' patterns, so it will rarely be chosen")
            if rec["truncated"]:
                probs.append(f"{rec['name']}: longer than {MAX_BODY} characters and was cut; split it into two skills")
            key = rec["name"].lower()
            if key in seen and seen[key] == scope:
                probs.append(f"{rec['name']}: two skills in the same folder share this name")
            seen[key] = scope
    return probs
