"""The tools a model may call: look around the project, search it, read parts of it, run the tests, remember something, change files.

Every tool is a plain function `fn(e, **args) -> str`, registered in TOOLS with a one-line description and a JSON schema for its arguments
(the same schema the model is constrained to when it picks a tool). Nothing here reaches the network, nothing leaves the project: paths go
through `arch_core.safe_path`, output is capped, and a write goes through `Engine.apply`/`put_file`, so the person's review mode, the
protected paths and the undo trail all still apply.

`schema()` builds the one JSON schema a tool-loop step constrains the reply to:
    {"tool": "<name>", "args": {...}}  |  {"final": "<answer>"}
"""
import os
import re

import arch_core as core

OUTPUT_CAP = 4000
TOOLS = {}


def tool(name, desc, args=None, required=(), writes=False):
    def deco(fn):
        TOOLS[name] = {"name": name, "fn": fn, "desc": desc, "args": args or {}, "required": list(required), "writes": writes}
        return fn
    return deco


def _cap(text, cap=OUTPUT_CAP):
    text = text or ""
    return text if len(text) <= cap else text[:cap] + f"\n... [cut: {len(text) - cap} more characters]"


def _rel(e, path):
    """A path inside the project, or raises ValueError with a plain reason (the same rule the edit protocol uses)."""
    p = (path or ".").strip().strip("`'\"").replace("\\", "/")
    if p in ("", "."):
        return e.root
    return core.safe_path(e.root, p, e.cfg) if not os.path.isdir(os.path.join(e.root, p)) else os.path.join(e.root, p)


@tool("ls", "List the files in the project, or in one folder. Use this first to see what exists.",
      {"path": {"type": "string", "description": "folder inside the project; omit for the whole project"}})
def t_ls(e, path="", **_):
    files = core.list_files(e.root, e.cfg)
    if not files:
        return "the project is empty"
    want = (path or "").strip().strip("`'\"").replace("\\", "/").strip()
    want = "" if want in (".", "./", "/") else want.lstrip("./").strip("/")
    if want:
        hits = [f for f in files if f == want or f.startswith(want + "/")]
        if not hits:                       # a folder that is not there: say so AND show what is, so the next step is obvious
            tops = sorted({f.split("/")[0] + ("/" if "/" in f else "") for f in files})
            return f"there is no '{want}' here. The project contains: " + ", ".join(tops[:40])
        files = hits
    return _cap("\n".join(files[:400]) + (f"\n... and {len(files) - 400} more" if len(files) > 400 else ""))


@tool("grep", "Find lines matching a regular expression, with their file and line number. Use it for exact names.",
      {"pattern": {"type": "string", "description": "a Python regular expression"},
       "glob": {"type": "string", "description": "only files whose path contains this, e.g. '.py' or 'tests/'"}}, required=("pattern",))
def t_grep(e, pattern="", glob="", **_):
    try:
        rx = re.compile(pattern)
    except re.error as ex:
        return f"that is not a valid regular expression: {ex}"
    hits = []
    for f in core.list_files(e.root, e.cfg):
        if glob and glob not in f:
            continue
        try:
            text = core.read_text(os.path.join(e.root, f), 200000)[0]
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{f}:{i}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    break
        if len(hits) >= 200:
            break
    return _cap("\n".join(hits) or f"nothing matches {pattern!r}")


@tool("search", "Search the project's functions and notes by meaning and words (full-text index). Use it when you do not know the exact name.",
      {"query": {"type": "string", "description": "what you are looking for, in your own words"},
       "k": {"type": "integer", "description": "how many results (default 8)"}}, required=("query",))
def t_search(e, query="", k=8, **_):
    r = e.retriever()
    hits = r.search(query, top=int(k or 8)) if hasattr(r, "search") else []
    if not hits:
        return "the index found nothing; try `grep` with an exact name"
    return _cap("\n".join(f"{h['path']}:{h['start']}-{h['end']} {h['name']} (score {h['score']})" for h in hits))


@tool("read", "Read a file, or part of one. Always read before you change something.",
      {"path": {"type": "string"}, "start": {"type": "integer", "description": "first line (1-based)"}, "end": {"type": "integer"}}, required=("path",))
def t_read(e, path="", start=0, end=0, **_):
    try:
        p = _rel(e, path)
        text = core.read_text(p, 400000)[0]
    except (ValueError, OSError) as ex:
        return f"cannot read {path}: {ex}"
    rows = text.splitlines()
    a = max(1, int(start or 1))
    b = min(len(rows), int(end) if end else len(rows))
    body = "\n".join(f"{i}: {rows[i - 1]}" for i in range(a, b + 1))
    return _cap(body or "(empty file)")


