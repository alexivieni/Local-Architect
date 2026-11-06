"""Plugins: one folder that bundles agents, workflows, skills, commands and hooks as a single way of working.

Like Claude Code's plugins, but for the models on this machine. Standard library only, no network.

Where they live (first found wins a name clash, so a shipped plugin can never be shadowed):
  <install>/plugins/<name>/             shipped examples (off until enabled)
  <data_dir>/plugins/<name>/            installed on this machine (`plugins install PATH|ZIP`)
  <project>/.arch/plugins/<name>/       arrived with a project's code

A plugin folder:
  plugin.json   {"name", "version", "description", "requires": {"architect": ">=13", "tiers": [...]},
                 "provides": {...informational...}, "settings_defaults": {...}}
  agents/*.md|*.yaml  workflows/*.yaml  skills/*.md  commands/*.md  hooks.json

State: <data_dir>/plugins.json  {name: {enabled, trusted_fingerprint, source, installed_at}}.

Trust follows the hooks rule exactly: a sha256 over every file of the plugin; ANY change revokes trust. Trust gates only the
executable parts (hooks, and agents with code:). Markdown skills/commands and plain YAML agents/workflows are inert data and
load once the plugin is enabled. Each item is checked by the existing validators; a plugin with problems is listed with its
reasons, never fatal. trusted() answers True / False / None (None = the files could not be read, so no fingerprint).

Items resolve as <plugin>:<name>; the bare name works too when nothing shipped or earlier already uses it.
"""
import builtins
import hashlib
import io
import json
import os
import re
import shutil
import time
import zipfile

import arch_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED_DIR = os.path.join(HERE, "plugins")
PROJECT_REL = os.path.join(".arch", "plugins")
MANIFEST = "plugin.json"
NAME_RX = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
KINDS = ("agents", "workflows", "skills", "commands")
MAX_FILES = 300
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
ARCHITECT_VERSION = 13


# ---------------------------------------------------------------- places and state

def machine_dir():
    return os.path.join(core.data_dir(), "plugins")


def state_file():
    return os.path.join(core.data_dir(), "plugins.json")


def _state():
    return core.load_json(state_file()) or {}


def _save_state(d):
    try:
        os.makedirs(os.path.dirname(state_file()), exist_ok=True)
        core._atomic_write(state_file(), json.dumps(d, indent=1, sort_keys=True))
        return True
    except (OSError, AttributeError):
        return False


def _emit(kind, **fields):
    """Telemetry when arch_telemetry exists (built in parallel); silent otherwise."""
    try:
        import arch_telemetry
        arch_telemetry.emit(kind, **fields)
    except Exception:
        pass


def _cfg(root=None, cfg=None):
    if cfg is not None:
        return cfg
    try:
        return core.load_config(root)
    except Exception:
        return {}


def _settings(cfg):
    p = (cfg or {}).get("plugins") or {}
    return {"enabled": p.get("enabled", True) is not False, "trust_project": bool(p.get("trust_project", False)),
            "disabled": [str(x).lower() for x in (p.get("disabled") or [])]}


def _roots(root=None):
    out = [(SHIPPED_DIR, "shipped"), (machine_dir(), "machine")]
    if root:
        out.append((os.path.join(root, PROJECT_REL), "project"))
    return out


def discover(root=None):
    """-> {name: {"dir", "scope", "manifest", "problems"}} for every plugin folder. Never raises."""
    found = {}
    for base, scope in _roots(root):
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            d = os.path.join(base, fn)
            if not os.path.isdir(d):
                continue
            man, probs = read_manifest(d)
            name = (man or {}).get("name") or fn
            if name in found:                     # shipped > machine > project: first wins
                continue
            if man and man.get("name") != fn:
                probs.append(f"plugin.json name {man.get('name')!r} does not match the folder {fn!r}")
            found[name] = {"dir": d, "scope": scope, "manifest": man or {}, "problems": probs}
    return found


# ---------------------------------------------------------------- manifest + validation

def read_manifest(folder):
    """-> (manifest or None, [problems])."""
    p = os.path.join(folder, MANIFEST)
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
    except OSError:
        return None, [f"no {MANIFEST}"]
    except ValueError as e:
        return None, [f"{MANIFEST} is not valid JSON: {e}"]
    return m, validate_manifest(m)


