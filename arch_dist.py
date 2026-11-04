"""Building an installer: one folder (or zip) that sets this app up on another computer with no network at all.

What goes in, for a chosen hardware profile (`gpu6`, `cpu16`, ...):

    architect/            the app itself -- code, flows, prompts, skills, commands, templates, profiles
    bin/llama/            the model server, if you ask for it (--with-server). About 1.2 GB with its CUDA libraries.
    models/               the profile's models as GGUF files, if you ask for them (--with-models). Several GB.
    MODELS.txt            otherwise: exactly which models to put in place, and the one command for each
    SHA256SUMS            a checksum for every file, so a copy that was damaged or altered is caught before it runs
    install.bat / .sh     verify the checksums, put the models where llama-server or Ollama finds them, pick the team for the
                          machine it lands on (`port --apply`), and run the offline self-check
    INSTALL.md            the same steps in plain words

Nothing about the machine that BUILT the installer goes in: no measured speeds, no chosen team, no cache, no history. The machine it
is installed on measures itself, because a 6 GB plan copied onto a 24 GB card would be wrong in both directions.

    python architect.py dist --profile gpu6                       # app only: small, the models come from Ollama or a USB stick
    python architect.py dist --profile gpu6 --with-server         # + the model server, so nothing has to be downloaded
    python architect.py dist --profile gpu6 --with-server --with-models --zip
    python architect.py dist --verify path/to/installer           # check every file against its checksum
"""
import hashlib
import json
import os
import shutil
import time
import zipfile


HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"__pycache__", "bin", "models", "._guard", ".arch", "hub_data", ".git", "node_modules", ".pytest_cache", "dist"}
SKIP_FILES = {"architect_config.json", "architect_config.json.bak", "portfolio.json"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".zip", ".bak", ".tmp"}
CHUNK = 1 << 20


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def app_files(src=None):
    """Every file of the app that belongs in an installer, as (absolute path, relative path)."""
    src = src or HERE                       # read when called, not frozen when the module loaded
    for base, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(files):
            if fn in SKIP_FILES or os.path.splitext(fn)[1].lower() in SKIP_EXT or ".bak" in fn:
                continue
            full = os.path.join(base, fn)
            yield full, os.path.relpath(full, src).replace("\\", "/")


def profile_models(profile):
    """The models a profile names, tier by tier. -> [{"tier", "model", "why"}]."""
    import arch_server
    prof = arch_server.load_profile(profile) or {}
    out = []
    for tier in ("worker", "router", "heavy", "draft"):
        t = (prof.get("tiers") or {}).get(tier) or {}
        if t.get("model"):
            out.append({"tier": tier, "model": t["model"], "why": t.get("why", "")})
    return out


