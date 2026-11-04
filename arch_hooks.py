"""Hooks: your own commands, run at fixed points in a run.

A harness cannot know that this project formats with one tool, generates a client from a schema, or refuses anything that fails a
house linter. A hook lets you say so once, as a command, and have it enforced on every run -- by the machine, not by reminding a model.

    <project>/.arch/hooks.json      this project's hooks
    <data_dir>/hooks.json           hooks for everything you do on this machine

    {
      "hooks": [
        {"on": "after_apply", "match": "*.py",  "run": "python -m pyflakes {files}", "blocking": true},
        {"on": "after_apply", "match": "*.sql", "run": "sqlfluff lint {files}",      "blocking": true, "timeout": 120},
        {"on": "after_verdict", "when": "pass", "run": "git add -A"}
      ]
    }

Points you can hook: `before_run`, `after_apply` (files just changed), `after_check`, `after_verdict`, `after_run`.
A **blocking** hook that exits non-zero fails the step, and its output is handed back to the model as the reason -- the same path a failing
test takes, so the fix loop works on it. A non-blocking hook is reported and never fails anything.

Placeholders: `{files}` (space-separated, quoted), `{root}`, `{flow}`, `{step}`, `{verdict}`, `{run}`.

**A project's hooks are not trusted until you say so.** `.arch/hooks.json` arrives with the code, so a project you cloned could otherwise
run commands the moment you opened it here. The first time a project's hook file appears -- and again whenever it changes -- the harness
refuses to run it, prints exactly what it would have run, and waits for `python architect.py hooks --trust`. Your own machine-wide file in
`<data_dir>` needs no such approval: you wrote it.
"""
import fnmatch
import hashlib
import json
import os
import shlex
import time

import arch_core as core

POINTS = ("before_run", "after_apply", "after_check", "after_verdict", "after_run")
DEFAULT_TIMEOUT = 120
MAX_OUTPUT = 4000
PROJECT_REL = os.path.join(".arch", "hooks.json")


def project_file(root):
    return os.path.join(root, PROJECT_REL)


def machine_file():
    return os.path.join(core.data_dir(), "hooks.json")


def trust_file():
    return os.path.join(core.data_dir(), "trusted_hooks.json")


# ---------------------------------------------------------------- loading and trust

def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    rows = d.get("hooks") if isinstance(d, dict) else d
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("run") or r.get("on") not in POINTS:
            continue
        out.append({"on": r["on"], "run": str(r["run"]), "match": str(r.get("match") or ""),
                    "when": str(r.get("when") or ""), "blocking": bool(r.get("blocking")),
                    "timeout": max(1, min(900, int(r.get("timeout") or DEFAULT_TIMEOUT))),
                    "name": str(r.get("name") or "")[:60], "source": path})
    return out