def validate_manifest(m):
    p = []
    if not isinstance(m, dict):
        return ["plugin.json must be an object"]
    if not NAME_RX.match(str(m.get("name") or "")):
        p.append("name: lower-case letters, digits and - (got %r)" % str(m.get("name"))[:40])
    if not isinstance(m.get("version"), str) or not re.match(r"^\d+(\.\d+){0,3}$", m.get("version") or ""):
        p.append("version: like 1.0.0")
    if not isinstance(m.get("description", ""), str) or not m.get("description"):
        p.append("description: one sentence is required")
    req = m.get("requires") or {}
    if not isinstance(req, dict):
        p.append("requires: an object")
    else:
        a = str(req.get("architect") or "")
        mm = re.match(r"^>=\s*(\d+)$", a)
        if a and not mm:
            p.append("requires.architect: like '>=13'")
        elif mm and int(mm.group(1)) > ARCHITECT_VERSION:
            p.append(f"requires.architect {a}: this is Architect {ARCHITECT_VERSION}")
        if not isinstance(req.get("tiers", []), builtins.list):
            p.append("requires.tiers: a list")
    if not isinstance(m.get("settings_defaults", {}), dict):
        p.append("settings_defaults: an object")
    extra = sorted(set(m) - {"name", "version", "description", "requires", "provides", "settings_defaults", "author", "homepage"})
    if extra:
        p.append("unknown keys: " + ", ".join(extra))
    return p


def _files(folder, kind, ext):
    d = os.path.join(folder, kind)
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith(ext)]


