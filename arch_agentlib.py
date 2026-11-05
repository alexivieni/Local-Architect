"""The agent library: briefs that worked, kept as reusable templates (agent_templates/*.md), filled in and handed to the fleet.

A template is one Markdown file: a small header between two `---` lines, then the brief itself with {slots} for the parts that
change from job to job. Header lines are `key: value`:

    name        the file name without .md (checked)
    title       a short display name
    kind        agent (a brief for one worker) or footer (text appended to other briefs, e.g. `common`)
    purpose     one sentence: what the agent does
    why         why it is shaped the way it is (shown by explain())
    mode        acceptEdits (edits its own files) or plan (read-only)
    model       empty = the fleet's default model
    budget_usd  the default spend cap for one run (a cap, not an estimate: jobs measured here came in at 10-25% of it)
    files       comma-separated files it owns; slots allowed ({module}.py)
    tests       `;`-separated test commands the FLEET runs afterwards (the worker cannot run commands)
    footer      common (append the common footer) or none
    source      which real brief(s) the template was cut from
    slot.<k>    plain words for slot k
    example.<k> an example value for slot k (explain() shows it; the self-test fills every template with them)
    default.<k> the value used when the caller gives none

Slots may not be called `name` or `folder` (the library's own arguments; load() refuses them).
`{{` and `}}` in a template stand for literal braces. Nothing here starts an agent or spends anything: `to_fleet_plan` only
builds the list that `arch_fleet.launch` takes, and refuses it (ValueError, plain words) when `arch_fleet.plan_check` would.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.join(HERE, "agent_templates")
NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")
SLOT_RE = re.compile(r"\{\{|\}\}|\{([a-z][a-z0-9_]*)\}")
LEFTOVER_RE = re.compile(r"(?<!\{)\{[a-z][a-z0-9_]*\}(?!\})")
KINDS = ("agent", "footer")
MODES = ("acceptEdits", "plan")
FOOTERS = ("common", "none")
SHARED = {"app": "Architect"}            # slots every template may use without the caller naming them
RESERVED = ("name", "folder")            # fill(name, folder=..., **slots): a slot with one of these names would never arrive


# ---- reading -------------------------------------------------------------------------------------------------------------------
def _folder(folder):
    return os.path.abspath(folder or FOLDER)


def _slots_in(text):
    """Slot names in the order they first appear (escaped braces are not slots)."""
    seen = []
    for m in SLOT_RE.finditer(text or ""):
        k = m.group(1)
        if k and k not in seen:
            seen.append(k)
    return seen


def _split(raw, name):
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{name}: a template starts with a '---' header line")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError(f"{name}: the header has no closing '---' line") from None
    return lines[1:end], "\n".join(lines[end + 1:]).strip("\n")


def load(name, folder=None):
    """One template -> dict (header fields, slots, examples, defaults, body, placeholders). ValueError in plain words."""
    name = str(name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"{name[:40]!r} is not a template name (lower-case letters, digits and _)")
    path = os.path.join(_folder(folder), name + ".md")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        raise ValueError(f"no template called {name!r} (see list_templates())") from None
    head, body = _split(raw, name)
    t = {"name": name, "path": path, "title": name, "kind": "agent", "purpose": "", "why": "", "mode": "acceptEdits",
         "model": None, "budget_usd": 0.0, "files": [], "tests": [], "footer": "common", "source": "",
         "slots": {}, "examples": {}, "defaults": {}, "body": body}
    for line in head:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{name}: header line {line.strip()[:60]!r} is not 'key: value'")
        key, val = (s.strip() for s in line.split(":", 1))
        for prefix, bucket in (("slot.", "slots"), ("example.", "examples"), ("default.", "defaults")):
            if key.startswith(prefix):
                t[bucket][key[len(prefix):]] = val
                break
        else:
            if key == "files":
                t["files"] = [s.strip() for s in val.split(",") if s.strip()]
            elif key == "tests":
                t["tests"] = [s.strip() for s in val.split(";") if s.strip()]
            elif key == "budget_usd":
                try:
                    t["budget_usd"] = float(val or 0)
                except ValueError:
                    raise ValueError(f"{name}: budget_usd must be a number (got {val[:20]!r})") from None
            elif key == "model":
                t["model"] = val or None
            elif key in ("name", "title", "kind", "purpose", "why", "mode", "footer", "source"):
                t[key] = val
            else:
                raise ValueError(f"{name}: unknown header key {key[:30]!r}")
    if t["name"] != name:
        raise ValueError(f"{name}: the header says name {t['name']!r}; it must match the file name")
    if t["kind"] not in KINDS:
        raise ValueError(f"{name}: kind must be agent or footer")
    if t["mode"] not in MODES:
        raise ValueError(f"{name}: mode must be acceptEdits or plan")
    if t["footer"] not in FOOTERS:
        raise ValueError(f"{name}: footer must be common or none")
    if t["kind"] == "agent" and not (t["budget_usd"] > 0 and t["files"]):
        raise ValueError(f"{name}: an agent template needs a budget_usd above 0 and the files it owns")
    if not t["body"].strip():
        raise ValueError(f"{name}: the brief is empty")
    t["placeholders"] = _slots_in("\n".join([t["body"], ", ".join(t["files"]), "; ".join(t["tests"])]))
    clash = [k for k in t["placeholders"] + list(t["slots"]) + list(t["examples"]) + list(t["defaults"]) if k in RESERVED]
    if clash:
        raise ValueError(f"{name}: slot name {clash[0]!r} is taken by the library's own arguments; call it something else")
    return t


def list_templates(folder=None):
    """-> [{name, title, kind, purpose, mode, budget_usd, files, tests}] sorted by name; a broken file shows its error."""
    try:
        names = sorted(n[:-3] for n in os.listdir(_folder(folder)) if n.endswith(".md") and NAME_RE.fullmatch(n[:-3]))
    except OSError:
        return []
    out = []
    for n in names:
        try:
            t = load(n, folder)
            out.append({k: t[k] for k in ("name", "title", "kind", "purpose", "mode", "budget_usd", "files", "tests")})
        except ValueError as e:
            out.append({"name": n, "title": n, "kind": "broken", "purpose": "", "error": str(e)})
    return out


# ---- filling -------------------------------------------------------------------------------------------------------------------
def _values(t, given):
    vals = dict(SHARED)
    vals.update(t["defaults"])
    unknown = sorted(k for k in given if k not in t["placeholders"] and k not in SHARED)
    if unknown:
        raise ValueError(f"{t['name']}: no slot called {', '.join(unknown)} (slots: {', '.join(t['placeholders']) or 'none'})")
    for k, v in given.items():
        if v is None:
            continue
        vals[k] = ", ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
    missing = [k for k in t["placeholders"] if not str(vals.get(k, "")).strip()]
    if missing:
        raise ValueError(f"{t['name']}: fill in {', '.join(missing)}")
    return vals


def _sub(text, vals):
    def rep(m):
        tok = m.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        return vals[m.group(1)]
    return SLOT_RE.sub(rep, text or "")


def fill_parts(name, folder=None, **values):
    """-> {name, brief, files, tests, budget_usd, mode, model}: everything the fleet needs from one filled template."""
    t = load(name, folder)
    vals = _values(t, values)
    body = _sub(t["body"], vals).strip()
    brief = body
    if t["footer"] == "common" and t["kind"] == "agent":
        brief += "\n\n" + fill("common", folder=folder)
    files = []                                                   # a slot may stand for several files ("a.py, b.py")
    for f in t["files"]:
        for part in _sub(f, vals).split(","):
            if part.strip() and part.strip() not in files:
                files.append(part.strip())
    return {"name": t["name"], "body": body, "brief": brief, "files": files,
            "tests": [_sub(c, vals) for c in t["tests"]], "budget_usd": t["budget_usd"], "mode": t["mode"],
            "model": t["model"], "kind": t["kind"]}


def fill(name, folder=None, **values):
    """-> the brief text, slots filled (and the common footer appended when the template asks for it)."""
    if name == "common" or (load(name, folder)["kind"] == "footer"):
        t = load(name, folder)
        return _sub(t["body"], _values(t, values)).strip()
    return fill_parts(name, folder=folder, **values)["brief"]


def leftovers(text):
    """Slots still unfilled in a text (the self-test's check, usable before a launch)."""
    return sorted(set(LEFTOVER_RE.findall(text or "")))


# ---- the fleet -----------------------------------------------------------------------------------------------------------------
def _item(it):
    if isinstance(it, str):
        return {"template": it}
    if isinstance(it, (list, tuple)) and len(it) == 2:
        return {"template": it[0], "values": dict(it[1] or {})}
    if isinstance(it, dict) and it.get("template"):
        return dict(it)
    raise ValueError("each item is a template name, (name, values) or {'template': name, 'values': {...}, ...}")


def to_fleet_plan(items, total_cap_usd=20.0, folder=None):
    """[{template, values, name?, budget_usd?, model?, mode?, files?, after?, kind?, flow?}] -> the plan for arch_fleet.launch.
    Refused (ValueError, the plan_check problems joined) when the fleet would refuse it; nothing is started here."""
    import arch_fleet as fl
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("give at least one template")
    plan, used = [], set()
    for raw in items:
        it = _item(raw)
        parts = fill_parts(it["template"], folder=folder, **dict(it.get("values") or {}))
        if parts["kind"] != "agent":
            raise ValueError(f"{it['template']}: a footer is appended to briefs; it is not an agent")
        name = str(it.get("name") or parts["name"]).strip()
        base, n = name, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        kind = it.get("kind") or "claude"
        mode = it.get("mode") or parts["mode"]
        files = list(it.get("files") or parts["files"])
        agent = {"name": name, "files": files}
        if kind == "local":
            if mode != "acceptEdits":
                raise ValueError(f"{name}: a read-only template cannot run as a local agent (local agents edit their files)")
            agent.update(kind="local", goal=parts["body"])     # the fleet adds its own local footer (owned files)
            if it.get("flow"):
                agent["flow"] = it["flow"]
            if it.get("budget_usd"):
                agent["budget_usd"] = float(it["budget_usd"])
        else:
            agent.update(kind=kind, brief=parts["brief"], mode=mode,
                         budget_usd=float(it["budget_usd"]) if it.get("budget_usd") is not None else parts["budget_usd"])
        model = it.get("model") or parts["model"]
        if model:
            agent["model"] = model
        if it.get("after"):
            agent["after"] = it["after"]
        plan.append(agent)
    problems = fl.plan_check(plan, total_cap_usd)
    if problems:
        raise ValueError("; ".join(problems))
    return plan


def fleet_tests(items, folder=None):
    """The test commands the chosen templates ask for, once each, in order, only those arch_fleet would accept."""
    import arch_fleet as fl
    out = []
    for raw in items:
        it = _item(raw)
        for c in fill_parts(it["template"], folder=folder, **dict(it.get("values") or {}))["tests"]:
            if c not in out and fl.command_problem(c) is None:
                out.append(c)
    return out


# ---- plain words ---------------------------------------------------------------------------------------------------------------
def explain(name, folder=None):
    """-> plain words: what the agent does, why it is shaped that way, what goes into it."""
    t = load(name, folder)
    if t["kind"] == "footer":
        return (f"{t['title']}: {t['purpose']}\n\nWhy: {t['why']}\n\nIt has no slots and owns no files: the library appends it "
                "to every brief whose header says 'footer: common'.")
    out = [f"{t['title']} -- {t['purpose']}", ""]
    if t["why"]:
        out += ["Why it is shaped this way: " + t["why"], ""]
    out.append("What goes into it:")
    for k in t["placeholders"]:
        desc = t["slots"].get(k) or ("shared by every template" if k in SHARED else "")
        ex = t["examples"].get(k) or t["defaults"].get(k) or SHARED.get(k)
        out.append(f"  - {k}: {desc}" + (f" (e.g. {ex})" if ex else ""))
    if not t["placeholders"]:
        out.append("  - nothing: it is used as written")
    out += ["",
            "It owns: " + ", ".join(t["files"]) + ". Every other file is off limits while it runs.",
            ("It only reads and reports; it changes no file." if t["mode"] == "plan"
             else "It edits its own files. It cannot run commands, so it does not judge its own work."),
            f"Default spend cap: ${t['budget_usd']:.2f} (the run stops there; measured jobs used 10-25% of their caps).",
            "Afterwards the fleet runs: " + ("; ".join(t["tests"]) if t["tests"] else "no tests (read-only work)") + ".",
            "Model: " + (t["model"] or "the fleet's default") + "."]
    if t["footer"] == "common":
        out.append("The common footer (shared rules: CLAUDE.md, own files only, no commits, write tests, report) is appended.")
    if t["source"]:
        out.append("Cut from: " + t["source"] + ".")
    return "\n".join(out)


def main(argv=None):
    """python arch_agentlib.py list | explain NAME | fill NAME key=value ..."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "list":
        for t in list_templates():
            print(f"  {t['name']:<18} {t.get('kind', ''):<7} ${t.get('budget_usd') or 0:<5} {t.get('purpose') or t.get('error', '')}")
        return 0
    try:
        if argv[0] == "explain" and len(argv) == 2:
            print(explain(argv[1]))
            return 0
        if argv[0] == "fill" and len(argv) >= 2:
            vals = dict(a.split("=", 1) for a in argv[2:] if "=" in a)
            print(fill(argv[1], **vals))
            return 0
    except ValueError as e:
        print(f"  {e}")
        return 1
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