def fingerprint(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def trusted(path):
    fp = fingerprint(path)
    if not fp:
        return False
    d = core.load_json(trust_file()) or {}
    return d.get(os.path.abspath(path)) == fp


def trust(path, yes=True):
    """Record that this exact hook file is approved. A later edit changes the fingerprint and needs approving again."""
    d = core.load_json(trust_file()) or {}
    p = os.path.abspath(path)
    if yes:
        fp = fingerprint(path)
        if not fp:
            return False
        d[p] = fp
    else:
        d.pop(p, None)
    try:
        os.makedirs(os.path.dirname(trust_file()), exist_ok=True)
        core._atomic_write(trust_file(), json.dumps(d, indent=1, sort_keys=True))
        return True
    except (OSError, AttributeError):
        return False


def load(root, cfg=None):
    """-> {"hooks": [...], "untrusted": [{"path", "hooks"}]}. Hooks from an unapproved project file are listed, never run."""
    cfg = cfg or {}
    out, untrusted = [], []
    mf = machine_file()
    if os.path.isfile(mf):
        out += _read(mf)                                  # your own machine-wide file: you wrote it, no approval needed
    pf = project_file(root) if root else ""
    if pf and os.path.isfile(pf):
        rows = _read(pf)
        if rows:
            if trusted(pf) or ((cfg.get("hooks") or {}).get("trust_project")):
                out += rows
            else:
                untrusted.append({"path": pf, "hooks": rows})
    return {"hooks": out, "untrusted": untrusted}


# ---------------------------------------------------------------- running

def _applies(hook, files, verdict):
    if hook.get("when") and verdict and hook["when"] != verdict:
        return False
    pat = hook.get("match")
    if not pat:
        return True
    return any(fnmatch.fnmatch((f or "").replace("\\", "/"), pat) or fnmatch.fnmatch(os.path.basename(f or ""), pat)
               for f in (files or []))


def split_cmd(cmd):
    """Split a command line into arguments without destroying Windows paths.

    shlex in POSIX mode eats the backslashes in `C:\\Python\\python.exe`; in Windows mode it keeps the quotation marks inside the token,
    so the program receives them literally and sees a different argument than the one that was written. Split the Windows way, then
    remove one matching pair of surrounding quotes per argument, which is what both shells actually mean.
    """
    out = []
    for tok in shlex.split(cmd, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def _fill(cmd, ctx):
    files = " ".join(shlex.quote(f) for f in (ctx.get("files") or []))
    out = cmd
    for k, v in (("files", files), ("root", ctx.get("root") or ""), ("flow", ctx.get("flow") or ""),
                 ("step", ctx.get("step") or ""), ("verdict", ctx.get("verdict") or ""), ("run", ctx.get("run") or "")):
        out = out.replace("{" + k + "}", str(v))
    return out


def fire(e, point, files=(), verdict="", step="", say=None):
    """Run every hook attached to this point. -> {"ran": [...], "blocked": "" or why}.

    `blocked` is the text to hand back to the model: a blocking hook that failed is treated exactly like a failing test, so the existing
    fix loop repairs it. A hook that cannot start at all is reported and never blocks -- a missing tool is the person's problem to fix, not
    a reason to fail their work.
    """
    cfg = getattr(e, "cfg", {}) or {}
    if not (cfg.get("hooks") or {}).get("enabled", True):
        return {"ran": [], "blocked": ""}
    root = getattr(e, "root", "") or ""
    found = load(root, cfg)
    say = say or getattr(e, "say", None) or (lambda _m: None)
    for u in found["untrusted"]:
        say(f"[hooks] {os.path.relpath(u['path'], root) if root else u['path']} is not approved yet, so its "
            f"{len(u['hooks'])} hook(s) did not run. Review them and approve with:  python architect.py hooks --trust")
    ctx = {"files": list(files or []), "root": root, "flow": getattr(e, "flow", ""), "step": step, "verdict": verdict,
           "run": getattr(e, "run_id", "")}
    ran, blocked = [], []
    for hk in found["hooks"]:
        if hk["on"] != point or not _applies(hk, files, verdict):
            continue
        cmd = _fill(hk["run"], ctx)
        t0 = time.time()
        label = hk["name"] or cmd[:60]
        try:
            argv = split_cmd(cmd)
        except ValueError as ex:
            argv = []
            miss = f"that command cannot be read as a command line: {ex}"
        else:
            # A tool that is not installed is the person's problem to fix, not a reason to fail their work. Checked before running so
            # that "pyflakes is not installed" never comes back to the model as "your code is wrong".
            import shutil as _sh
            miss = "" if (argv and (_sh.which(argv[0]) or os.path.isfile(argv[0]))) else \
                   (f"{argv[0]} is not installed or not on the PATH" if argv else "that hook has no command in it")
        if miss:
            rec = {"hook": label, "ok": None, "secs": 0.0, "output": miss[:MAX_OUTPUT]}
            ran.append(rec)
            say(f"    [hook] {label}: skipped -- {miss}")
            _trace(e, point, rec, cmd)
            continue
        say(f"    [hook] {label}")
        try:
            rc, out = core.run_cmd(argv, root or os.getcwd(), hk["timeout"],
                                   cfg.get("behavior", {}).get("network", "offline") == "open")
        except Exception as ex:
            rec = {"hook": label, "ok": None, "secs": 0.0, "output": f"could not start: {type(ex).__name__}: {ex}"[:MAX_OUTPUT]}
            ran.append(rec)
            say(f"    [hook] {label}: could not start ({ex})")
            _trace(e, point, rec, cmd)
            continue
        rec = {"hook": label, "ok": rc == 0, "secs": round(time.time() - t0, 1), "output": (out or "")[:MAX_OUTPUT], "rc": rc}
        ran.append(rec)
        _trace(e, point, rec, cmd)
        if rc != 0:
            say(f"    [hook] {label}: failed (exit {rc})")
            if hk["blocking"]:
                blocked.append(f"The project hook `{label}` failed:\n{(out or '').strip()[:1500]}")
    return {"ran": ran, "blocked": "\n\n".join(blocked)}


def _trace(e, point, rec, cmd):
    try:
        e._trace({"type": "hook", "t": time.time(), "flow": getattr(e, "flow", ""), "point": point,
                  "hook": rec["hook"], "cmd": cmd[:300], "ok": rec["ok"], "secs": rec["secs"],
                  "output": (rec.get("output") or "")[:1000]})
    except Exception:
        pass


# ---------------------------------------------------------------- reporting

def summary(root, cfg=None):
    """For `status` and the hub: what is set up, and what is waiting for approval."""
    found = load(root, cfg)
    return {"active": [{"on": h["on"], "run": h["run"], "blocking": h["blocking"], "match": h["match"],
                        "where": "this project" if h["source"].endswith(PROJECT_REL) else "this machine"}
                       for h in found["hooks"]],
            "waiting_for_approval": [{"path": u["path"], "count": len(u["hooks"]),
                                      "commands": [x["run"] for x in u["hooks"]]} for u in found["untrusted"]]}


EXAMPLE = {
    "hooks": [
        {"name": "pyflakes", "on": "after_apply", "match": "*.py", "run": "python -m pyflakes {files}", "blocking": True},
        {"name": "format check", "on": "after_apply", "match": "*.py", "run": "python -m black --check {files}", "blocking": False},
        {"name": "stage the change", "on": "after_verdict", "when": "pass", "run": "git add -A", "blocking": False},
    ]
}