def _read_text(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def contents(folder):
    """What a plugin folder provides, each item checked by the validator it will meet when it loads.

    -> {"agents": [names], "workflows": [...], "skills": [...], "commands": [...], "hooks": [{"on","run"}], "code_agents": [...],
        "problems": [...]}"""
    out = {"agents": [], "workflows": [], "skills": [], "commands": [], "hooks": [], "code_agents": [], "problems": []}
    agent_paths = {}
    try:
        import arch_ci
        import arch_squad
    except Exception as e:                                           # pragma: no cover - those ship with the install
        arch_ci = arch_squad = None
        out["problems"].append(f"agents/workflows not checked: {e}")
    for p in _files(folder, "agents", ".yaml") + _files(folder, "agents", ".md"):
        rel = os.path.relpath(p, folder)
        if arch_squad is None:
            continue
        try:
            d = arch_squad.load_agent(p)                 # .md (Claude Code format) and .yaml alike; ValueError lists the problems
            probs = []
        except Exception as e:
            probs = [str(e)]
            d = {}
        if probs:
            out["problems"].append(f"{rel}: " + "; ".join(probs))
            continue
        out["agents"].append(d["name"])
        agent_paths[d["name"]] = p
        if d.get("code"):
            out["code_agents"].append(d["name"])
    for p in _files(folder, "workflows", ".yaml"):
        rel = os.path.relpath(p, folder)
        if arch_squad is None:
            continue
        try:
            w = arch_ci.load_yaml(_read_text(p))
            probs = arch_squad.validate_workflow(with_plugin_agents(w, agent_paths))
        except Exception as e:
            probs, w = [str(e)], {}
        if probs:
            out["problems"].append(f"{rel}: " + "; ".join(probs))
        else:
            out["workflows"].append(os.path.splitext(os.path.basename(p))[0])
    try:
        import arch_skills
        for p in _files(folder, "skills", ".md"):
            rec = arch_skills.parse(_read_text(p), p, "plugin")
            if rec:
                out["skills"].append(rec["name"])
            else:
                out["problems"].append(f"{os.path.relpath(p, folder)}: empty skill")
    except Exception as e:
        out["problems"].append(f"skills not checked: {e}")
    try:
        import arch_commands
        for p in _files(folder, "commands", ".md"):
            rec = arch_commands.parse(_read_text(p), p, "plugin")
            if rec:
                out["commands"].append(rec["name"])
            else:
                out["problems"].append(f"{os.path.relpath(p, folder)}: not a usable command (name must match {arch_commands.NAME_RX.pattern})")
    except Exception as e:
        out["problems"].append(f"commands not checked: {e}")
    hf = os.path.join(folder, "hooks.json")
    if os.path.isfile(hf):
        try:
            import arch_hooks
            rows = arch_hooks._read(hf)
            raw = json.loads(_read_text(hf))
            raw = raw.get("hooks") if isinstance(raw, dict) else raw
            if len(rows) != len(raw or []):
                out["problems"].append(f"hooks.json: {len(raw or []) - len(rows)} hook(s) unusable (need on: one of "
                                       + ", ".join(arch_hooks.POINTS) + " and run:)")
            out["hooks"] = [{"on": h["on"], "run": h["run"], "blocking": h["blocking"]} for h in rows]
        except Exception as e:
            out["problems"].append(f"hooks.json: {e}")
    return out


def with_plugin_agents(w, agent_paths):
    """A workflow whose agent: names are this plugin's own agents, rewritten to their file paths so arch_squad can load them."""
    if not isinstance(w, dict) or not agent_paths:
        return w

    def fix(x):
        if isinstance(x, dict):
            y = {k: fix(v) for k, v in x.items()}
            for k in ("agent", "panel"):
                if isinstance(y.get(k), str) and y[k] in agent_paths:
                    y[k] = agent_paths[y[k]]
            return y
        if isinstance(x, builtins.list):
            return [fix(v) for v in x]
        return x
    return fix(w)


# ---------------------------------------------------------------- trust

def fingerprint(folder):
    """sha256 over every file's relative path and bytes, sorted. '' when any file cannot be read."""
    h = hashlib.sha256()
    try:
        for dp, dns, fns in os.walk(folder):
            dns[:] = sorted(d for d in dns if d != "__pycache__")
            if any(os.path.islink(os.path.join(dp, x)) for x in dns + fns):
                return ""                                   # a link's target can change without changing these bytes
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, folder).replace(os.sep, "/")
                with open(p, "rb") as f:
                    h.update(rel.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    except OSError:
        return ""
    return h.hexdigest()[:24] if os.path.isdir(folder) else ""


def trusted(name, root=None, _found=None):
    """True = approved and unchanged; False = never approved or edited since; None = could not fingerprint."""
    found = _found if _found is not None else discover(root)
    p = found.get(name)
    if not p:
        return None
    fp = fingerprint(p["dir"])
    if not fp:
        return None
    return (_state().get(name) or {}).get("trusted_fingerprint") == fp


def trust(name, root=None, yes=True):
    """Approve this exact content. A later edit changes the fingerprint and needs approving again."""
    found = discover(root)
    if name not in found:
        return False
    st = _state()
    rec = st.setdefault(name, {})
    if yes:
        fp = fingerprint(found[name]["dir"])
        if not fp:
            return False
        rec["trusted_fingerprint"] = fp
    else:
        rec.pop("trusted_fingerprint", None)
    ok = _save_state(st)
    _emit("plugin.trust", plugin=name, trusted=bool(yes and ok))
    return ok


# ---------------------------------------------------------------- enable / disable

def is_enabled(name, root=None, cfg=None, _found=None):
    s = _settings(_cfg(root, cfg))
    if not s["enabled"] or name.lower() in s["disabled"]:
        return False
    return bool((_state().get(name) or {}).get("enabled"))


def _set_enabled(name, on, root=None):
    found = discover(root)
    if name not in found:
        return False
    st = _state()
    st.setdefault(name, {})["enabled"] = bool(on)
    st[name].setdefault("source", found[name]["scope"])
    ok = _save_state(st)
    _emit("plugin.enable" if on else "plugin.disable", plugin=name)
    return ok


def enable(name, root=None):
    return _set_enabled(name, True, root)


def disable(name, root=None):
    return _set_enabled(name, False, root)


def active(root=None, cfg=None):
    """Enabled plugins with a usable manifest -> [{"name", "dir", "scope", "trusted"}]. The one entry point the loaders use."""
    cfg = _cfg(root, cfg)
    s = _settings(cfg)
    if not s["enabled"]:
        return []
    st = _state()
    out = []
    found = discover(root)
    for name, p in found.items():
        if name.lower() in s["disabled"] or not (st.get(name) or {}).get("enabled"):
            continue
        if p["manifest"] is None or any(x.startswith("no plugin.json") or "not valid JSON" in x or x.startswith("name:")
                                        for x in p["problems"]):
            continue
        t = trusted(name, root, found)
        if p["scope"] == "project" and s["trust_project"] and t is False:
            t = True
        out.append({"name": name, "dir": p["dir"], "scope": p["scope"], "trusted": t})
    return out


def _dirs(kind, root=None, cfg=None):
    return [(os.path.join(p["dir"], kind), p["name"]) for p in active(root, cfg) if os.path.isdir(os.path.join(p["dir"], kind))]


def skill_dirs(root=None, cfg=None):
    return _dirs("skills", root, cfg)


def command_dirs(root=None, cfg=None):
    return _dirs("commands", root, cfg)


def agent_dirs(root=None, cfg=None):
    """[(folder, plugin)]. arch_squad must still skip a code: agent from a plugin where agent_allowed() is False."""
    return _dirs("agents", root, cfg)


def workflow_dirs(root=None, cfg=None):
    return _dirs("workflows", root, cfg)


def agent_allowed(defn, plugin, root=None):
    """A plain YAML agent is data; one with code: runs a program, so it needs the plugin trusted."""
    if not (defn or {}).get("code"):
        return True
    return trusted(plugin, root) is True


def hook_files(root=None, cfg=None):
    """[(hooks.json path, plugin, trusted True/False/None)] for enabled plugins."""
    return [(os.path.join(p["dir"], "hooks.json"), p["name"], p["trusted"])
            for p in active(root, cfg) if os.path.isfile(os.path.join(p["dir"], "hooks.json"))]


# ---------------------------------------------------------------- listing

def list(root=None, cfg=None):  # noqa: A001 - the shared contract names it list()
    cfg = _cfg(root, cfg)
    st = _state()
    rows = []
    found = discover(root)
    for name, p in sorted(found.items()):
        c = contents(p["dir"]) if p["manifest"] else {k: [] for k in KINDS + ("hooks", "code_agents", "problems")}
        m = p["manifest"] or {}
        rows.append({"name": name, "version": m.get("version", ""), "description": m.get("description", ""),
                     "scope": p["scope"], "dir": p["dir"], "enabled": is_enabled(name, root, cfg),
                     "trusted": trusted(name, root, found),
                     "needs_trust": bool(c["hooks"] or c["code_agents"]),
                     "provides": {"agents": c["agents"], "workflows": c["workflows"], "skills": c["skills"],
                                  "commands": c["commands"], "hooks": [h["run"] for h in c["hooks"]]},
                     "would_run": c["hooks"], "code_agents": c["code_agents"],
                     "requires": m.get("requires") or {},
                     "source": (st.get(name) or {}).get("source", p["scope"]),
                     "installed_at": (st.get(name) or {}).get("installed_at", ""),
                     "problems": p["problems"] + c["problems"]})
    return rows


def show(name, root=None):
    for r in list(root):
        if r["name"] == name:
            return r
    return None


# ---------------------------------------------------------------- install / remove / new / pack

def _safe_rel(name):
    n = name.replace("\\", "/")
    if not n or n.startswith("/") or ":" in n or "\0" in n or any(part == ".." for part in n.split("/")):
        return None
    return n


def _check_tree(folder):
    n = total = 0
    for dp, dns, fns in os.walk(folder):
        for dn in dns:
            if os.path.islink(os.path.join(dp, dn)):
                raise ValueError(f"links are not allowed in a plugin: {os.path.relpath(os.path.join(dp, dn), folder)}")
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.islink(p):
                raise ValueError(f"links are not allowed in a plugin: {os.path.relpath(p, folder)}")
            sz = os.path.getsize(p)
            n += 1
            total += sz
            if sz > MAX_FILE_BYTES:
                raise ValueError(f"{os.path.relpath(p, folder)} is larger than {MAX_FILE_BYTES // 1024} KB")
    if n > MAX_FILES or total > MAX_TOTAL_BYTES:
        raise ValueError(f"a plugin is at most {MAX_FILES} files and {MAX_TOTAL_BYTES // (1024 * 1024)} MB")


def _extract_zip(zpath, dest):
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist()
        if len(infos) > MAX_FILES:
            raise ValueError(f"the zip has more than {MAX_FILES} entries")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ValueError("the zip unpacks to more than the size cap")
        names = [i.filename for i in infos]
        bad = [n for n in names if _safe_rel(n) is None]
        if bad:
            raise ValueError("refused: path outside the plugin folder in the zip: " + ", ".join(bad[:3]))
        tops = {n.replace("\\", "/").split("/")[0] for n in names}
        strip = len(tops) == 1 and MANIFEST not in tops and any(n.replace("\\", "/").count("/") for n in names)
        for i in infos:
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"links are not allowed in a plugin: {i.filename}")
            rel = _safe_rel(i.filename)
            if strip:
                rel = rel.split("/", 1)[1] if "/" in rel else ""
            if not rel or rel.endswith("/"):
                continue
            if i.file_size > MAX_FILE_BYTES:
                raise ValueError(f"{rel} is larger than {MAX_FILE_BYTES // 1024} KB")
            target = os.path.abspath(os.path.join(dest, rel))
            if not target.startswith(os.path.abspath(dest) + os.sep):
                raise ValueError(f"refused: {rel} escapes the plugin folder")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                out.write(src.read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES])