@tool("outline", "The functions and classes in a file, with their line numbers. Cheaper than reading the whole file.", {"path": {"type": "string"}}, required=("path",))
def t_outline(e, path="", **_):
    try:
        return _cap(core.outline(_rel(e, path)) or "(nothing to outline)")
    except (ValueError, OSError) as ex:
        return f"cannot read {path}: {ex}"


@tool("run_tests", "Run the project's tests (or just some of them) and report what happened.",
      {"targets": {"type": "array", "items": {"type": "string"}, "description": "test files to run; omit for all"}})
def t_run_tests(e, targets=None, **_):
    st, log = core.run_tests(e.root, e.cfg, [t for t in (targets or []) if isinstance(t, str)] or None)
    return _cap(f"result: {st}\n{log}", 2500)


@tool("memory_read", "Read what was remembered about this project (decisions, lessons, gotchas).",
      {"name": {"type": "string", "description": "project.md | decisions.md | lessons.md | gotchas.md | notes.md"}}, required=("name",))
def t_memory_read(e, name="", **_):
    import arch_memory
    return _cap(arch_memory.read(e.root, name) or f"'{name}' is empty")


@tool("memory_append", "Remember ONE short, concrete thing for next time (a decision, a lesson, a trap). One line, no secrets.",
      {"name": {"type": "string", "description": "decisions.md | lessons.md | gotchas.md | notes.md"}, "line": {"type": "string"}}, required=("name", "line"), writes=True)
def t_memory_append(e, name="", line="", **_):
    import arch_memory
    r = arch_memory.append(e.root, name, line, run_id=getattr(e, "run_id", ""), say=e.say,
                           max_lines=((getattr(e, "cfg", {}) or {}).get("memory") or {}).get("max_lines"))
    return ("remembered: " + r["line"]) if r["ok"] else ("not remembered: " + r["why"])


@tool("apply", "Change files. Use the FILE:/EDIT:/DEF: formats exactly as in your instructions; the change is reviewed and can be undone.",
      {"blocks": {"type": "string", "description": "one or more FILE:/EDIT:/DEF: blocks"}}, required=("blocks",), writes=True)
def t_apply(e, blocks="", **_):
    res = e.apply(blocks, "tool")
    if any(x["status"] == "quit" for x in res):
        return "stopped by the person"
    ok = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
    bad = [f"{x['rel']}: {x['msg']}" for x in res if x["status"] == "error"]
    return _cap(("changed: " + ", ".join(ok) if ok else "nothing was applied") + ("\nrefused:\n" + "\n".join(bad) if bad else ""))


def syntax_error(rel, text):
    """The parser's complaint about `text` as the file `rel`, or "" when it parses (or the type has no parser here)."""
    ext = os.path.splitext(rel)[1].lower()
    try:
        if ext == ".py":
            import ast
            ast.parse(text)
        elif ext == ".json":
            import json
            json.loads(text)
    except SyntaxError as ex:
        return f"line {ex.lineno}: {ex.msg}"
    except ValueError as ex:
        return str(ex)[:160]
    return ""


@tool("view", "Show about 100 numbered lines of a file around one line. Use it to look closely before an edit.",
      {"path": {"type": "string"}, "line": {"type": "integer", "description": "the line to centre on (1-based)"},
       "n": {"type": "integer", "description": "how many lines (default 100, at most 200)"}}, required=("path",))