def build(profile, dest, with_server=False, with_models=False, make_zip=False, say=print):
    """Write an installer folder (and optionally a zip of it). -> a summary of what went in."""
    import arch_server
    if not arch_server.load_profile(profile):
        raise ValueError(f"there is no '{profile}' profile; the profiles are: {', '.join(arch_server.list_profiles())}")
    root = os.path.abspath(dest)
    if os.path.exists(root):
        shutil.rmtree(root)
    app = os.path.join(root, "architect")
    os.makedirs(app)
    n_app = 0
    for full, rel in app_files():
        target = os.path.join(app, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(full, target)
        n_app += 1
    say(f"[dist] app: {n_app} files")

    server_note = "not included: install llama-server with `python architect.py server fetch --yes`, or the app uses Ollama instead"
    if with_server:
        src = os.path.join(HERE, "bin", "llama")
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(app, "bin", "llama"))
            ver = ""
            try:
                with open(os.path.join(src, "VERSION.txt"), encoding="utf-8") as f:
                    ver = f.read().strip()[:120]
            except OSError:
                pass
            server_note = f"included: {ver or 'llama-server'}"
            say(f"[dist] server: {server_note}")
        else:
            server_note = "asked for, but this machine has no llama-server to copy (run `python architect.py server fetch --yes` first)"
            say(f"[dist] server: {server_note}")

    models = profile_models(profile)
    copied, listed = [], []
    for m in models:
        rm = arch_server.resolve_model(m["model"]) if with_models else None
        if rm and rm.get("gguf") and os.path.isfile(rm["gguf"]):
            name = arch_server.safe_name(m["model"]) if hasattr(arch_server, "safe_name") else m["model"].replace("/", "_").replace(":", "_")
            target = os.path.join(root, "models", name + ".gguf")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(rm["gguf"], target)
            copied.append({**m, "file": "models/" + name + ".gguf", "gb": round(os.path.getsize(target) / 1024 ** 3, 2)})
            say(f"[dist] model for {m['tier']}: {m['model']} ({copied[-1]['gb']} GB)")
        else:
            listed.append(m)
    with open(os.path.join(root, "MODELS.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_models_text(profile, copied, listed, with_models))

    with open(os.path.join(root, "INSTALL.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_install_md(profile, server_note, copied, listed))
    with open(os.path.join(root, "install.bat"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(_install_bat(profile))
    with open(os.path.join(root, "install.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_install_sh(profile))
    with open(os.path.join(root, "installer.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"profile": profile, "built": time.strftime("%Y-%m-%d %H:%M"), "app_files": n_app,
                   "server": server_note, "models_included": copied, "models_to_fetch": listed,
                   "left_out": "every measurement and choice of the machine that built this: it is re-measured where it is installed"},
                  f, indent=1)

    sums = write_sums(root)
    say(f"[dist] checksums: {sums} files")
    out = {"path": root, "profile": profile, "app_files": n_app, "server": server_note, "models_included": copied,
           "models_to_fetch": listed, "files": sums, "bytes": _size(root)}
    if make_zip:
        zp = root.rstrip("\\/") + ".zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for base, _dirs, files in os.walk(root):
                for fn in files:
                    full = os.path.join(base, fn)
                    z.write(full, os.path.join(os.path.basename(root), os.path.relpath(full, root)))
        out["zip"] = zp
        out["zip_bytes"] = os.path.getsize(zp)
        say(f"[dist] zip: {zp} ({round(out['zip_bytes'] / 1024 ** 2, 1)} MB)")
    return out


def _size(root):
    return sum(os.path.getsize(os.path.join(b, f)) for b, _d, fs in os.walk(root) for f in fs)


def write_sums(root):
    """SHA256SUMS over every file except itself, in the format `sha256sum -c` and the install scripts both read."""
    rows = []
    for base, _dirs, files in os.walk(root):
        for fn in sorted(files):
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel == "SHA256SUMS":
                continue
            rows.append(f"{_sha256(full)}  {rel}")
    with open(os.path.join(root, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(rows, key=lambda r: r.split("  ", 1)[1])) + "\n")
    return len(rows)


def verify(root):
    """Re-hash every file named in SHA256SUMS. -> {"ok", "checked", "changed", "missing", "extra"}."""
    root = os.path.abspath(root)
    p = os.path.join(root, "SHA256SUMS")
    if not os.path.isfile(p):
        return {"ok": False, "checked": 0, "changed": [], "missing": [], "extra": [], "why": "there is no SHA256SUMS file here"}
    want = {}
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "  " in line:
                h, rel = line.split("  ", 1)
                want[rel] = h
    changed, missing = [], []
    for rel, h in want.items():
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            missing.append(rel)
        elif _sha256(full) != h:
            changed.append(rel)
    have = {os.path.relpath(os.path.join(b, f), root).replace("\\", "/") for b, _d, fs in os.walk(root) for f in fs}
    extra = sorted(x for x in have - set(want) - {"SHA256SUMS"} if not x.startswith("architect/hub_data/")
                   and not x.startswith("architect/._guard/") and "/__pycache__/" not in x and not x.endswith("architect_config.json"))
    return {"ok": not changed and not missing, "checked": len(want), "changed": changed, "missing": missing, "extra": extra}


# ---------------------------------------------------------------- the words that go with it

def _models_text(profile, copied, listed, asked):
    lines = [f"Models for the '{profile}' profile", ""]
    if copied:
        lines.append("Included in models/ (the install script puts them where the app finds them):")
        lines += [f"  {m['tier']:<7} {m['model']}  ->  {m['file']}  ({m['gb']} GB)" for m in copied]
        lines.append("")
    if listed:
        lines.append("Not included" + ("" if asked else " (build with --with-models to carry them)") + ". Install them with Ollama:")
        lines += [f"  {m['tier']:<7} ollama pull {m['model']}" for m in listed]
        lines.append("")
    lines.append("These are only the profile's defaults. After installing, `python architect.py port` measures every model you")
    lines.append("actually have against the new machine and picks the team from those -- a model you prefer is fine.")
    return "\n".join(lines) + "\n"


def _install_md(profile, server_note, copied, listed):
    return f"""# Installing Architect ({profile} profile)

Everything here works without a network connection.

## 1. Check the copy is intact

Windows: double-click `install.bat`. macOS or Linux: `sh install.sh`.

The script checks every file against `SHA256SUMS` first and stops if anything does not match. A damaged download or a file that was
changed after this installer was built is caught before anything runs.

## 2. What it then does

1. Puts any models from `models/` where the model server finds them.
2. Measures every model on this machine against this graphics card and picks one for each job (`port --apply`). Nothing is copied
   from the machine that built this installer: a plan for another card would be wrong here.
3. Runs the offline self-check, which proves the app works on this machine with no model and no network.

## What is in this installer

- The model server: {server_note}.
- Models included: {", ".join(m["model"] for m in copied) or "none"}.
- Models to install yourself: {", ".join(m["model"] for m in listed) or "none"} (see `MODELS.txt`).

## Afterwards

```
python architect/architect.py port          # see which model does which job here, and why
python architect/architect_hub.py           # open the app
```
"""


def _install_bat(profile):
    return f"""@echo off
setlocal
cd /d "%~dp0"
echo Architect installer ({profile} profile)
echo.
where python >nul 2>nul || (echo Python 3.10 or newer is needed: https://www.python.org/downloads/ & exit /b 1)
echo [1/4] Checking every file against SHA256SUMS...
python architect\\architect.py dist --verify . || (echo The installer is damaged or was changed. Nothing was installed. & exit /b 1)
echo [2/4] Placing models...
if exist models\\*.gguf (
  if not exist architect\\models mkdir architect\\models
  copy /y models\\*.gguf architect\\models\\ >nul
)
echo [3/4] Picking the team for THIS machine...
python architect\\architect.py port --apply
echo [4/4] Offline self-check...
python architect\\architect.py selftest --suite core
echo.
echo Done. Open the app with:  python architect\\architect_hub.py
endlocal
"""


def _install_sh(profile):
    return f"""#!/bin/sh
set -e
cd "$(dirname "$0")"
echo "Architect installer ({profile} profile)"
command -v python3 >/dev/null 2>&1 || {{ echo "Python 3.10 or newer is needed."; exit 1; }}
echo "[1/4] Checking every file against SHA256SUMS..."
python3 architect/architect.py dist --verify . || {{ echo "The installer is damaged or was changed. Nothing was installed."; exit 1; }}
echo "[2/4] Placing models..."
if ls models/*.gguf >/dev/null 2>&1; then mkdir -p architect/models && cp models/*.gguf architect/models/; fi
echo "[3/4] Picking the team for THIS machine..."
python3 architect/architect.py port --apply
echo "[4/4] Offline self-check..."
python3 architect/architect.py selftest --suite core
echo
echo "Done. Open the app with:  python3 architect/architect_hub.py"
"""