def install(path_or_zip, overwrite=False):
    """A plugin folder or .zip -> its name, installed under <data_dir>/plugins/ (disabled, untrusted). ValueError on refusal."""
    src = os.path.abspath(path_or_zip)
    stage = os.path.join(core.data_dir(), "plugins.staging", f"{os.getpid()}-{int(time.time() * 1000)}")
    os.makedirs(stage, exist_ok=True)
    try:
        if os.path.isdir(src):
            _check_tree(src)
            shutil.copytree(src, os.path.join(stage, "p"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            staged = os.path.join(stage, "p")
        elif os.path.isfile(src) and zipfile.is_zipfile(src):
            staged = os.path.join(stage, "p")
            os.makedirs(staged)
            _extract_zip(src, staged)
        else:
            raise ValueError(f"not a plugin folder or zip: {path_or_zip}")
        man, probs = read_manifest(staged)
        if man is None or probs:
            raise ValueError("refused: " + "; ".join(probs))
        name = man["name"]
        dest = os.path.join(machine_dir(), name)
        if os.path.isdir(os.path.join(SHIPPED_DIR, name)):
            raise ValueError(f"refused: {name} is a shipped plugin name")
        if os.path.exists(dest):
            if not overwrite:
                raise ValueError(f"{name} is already installed (remove it first)")
            shutil.rmtree(dest)
        os.makedirs(machine_dir(), exist_ok=True)
        shutil.move(staged, dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    st = _state()
    st[name] = {"enabled": False, "source": src, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save_state(st)
    _emit("plugin.install", plugin=name, source="zip" if src.lower().endswith(".zip") else "folder")
    return name


def remove(name):
    """Remove a plugin installed on this machine. Shipped and project plugins are not removed here (disable them instead)."""
    if not NAME_RX.match(name or ""):
        return False
    d = os.path.join(machine_dir(), name)
    if not os.path.isdir(d) or os.path.abspath(os.path.dirname(d)) != os.path.abspath(machine_dir()):
        return False
    shutil.rmtree(d)
    st = _state()
    st.pop(name, None)
    _save_state(st)
    _emit("plugin.remove", plugin=name)
    return True


def new(name, where=None):
    """Scaffold an empty, valid plugin. -> its folder."""
    if not NAME_RX.match(name or ""):
        raise ValueError("name: lower-case letters, digits and -")
    d = os.path.join(where or machine_dir(), name)
    if os.path.exists(d):
        raise ValueError(f"{d} already exists")
    for k in KINDS:
        os.makedirs(os.path.join(d, k))
    with open(os.path.join(d, MANIFEST), "w", encoding="utf-8") as f:
        json.dump({"name": name, "version": "0.1.0", "description": f"{name}: describe the way of working this bundles",
                   "requires": {"architect": f">={ARCHITECT_VERSION}"}}, f, indent=2)
    with open(os.path.join(d, "skills", name + ".md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: what this skill teaches\nwhen: words, that, trigger, it\n---\n# {name}\n\nWrite the way of working here.\n")
    return d


def pack(name, out=None, root=None):
    """Zip a plugin (any scope) -> the zip path. No network; the zip holds only the plugin folder."""
    p = discover(root).get(name)
    if not p:
        raise ValueError(f"no plugin {name}")
    _check_tree(p["dir"])
    out = out or os.path.join(core.data_dir(), f"{name}-{(p['manifest'] or {}).get('version', '0')}.zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(p["dir"]):
            dns[:] = sorted(d for d in dns if d != "__pycache__")
            for fn in sorted(fns):
                fp = os.path.join(dp, fn)
                z.write(fp, os.path.relpath(fp, p["dir"]).replace(os.sep, "/"))
    with open(out, "wb") as f:
        f.write(buf.getvalue())
    return out


def validate(root=None):
    """-> {name: [problems]} for every plugin found (empty list = fine)."""
    return {r["name"]: r["problems"] for r in list(root)}


def doctor_check(root=None):
    """For arch_doctor: (ok True/False/None, one line). None = plugins could not be read at all."""
    try:
        rows = list(root)
    except Exception as e:
        return None, f"plugins could not be read: {e}"
    bad = [r["name"] for r in rows if r["problems"]]
    wait = [r["name"] for r in rows if r["enabled"] and r["needs_trust"] and r["trusted"] is not True]
    on = sum(1 for r in rows if r["enabled"])
    msg = f"{len(rows)} plugin(s), {on} enabled"
    if wait:
        msg += "; waiting for trust: " + ", ".join(wait)
    if bad:
        msg += "; with problems: " + ", ".join(bad)
        return False, msg
    return True, msg
