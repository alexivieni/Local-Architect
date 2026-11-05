"""Slash commands: the things you ask for again and again, saved once and invoked by name.

    /review src/api.py
    /testgen payments.py
    /secure

A command is a Markdown file with a small header. The body is the request, with `{args}` where whatever you typed after the name goes:

    commands/<name>.md          shipped with the harness
    <project>/.arch/commands/   this project's own

    ---
    name: review
    description: Review the current changes for correctness and security
    flow: review
    ---
    Review {args} carefully. Report problems in order of severity, with the file and line for each.

`flow:` pins which flow runs it; leave it out and the router decides as usual. Nothing here can run a program -- a command expands to
words, and those words go through the same flows, budgets and checks as anything you type yourself. That is the whole safety story: a
command file you were sent is a request, never an instruction to your computer.
"""
import os
import re

COMMANDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands")
PROJECT_REL = os.path.join(".arch", "commands")
MAX_BODY = 4000
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
NAME_RX = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def parse(text, path="", scope="shipped"):
    """One command file -> a record, or None. A file with no usable body is skipped, never fatal."""
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
    name = (head.get("name") or os.path.splitext(os.path.basename(path))[0]).strip().lower()
    if not NAME_RX.match(name):
        return None
    return {"name": name, "description": (head.get("description") or body.split("\n")[0])[:160],
            "flow": (head.get("flow") or "").strip().lower(), "args_hint": (head.get("args") or "").strip(),
            "body": body[:MAX_BODY], "path": path, "scope": scope,
            "needs_args": "{args}" in body}


def load_all(root=None):
    """Every command available here. A project command of the same name replaces the shipped one."""
    out = {}
    for folder, scope in ((COMMANDS_DIR, "shipped"), (os.path.join(root, PROJECT_REL) if root else None, "project")):
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
                out[rec["name"]] = rec
    try:
        import arch_plugins
        dirs = arch_plugins.command_dirs(root)
    except Exception:
        dirs = []
    for folder, plugin in dirs:                  # enabled plugins: bare name when free, else plugin:name; shipped/project win
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(".md"):
                continue
            p = os.path.join(folder, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    rec = parse(f.read(), p, "plugin")
            except OSError:
                continue
            if rec:
                rec["plugin"] = plugin
                rec["qualified"] = f"{plugin}:{rec['name']}"
                out[rec["name"] if rec["name"] not in out else rec["qualified"]] = rec
    return out


def split(text):
    """'/review src/api.py' -> ('review', 'src/api.py'). -> (None, text) when it is not a command."""
    t = (text or "").strip()
    if not t.startswith("/"):
        return (None, text)
    rest = t[1:]
    name, _, args = rest.partition(" ")
    return (name.strip().lower(), args.strip())


def expand(text, root=None, commands=None):
    """-> {"ok", "goal", "flow", "name"} or {"ok": False, "why", "suggestions"}.

    An unknown name is answered with the closest ones rather than a shrug, because a typo should cost a second, not a search.
    """
    name, args = split(text)
    if not name:
        return {"ok": False, "why": "that is not a command", "suggestions": []}
    have = commands if commands is not None else load_all(root)
    c = have.get(name) or next((x for x in have.values() if x.get("qualified") == name), None)
    if not c:
        close = sorted(have, key=lambda n: (0 if n.startswith(name[:2]) else 1, abs(len(n) - len(name)), n))[:4]
        return {"ok": False, "why": f"there is no /{name} command here", "suggestions": close}
    if c["needs_args"] and not args:
        return {"ok": False, "why": f"/{name} needs something to work on" + (f" ({c['args_hint']})" if c["args_hint"] else ""),
                "suggestions": []}
    goal = c["body"].replace("{args}", args)
    return {"ok": True, "goal": goal.strip(), "flow": c["flow"] or "", "name": name, "description": c["description"]}


def summary(root=None):
    """For the console's own autocomplete and for `commands --list`."""
    return [{"name": c["name"], "description": c["description"], "flow": c["flow"], "scope": c["scope"],
             "needs_args": c["needs_args"], "args_hint": c["args_hint"]}
            for c in sorted(load_all(root).values(), key=lambda x: x["name"])]
