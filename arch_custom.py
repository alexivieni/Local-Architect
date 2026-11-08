"""Your versions of agents, crew workflows, flows and prompts: edit, check, save, try, go back.

Everything the app ships (agents/*.md, workflows/*.yaml, flows/*.yaml, prompts/<flow>/*.md) is the BUILT-IN version and is never
written from the hub. A change you make is saved here instead, under <data dir>/custom/<kind>/, and wins over the built-in file of
the same name everywhere it is looked up (arch_squad for agents and crew workflows, arch_dataflow for flows and prompts). So:
  - an update of the app never overwrites your changes, and the install's MANIFEST stays clean;
  - "reset to built-in" is always one step (your file goes; its versions stay);
  - every save is checked first by the same validator the run uses (nothing invalid is written), and is kept as a version you can
    go back to (versions(), restore()), up to KEEP per item.

    read(kind, name)            -> {text, source: "custom"|"built-in"|"new", builtin_text, versions, path}
    check(kind, name, text)     -> [problems]              (empty = it can be saved)
    save(kind, name, text, note)-> {ok, problems, path, version}
    restore(kind, name, vid)    -> save() of that version's text (itself a new version: nothing is ever lost)
    reset(kind, name)           -> remove your file; the built-in one (if any) is used again
    listing(kind)               -> [{name, source, builtin}]

The per-project launch profile (how "Start my project" runs it) lives with the project: <project>/.arch/launch.json, with its own
versions in <project>/.arch/launch_history/ (launch_read / launch_save / launch_restore).
"""
import hashlib
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP = 50
KINDS = {
    "agents": {"ext": ".md", "builtin": os.path.join(HERE, "agents"), "name": r"[a-z][a-z0-9_-]{0,40}"},
    "workflows": {"ext": ".yaml", "builtin": os.path.join(HERE, "workflows"), "name": r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"},
    "flows": {"ext": ".yaml", "builtin": os.path.join(HERE, "flows"), "name": r"[a-z][a-z0-9_]{1,40}"},
    "prompts": {"ext": ".md", "builtin": os.path.join(HERE, "prompts"), "name": r"[a-z0-9_][a-z0-9_.-]{0,60}/[a-z0-9_][a-z0-9_.-]{0,60}"},
}


def _data_dir():
    import arch_core
    return arch_core.data_dir()


def root_dir():
    return os.path.join(_data_dir(), "custom")


def custom_dir(kind):
    _kind(kind)
    return os.path.join(root_dir(), kind)


def _kind(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
    return KINDS[kind]


def clean_name(kind, name):
    """The item's name without its extension; ValueError for anything that could leave the folder."""
    k = _kind(kind)
    n = str(name or "").strip().replace("\\", "/")
    if n.endswith(k["ext"]):
        n = n[: -len(k["ext"])]
    if not re.fullmatch(k["name"], n) or ".." in n.split("/"):
        raise ValueError(f"'{str(name)[:60]}' is not a valid {kind[:-1]} name")
    return n


def custom_path(kind, name):
    n = clean_name(kind, name)
    return os.path.join(custom_dir(kind), *n.split("/")) + KINDS[kind]["ext"]


def builtin_path(kind, name):
    """The shipped file for this name, or None. Agents may ship as .md or .yaml."""
    n = clean_name(kind, name)
    base = os.path.join(KINDS[kind]["builtin"], *n.split("/"))
    for ext in ((".md", ".yaml") if kind == "agents" else (KINDS[kind]["ext"],)):
        if os.path.isfile(base + ext):
            return base + ext
    return None


def effective_path(kind, name):
    """The file a run reads for this name: yours when you saved one, else the built-in one, else None."""
    p = custom_path(kind, name)
    return p if os.path.isfile(p) else builtin_path(kind, name)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, TypeError):
        return None


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- checking: the same validators a run uses

def _prompt_problems(text):
    probs = []
    if not text.strip():
        probs.append("the instructions are empty")
    if "{feedback}" in text:
        last = [ln for ln in text.splitlines() if ln.strip()][-1:]
        if not last or "{feedback}" not in last[0]:
            probs.append("{feedback} must be on the last line: the part that changes on every try goes last, so the model server can "
                         "reuse everything before it (prefix cache)")
    return probs


def check(kind, name, text):
    """[] when `text` can be saved as `name`, else plain-words problems. Nothing is written."""
    n = clean_name(kind, name)
    text = str(text or "")
    if len(text) > 400_000:
        return ["too long (400 KB at most)"]
    if kind == "prompts":
        return _prompt_problems(text)
    import arch_ci
    if kind == "agents":
        import arch_squad
        try:
            d = arch_squad.parse_agent_md(text) if text.lstrip().startswith("---") else arch_ci.load_yaml(text)
        except Exception as ex:
            return [f"it does not read: {ex}"]
        if not isinstance(d, dict):
            return ["it does not read as an agent definition"]
        if str(d.get("name") or n) != n:
            return [f"the definition is called {d.get('name')!r}, not {n!r} (rename the file instead, or change the name: line)"]
        d.setdefault("name", n)
        return list(arch_squad.validate(d))
    try:
        doc = arch_ci.load_yaml(text)
    except Exception as ex:
        return [f"it does not read as YAML: {ex}"]
    if not isinstance(doc, dict):
        return ["it does not read as a YAML mapping"]
    if kind == "workflows":
        import arch_squad
        return [str(p) for p in (arch_squad.validate_workflow(doc) or [])]
    import arch_dataflow
    if str(doc.get("id") or "") != n:
        return [f"its id: is {doc.get('id')!r}; it must be {n!r} (the file name)"]
    probs = [str(p) for p in arch_dataflow.validate(doc)]
    for s in arch_dataflow.validate_steps(doc):
        if s.get("prompt") and not os.path.isfile(arch_dataflow.prompt_path(n, s["prompt"])):
            probs.append(f"step {s.get('id')}: no instructions file {s['prompt']!r} (add it under the flow's prompts first)")
    return probs


# ---------------------------------------------------------------- versions

def _hist_dir(kind, name):
    n = clean_name(kind, name)
    return os.path.join(root_dir(), ".history", kind, n.replace("/", "__"))


def _index(kind, name):
    p = os.path.join(_hist_dir(kind, name), "index.json")
    try:
        with open(p, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _add_version(kind, name, text, note):
    d = _hist_dir(kind, name)
    os.makedirs(d, exist_ok=True)
    idx = _index(kind, name)
    t = time.time()
    vid = time.strftime("%Y%m%d_%H%M%S", time.localtime(t)) + f"_{int((t % 1) * 1e6):06d}"
    _write(os.path.join(d, vid + KINDS[kind]["ext"]), text)
    idx.append({"id": vid, "t": round(t, 3), "note": str(note or "")[:200], "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
                "bytes": len(text.encode("utf-8"))})
    for old in idx[:-KEEP]:                      # oldest beyond KEEP leave; the newest KEEP always stay
        try:
            os.remove(os.path.join(d, old["id"] + KINDS[kind]["ext"]))
        except OSError:
            pass
    idx = idx[-KEEP:]
    _write(os.path.join(d, "index.json"), json.dumps(idx, indent=1))
    return vid


def versions(kind, name):
    """Newest first: [{id, t, note, sha, bytes}]."""
    return list(reversed(_index(kind, name)))


def version_text(kind, name, vid):
    if not re.fullmatch(r"\d{8}_\d{6}_\d{6}", str(vid or "")):
        raise ValueError("that is not a version id")
    t = _read(os.path.join(_hist_dir(kind, name), vid + KINDS[kind]["ext"]))
    if t is None:
        raise ValueError("no such version (it may have been dropped: the newest %d are kept)" % KEEP)
    return t


# ---------------------------------------------------------------- the operations

def read(kind, name):
    n = clean_name(kind, name)
    cp, bp = custom_path(kind, n), builtin_path(kind, n)
    mine, base = _read(cp), _read(bp)
    src = "custom" if mine is not None else ("built-in" if base is not None else "new")
    return {"kind": kind, "name": n, "text": mine if mine is not None else (base or ""), "source": src, "builtin": base is not None,
            "builtin_text": base, "path": cp if mine is not None else bp, "versions": versions(kind, n)}


def save(kind, name, text, note=""):
    """Check, then write your version and keep it as a version. -> {ok, problems, path, version}."""
    n = clean_name(kind, name)
    text = str(text or "").replace("\r\n", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    probs = check(kind, n, text)
    if probs:
        return {"ok": False, "problems": probs, "name": n}
    p = custom_path(kind, n)
    if _read(p) == text:
        return {"ok": True, "problems": [], "path": p, "version": None, "unchanged": True, "name": n}
    if not _index(kind, n):
        base = _read(builtin_path(kind, n))     # the first change keeps the starting point too, so "go back" can reach it
        if base is not None:
            _add_version(kind, n, base, "the built-in version, before your first change")
    _write(p, text)
    vid = _add_version(kind, n, text, note or "saved")
    _changed(kind)
    return {"ok": True, "problems": [], "path": p, "version": vid, "name": n}


def restore(kind, name, vid):
    return save(kind, name, version_text(kind, name, vid), note=f"restored version {vid}")


def reset(kind, name):
    """Your file goes (its versions stay); the built-in one is used again. For an item that has no built-in this removes it."""
    n = clean_name(kind, name)
    p = custom_path(kind, n)
    had = os.path.isfile(p)
    if had:
        os.remove(p)
        _changed(kind)
    return {"ok": True, "removed": had, "builtin": builtin_path(kind, n) is not None, "name": n}


def listing(kind):
    d = custom_dir(kind)
    ext = KINDS[kind]["ext"]
    out = []
    for dp, _dns, fns in os.walk(d):
        for fn in sorted(fns):
            if fn.endswith(ext):
                rel = os.path.relpath(os.path.join(dp, fn), d).replace("\\", "/")[: -len(ext)]
                try:
                    out.append({"name": clean_name(kind, rel), "source": "custom", "builtin": builtin_path(kind, rel) is not None})
                except ValueError:
                    pass
    return out


def customized_flow_ids():
    """Flow ids with a flow file or a prompt of yours: these run from their YAML (and your prompts) whatever the engine setting."""
    ids = {x["name"] for x in listing("flows")}
    ids |= {x["name"].split("/")[0] for x in listing("prompts")}
    return ids


def _changed(kind):
    """A saved flow or prompt must be seen by the next run in this process (the hub keeps one registry)."""
    if kind in ("flows", "prompts"):
        try:
            import arch_core
            import arch_dataflow
            import arch_flows
            mode = ((arch_core.load_config().get("flows") or {}).get("engine") or "legacy")
            arch_dataflow.register(arch_flows.FLOWS, mode)
        except Exception:
            pass


# ---------------------------------------------------------------- the launch profile (how "Start my project" runs a project)

LAUNCH_NETS = ("", "offline", "practice", "open")


def _launch_path(root):
    return os.path.join(os.path.abspath(root), ".arch", "launch.json")


def launch_read(root):
    try:
        with open(_launch_path(root), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    hist = os.path.join(os.path.abspath(root), ".arch", "launch_history")
    vs = sorted((x[:-5] for x in os.listdir(hist) if x.endswith(".json")), reverse=True) if os.path.isdir(hist) else []
    return {"profile": d if isinstance(d, dict) else {}, "versions": vs[:KEEP]}


def launch_check(p):
    probs = []
    if not str(p.get("cmd") or "").strip():
        probs.append("a run command is needed (for example: python app.py, or npm start)")
    if len(str(p.get("cmd") or "")) > 1000:
        probs.append("the run command is too long")
    port = p.get("port")
    if port not in (None, ""):
        try:
            if not 1 <= int(port) <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            probs.append("the port is a number from 1 to 65535")
    env = p.get("env") or {}
    if not isinstance(env, dict):
        probs.append("the environment is NAME=value lines")
    else:
        for k in env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(k)):
                probs.append(f"'{str(k)[:30]}' is not a variable name")
    if str(p.get("network") or "") not in LAUNCH_NETS:
        probs.append("network is one of: offline, practice, open (or blank = the project's setting)")
    return probs


def launch_save(root, p, note=""):
    p = {"cmd": str(p.get("cmd") or "").strip(), "port": (int(p["port"]) if str(p.get("port") or "").strip() else None),
         "env": {str(k): str(v) for k, v in (p.get("env") or {}).items()} if isinstance(p.get("env"), dict) else p.get("env"),
         "network": str(p.get("network") or ""), "note": str(note or "")[:200]}
    probs = launch_check(p)
    if probs:
        return {"ok": False, "problems": probs}
    path = _launch_path(root)
    _write(path, json.dumps(p, indent=1))
    hist = os.path.join(os.path.dirname(path), "launch_history")
    t = time.time()
    vid = time.strftime("%Y%m%d_%H%M%S", time.localtime(t)) + f"_{int((t % 1) * 1e6):06d}"
    _write(os.path.join(hist, vid + ".json"), json.dumps(p, indent=1))
    old = sorted(x for x in os.listdir(hist) if x.endswith(".json"))
    for x in old[:-KEEP]:
        try:
            os.remove(os.path.join(hist, x))
        except OSError:
            pass
    return {"ok": True, "problems": [], "version": vid, "profile": p}


def launch_restore(root, vid):
    if not re.fullmatch(r"\d{8}_\d{6}_\d{6}", str(vid or "")):
        raise ValueError("that is not a version id")
    p = os.path.join(os.path.abspath(root), ".arch", "launch_history", vid + ".json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        raise ValueError("no such version")
    return launch_save(root, d, note=f"restored version {vid}")