def t_view(e, path="", line=1, n=100, **_):
    n = max(10, min(int(n or 100), 200))
    a = max(1, int(line or 1) - n // 2)
    return t_read(e, path=path, start=a, end=a + n - 1)


@tool("edit_lines", "Replace lines start..end of a file with new text. The edit is refused if the file would no longer parse "
      "(Python, JSON), and you get the parser's message instead. Use view first to get the line numbers.",
      {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
       "text": {"type": "string", "description": "the new lines (may be more or fewer than replaced)"}},
      required=("path", "start", "end", "text"), writes=True)
def t_edit_lines(e, path="", start=0, end=0, text="", **_):
    try:
        p = _rel(e, path)
        old = core.read_text(p, 2_000_000)[0]
    except (ValueError, OSError) as ex:
        return f"cannot edit {path}: {ex}"
    rows = old.splitlines()
    a, b = int(start or 0), int(end or 0)
    if a < 1 or b < a - 1 or a > len(rows) + 1:
        return f"lines {start}..{end} are not in {path} (it has {len(rows)} lines)"
    new_rows = rows[:a - 1] + (text.splitlines() if text else []) + rows[b:]
    new = "\n".join(new_rows) + "\n"
    rel = os.path.relpath(p, e.root).replace("\\", "/")
    err = syntax_error(rel, new)
    if err:
        return f"refused: after this edit {path} would not parse ({err}). Nothing was changed; fix the text and try again."
    r = e.put_file(rel, new, label="edit_lines")
    return f"changed {path}: lines {a}..{b} replaced by {len(text.splitlines())} line(s)" if r is not False else "nothing was applied"


def syntax_error(rel, text):
    """The parser's complaint about `text` as the file `rel`, or "" when it parses (or the type has no parser here)."""
    ext = os.path.splitext(rel)[1].lower()
    try:
        if ext == ".py":
            import ast
            ast.parse(text)
        elif ext == ".json":
            import json
            json.loads(text)
    except SyntaxError as ex:
        return f"line {ex.lineno}: {ex.msg}"
    except ValueError as ex:
        return str(ex)[:160]
    return ""


@tool("view", "Show about 100 numbered lines of a file around one line. Use it to look closely before an edit.",
      {"path": {"type": "string"}, "line": {"type": "integer", "description": "the line to centre on (1-based)"},
       "n": {"type": "integer", "description": "how many lines (default 100, at most 200)"}}, required=("path",))
def t_view(e, path="", line=1, n=100, **_):
    n = max(10, min(int(n or 100), 200))
    a = max(1, int(line or 1) - n // 2)
    return t_read(e, path=path, start=a, end=a + n - 1)


@tool("edit_lines", "Replace lines start..end of a file with new text. The edit is refused if the file would no longer parse "
      "(Python, JSON), and you get the parser's message instead. Use view first to get the line numbers.",
      {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
       "text": {"type": "string", "description": "the new lines (may be more or fewer than replaced)"}},
      required=("path", "start", "end", "text"), writes=True)
def t_edit_lines(e, path="", start=0, end=0, text="", **_):
    try:
        p = _rel(e, path)
        old = core.read_text(p, 2_000_000)[0]
    except (ValueError, OSError) as ex:
        return f"cannot edit {path}: {ex}"
    rows = old.splitlines()
    a, b = int(start or 0), int(end or 0)
    if a < 1 or b < a - 1 or a > len(rows) + 1:
        return f"lines {start}..{end} are not in {path} (it has {len(rows)} lines)"
    new_rows = rows[:a - 1] + (text.splitlines() if text else []) + rows[b:]
    new = "\n".join(new_rows) + "\n"
    rel = os.path.relpath(p, e.root).replace("\\", "/")
    err = syntax_error(rel, new)
    if err:
        return f"refused: after this edit {path} would not parse ({err}). Nothing was changed; fix the text and try again."
    r = e.put_file(rel, new, label="edit_lines")
    return f"changed {path}: lines {a}..{b} replaced by {len(text.splitlines())} line(s)" if r is not False else "nothing was applied"


def syntax_error(rel, text):
    """The parser's complaint about `text` as the file `rel`, or "" when it parses (or the type has no parser here)."""
    ext = os.path.splitext(rel)[1].lower()
    try:
        if ext == ".py":
            import ast
            ast.parse(text)
        elif ext == ".json":
            import json
            json.loads(text)
    except SyntaxError as ex:
        return f"line {ex.lineno}: {ex.msg}"
    except ValueError as ex:
        return str(ex)[:160]
    return ""


@tool("view", "Show about 100 numbered lines of a file around one line. Use it to look closely before an edit.",
      {"path": {"type": "string"}, "line": {"type": "integer", "description": "the line to centre on (1-based)"},
       "n": {"type": "integer", "description": "how many lines (default 100, at most 200)"}}, required=("path",))
def t_view(e, path="", line=1, n=100, **_):
    n = max(10, min(int(n or 100), 200))
    a = max(1, int(line or 1) - n // 2)
    return t_read(e, path=path, start=a, end=a + n - 1)


@tool("edit_lines", "Replace lines start..end of a file with new text. The edit is refused if the file would no longer parse "
      "(Python, JSON), and you get the parser's message instead. Use view first to get the line numbers.",
      {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
       "text": {"type": "string", "description": "the new lines (may be more or fewer than replaced)"}},
      required=("path", "start", "end", "text"), writes=True)
def t_edit_lines(e, path="", start=0, end=0, text="", **_):
    try:
        p = _rel(e, path)
        old = core.read_text(p, 2_000_000)[0]
    except (ValueError, OSError) as ex:
        return f"cannot edit {path}: {ex}"
    rows = old.splitlines()
    a, b = int(start or 0), int(end or 0)
    if a < 1 or b < a - 1 or a > len(rows) + 1:
        return f"lines {start}..{end} are not in {path} (it has {len(rows)} lines)"
    new_rows = rows[:a - 1] + (text.splitlines() if text else []) + rows[b:]
    new = "\n".join(new_rows) + "\n"
    rel = os.path.relpath(p, e.root).replace("\\", "/")
    err = syntax_error(rel, new)
    if err:
        return f"refused: after this edit {path} would not parse ({err}). Nothing was changed; fix the text and try again."
    r = e.put_file(rel, new, label="edit_lines")
    return f"changed {path}: lines {a}..{b} replaced by {len(text.splitlines())} line(s)" if r is not False else "nothing was applied"


def syntax_error(rel, text):
    """The parser's complaint about `text` as the file `rel`, or "" when it parses (or the type has no parser here)."""
    ext = os.path.splitext(rel)[1].lower()
    try:
        if ext == ".py":
            import ast
            ast.parse(text)
        elif ext == ".json":
            import json
            json.loads(text)
    except SyntaxError as ex:
        return f"line {ex.lineno}: {ex.msg}"
    except ValueError as ex:
        return str(ex)[:160]
    return ""


@tool("view", "Show about 100 numbered lines of a file around one line. Use it to look closely before an edit.",
      {"path": {"type": "string"}, "line": {"type": "integer", "description": "the line to centre on (1-based)"},
       "n": {"type": "integer", "description": "how many lines (default 100, at most 200)"}}, required=("path",))
def t_view(e, path="", line=1, n=100, **_):
    n = max(10, min(int(n or 100), 200))
    a = max(1, int(line or 1) - n // 2)
    return t_read(e, path=path, start=a, end=a + n - 1)


@tool("edit_lines", "Replace lines start..end of a file with new text. The edit is refused if the file would no longer parse "
      "(Python, JSON), and you get the parser's message instead. Use view first to get the line numbers.",
      {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
       "text": {"type": "string", "description": "the new lines (may be more or fewer than replaced)"}},
      required=("path", "start", "end", "text"), writes=True)
def t_edit_lines(e, path="", start=0, end=0, text="", **_):
    try:
        p = _rel(e, path)
        old = core.read_text(p, 2_000_000)[0]
    except (ValueError, OSError) as ex:
        return f"cannot edit {path}: {ex}"
    rows = old.splitlines()
    a, b = int(start or 0), int(end or 0)
    if a < 1 or b < a - 1 or a > len(rows) + 1:
        return f"lines {start}..{end} are not in {path} (it has {len(rows)} lines)"
    new_rows = rows[:a - 1] + (text.splitlines() if text else []) + rows[b:]
    new = "\n".join(new_rows) + "\n"
    rel = os.path.relpath(p, e.root).replace("\\", "/")
    err = syntax_error(rel, new)
    if err:
        return f"refused: after this edit {path} would not parse ({err}). Nothing was changed; fix the text and try again."
    r = e.put_file(rel, new, label="edit_lines")
    return f"changed {path}: lines {a}..{b} replaced by {len(text.splitlines())} line(s)" if r is not False else "nothing was applied"


def describe(names=None):
    """The tool list for the prompt: one line each, plain words first."""
    out = []
    for n, t in TOOLS.items():
        if names and n not in names:
            continue
        args = ", ".join(f"{k}" + ("" if k in t["required"] else "?") for k in t["args"])
        out.append(f"- {n}({args}): {t['desc']}")
    return "\n".join(out)


def schema(names=None):
    """The JSON schema a tool-loop reply is constrained to: one tool call, or the final answer."""
    names = [n for n in (names or TOOLS) if n in TOOLS]
    return {"type": "object",
            "properties": {"tool": {"type": "string", "enum": names},
                           "args": {"type": "object"},
                           "final": {"type": "string", "description": "your answer, when you are done"}},
            "additionalProperties": False}


def call(e, name, args, allowed=None):
    """Run one tool. -> (text, ok). An unknown tool or bad arguments cost a step and are explained, never crash the loop."""
    if allowed and name not in allowed:
        return f"'{name}' is not available in this step. Available: {', '.join(allowed)}", False
    t = TOOLS.get(name)
    if not t:
        return f"there is no tool called '{name}'. Available: {', '.join(TOOLS)}", False
    args = args if isinstance(args, dict) else {}
    missing = [k for k in t["required"] if not args.get(k)]
    if missing:
        return f"{name} needs: {', '.join(missing)}", False
    try:
        return t["fn"](e, **{k: v for k, v in args.items() if isinstance(k, str)}), True
    except TypeError as ex:
        return f"{name}: wrong arguments ({ex})", False
    except Exception as ex:                                        # a tool must never take the run down
        return f"{name} failed: {type(ex).__name__}: {str(ex)[:200]}", False
