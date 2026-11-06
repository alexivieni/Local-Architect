"""Onboarding, the /init equivalent: look at a project once and draft its ARCHITECT.md for the person to accept.

    onboard(root)          -> report dict; writes .arch/profile.*, builds the FTS index, drafts .arch/memory/proposed_ARCHITECT.md
    accept_draft(root)     -> copies the draft to <root>/ARCHITECT.md (only when there is none, or force=True)

No model is used: language, test command, run command, package layout and entry points come from arch_scan and the contract derivation
in arch_templates_catalog; conventions and gotchas are measured from the code (indentation, line endings, type hints, test naming,
files that do not parse, fake-code findings). Idempotent: the draft is a pure function of the project, so a second run on an unchanged
project changes nothing (`changed: False`). The person's own ARCHITECT.md is never written by onboard().
"""
import ast
import json
import os
import re
import sys

import arch_scan as scan_mod

DRAFT_NAME = "proposed_ARCHITECT.md"
MAX_READ = 200_000
MAX_PY = 400


def draft_path(root):
    return os.path.join(root, ".arch", "memory", DRAFT_NAME)


def _cmd(c):
    """A command as the person would type it: this interpreter's path becomes plain `python`."""
    return (c or "").replace(sys.executable, "python") if c else c


def _read_bytes(p, limit=MAX_READ):
    try:
        with open(p, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _py_texts(root, files):
    out = {}
    for rel in files:
        if rel.endswith(".py") and len(out) < MAX_PY:
            b = _read_bytes(os.path.join(root, rel))
            if b:
                out[rel] = b.decode("utf-8", "replace")
    return out


def _layout(files):
    top_dirs, top_files, pkgs = {}, [], set()
    for f in files:
        parts = f.split("/")
        if len(parts) == 1:
            top_files.append(f)
        else:
            top_dirs[parts[0]] = top_dirs.get(parts[0], 0) + 1
        if parts[-1] == "__init__.py" and len(parts) >= 2:
            pkgs.add("/".join(parts[:-1]))
    src_layout = "src" in top_dirs and any(p.startswith("src/") for p in pkgs)
    top_pkgs = sorted(p for p in pkgs if "/" not in p or (src_layout and p.count("/") == 1 and p.startswith("src/")))
    test_dirs = sorted(d for d in top_dirs if d in ("tests", "test", "__tests__", "spec"))
    return {"dirs": dict(sorted(top_dirs.items())), "top_files": sorted(top_files), "packages": top_pkgs, "src_layout": src_layout,
            "test_dirs": test_dirs, "docs": "docs" in top_dirs}


def _conventions(texts, root, files):
    """Measured from the code, never guessed. -> list of plain sentences."""
    out = []
    crlf = lf = tabs = spaces = 0
    funcs = typed = docd = 0
    longest = []
    for rel, t in texts.items():
        raw = _read_bytes(os.path.join(root, rel))
        crlf += raw.count(b"\r\n")
        lf += raw.count(b"\n") - raw.count(b"\r\n")
        for ln in t.splitlines():
            if ln.startswith("\t"):
                tabs += 1
            elif ln.startswith("    "):
                spaces += 1
            longest.append(len(ln))
        try:
            tree = ast.parse(t)
        except (SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs += 1
                typed += 1 if (n.returns is not None or any(a.annotation is not None for a in n.args.args)) else 0
                docd += 1 if ast.get_docstring(n) else 0
    if texts:
        out.append(f"Python files indent with {'tabs' if tabs > spaces else '4 spaces'}.")
        if crlf and lf:
            out.append("Line endings are mixed (CRLF and LF): keep each file's own endings when editing.")
        elif crlf:
            out.append("Python files use CRLF line endings: keep them.")
        else:
            out.append("Python files use LF line endings.")
        if longest:
            longest.sort()
            p95 = longest[int(len(longest) * 0.95) - 1] if len(longest) > 20 else longest[-1]
            out.append(f"Lines are usually under {max(80, (p95 // 10 + 1) * 10)} characters.")
    if funcs:
        if typed / funcs >= 0.5:
            out.append(f"Type hints are the norm ({typed} of {funcs} functions have them): add them to new functions.")
        elif typed == 0:
            out.append("The code has no type hints; do not add them to untouched code.")
        if docd / funcs >= 0.4:
            out.append(f"Functions usually have docstrings ({docd} of {funcs}): write one for each new function.")
    names = [os.path.basename(f) for f in files if f.endswith(".py") and ("test" in f.lower())]
    if names:
        out.append("Test files are named " + ("`test_*.py`." if sum(n.startswith("test_") for n in names) >= len(names) / 2 else "`*_test.py`."))
    return out


def _gotchas(root, files, prof, texts, has_git):
    g = []
    crlf_files = [r for r in texts if b"\r\n" in _read_bytes(os.path.join(root, r), 20000)]
    if crlf_files:
        g.append(f"{len(crlf_files)} Python file(s) use CRLF endings (e.g. `{crlf_files[0]}`): an edit that rewrites them with LF makes a noisy diff.")
    bad = []
    for rel, t in texts.items():
        try:
            ast.parse(t)
        except (SyntaxError, ValueError):
            bad.append(rel)
    if bad:
        g.append(f"These files do not parse today, so tests that import them fail before any change: {', '.join(bad[:4])}.")
    if not prof["test_command"]:
        g.append("No test command was found: adding tests is the first job before any change can be checked.")
    fk = prof["fake_code"]
    if fk.get("total"):
        g.append(f"The fake-code scan already reports {fk['total']} finding(s) ({fk['gate']} at gate level): they were there before any run.")
    if prof["largest_files"] and prof["largest_files"][0]["bytes"] > 60_000:
        b = prof["largest_files"][0]
        g.append(f"`{b['file']}` is {b['bytes'] // 1000} KB: too big for a small context window; ask for changes to one function at a time.")
    if not has_git:
        g.append("This folder is not a git repository, so there is no branch or commit to land a run on.")
    if "pytest" in prof["frameworks"] and not any(f.endswith("conftest.py") for f in files) and any(f.startswith("tests/") for f in files):
        g.append("Tests live in `tests/` without a conftest.py: imports from the project root only work when pytest runs from the root.")
    return g


def _run_command(prof, contract, files):
    top = set(files)
    mains = (contract or {}).get("cli", {}).get("mains") or []
    if mains:
        return f"python -m {mains[0]}"
    if "package.json" in top:
        try:
            scripts = json.loads(_read_bytes(os.path.join(prof["root"], "package.json")).decode("utf-8", "replace")).get("scripts") or {}
        except ValueError:
            scripts = {}
        for k in ("start", "dev"):
            if k in scripts:
                return "npm start" if k == "start" else "npm run dev"
    cli_mod = (contract or {}).get("cli", {}).get("module")
    for e in prof["entry_points"]:
        if e == "manage.py":
            return "python manage.py runserver"
        if e.endswith(".py") and "/" not in e:
            return f"python {e}"
    if cli_mod:
        return f"python -m {cli_mod}"
    for e in prof["entry_points"]:
        if e.endswith("__main__.py") and "/" in e:
            return "python -m " + e[:-len("/__main__.py")].replace("/", ".")
        if e.endswith(".py"):
            return f"python {e}"
    if "go.mod" in top:
        return "go run ."
    if "Cargo.toml" in top:
        return "cargo run"
    return None


def draft(report):
    """The ARCHITECT.md text, a pure function of the report (so an unchanged project gives identical bytes)."""
    r = report
    L = ["# Project memory", "", "Drafted by Architect from the code; edit freely. Architect reads the top of this file in every prompt, so keep it short.", ""]
    L += ["## What this is", "",
          f"- Language: {r['language'] or 'not detected'}" + (f" ({', '.join(r['frameworks'])})" if r["frameworks"] else ""),
          f"- Size: {r['files']} files, about {r['approx_tokens']} tokens of code."]
    lay = r["layout"]
    if lay["packages"]:
        L.append(f"- Packages: {', '.join('`' + p + '`' for p in lay['packages'][:8])}" + (" (src layout)" if lay["src_layout"] else ""))
    if lay["test_dirs"]:
        L.append(f"- Tests live in: {', '.join('`' + d + '/`' for d in lay['test_dirs'])}")
    if r["entry_points"]:
        L.append(f"- Entry points: {', '.join('`' + e + '`' for e in r['entry_points'][:6])}")
    L += ["", "## Commands", "",
          f"- Test: `{r['test_command']}`" if r["test_command"] else "- Test: none found yet",
          f"- Run: `{r['run_command']}`" if r["run_command"] else "- Run: not detected"]
    if r["cli_commands"]:
        L.append(f"- CLI commands: {', '.join(r['cli_commands'][:10])}")
    if r["http_routes"]:
        L.append(f"- HTTP routes: {', '.join(r['http_routes'][:8])}")
    if r["public_api"]:
        L += ["", "## Public API (modules and what they define)", ""] + [f"- `{m}`: {', '.join(n)}" for m, n in r["public_api"][:12]]
    L += ["", "## Conventions", ""] + ([f"- {c}" for c in r["conventions"]] or ["- (nothing measurable yet)"])
    L += ["", "## Gotchas", ""] + ([f"- {g}" for g in r["gotchas"]] or ["- None found by the scan."])
    L += ["", "## Learned", ""]
    return "\n".join(L) + "\n"


def onboard(root, build_index=True, write=True):
    """Inspect `root` and draft its ARCHITECT.md. -> the report (see keys below); `changed` says whether the draft file's bytes changed.
    Never touches <root>/ARCHITECT.md. Safe to call again at any time."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"ok": False, "why": f"{root} is not a folder"}
    prof = scan_mod.scan(root)
    files = [rel for rel, _ in scan_mod.walk(root)]
    texts = _py_texts(root, files)
    contract = None
    try:
        import arch_templates_catalog as cat
        src = {r: t for r, t in texts.items() if len(t) < 120_000}
        contract = cat.derive_contract(src, pkg=os.path.basename(root), test_cmd=_cmd(prof["test_command"]) or "")
    except Exception:
        contract = None
    langs = [k for k in prof["languages"] if k not in ("HTML", "CSS")]
    layout = _layout(files)
    has_git = os.path.isdir(os.path.join(root, ".git"))
    public = []
    for m in (contract or {}).get("modules", [])[:40]:
        names = [p if isinstance(p, str) else p.get("name", "") for p in m.get("public", [])]
        names = [n for n in names if n][:6]
        if names and not m["path"].endswith("__main__.py"):
            public.append((m["module"], names))
    http = [f"{h['method']} {h['path']}" for h in (contract or {}).get("http", [])]
    rep = {"ok": True, "root": root, "files": prof["files"], "approx_tokens": prof["approx_tokens"], "language": langs[0] if langs else "",
           "languages": list(prof["languages"]), "frameworks": prof["frameworks"], "test_command": _cmd(prof["test_command"]),
           "run_command": _run_command(prof, contract, files), "layout": layout,
           "entry_points": sorted(set(prof["entry_points"] + [m["path"] for m in (contract or {}).get("modules", []) if m["path"].endswith("__main__.py")]))[:12],
           "cli_commands": (contract or {}).get("cli", {}).get("commands", []), "http_routes": http, "public_api": public,
           "conventions": _conventions(texts, root, files), "gotchas": _gotchas(root, files, prof, texts, has_git),
           "git": has_git, "existing_architect_md": os.path.isfile(os.path.join(root, "ARCHITECT.md")),
           "index": None, "draft_path": draft_path(root), "changed": False}
    text = draft(rep)
    rep["draft"] = text
    if build_index:
        rep["index"] = _index(root)
    if write:
        try:
            scan_mod.write(root, prof)
        except OSError:
            pass
        p = draft_path(root)
        old = _read_bytes(p, 10_000_000).decode("utf-8", "replace") if os.path.isfile(p) else None
        if old != text:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            rep["changed"] = True
    return rep


def _index(root):
    """Build (or refresh) the FTS5 index. -> {"ok", "chunks", "new", "kept", "why"}; never raises."""
    try:
        import arch_core as core
        import arch_fts
        import arch_index
        if not arch_fts.available():
            return {"ok": False, "why": "this Python's SQLite has no FTS5; keyword ranking only"}
        cfg = core.load_config(root)
        rt = arch_index.Retriever(root, cfg)
        if not rt.index:
            return {"ok": False, "why": rt.off_reason or "retrieval is off"}
        rt.ensure(core.list_files(root, cfg))
        st = rt.last or {}
        if rt.off_reason:
            return {"ok": False, "why": rt.off_reason}
        return {"ok": True, "new": st.get("new", 0), "kept": st.get("kept", 0), "dropped": st.get("dropped", 0),
                "chunks": st.get("chunks", st.get("new", 0) + st.get("kept", 0))}
    except Exception as e:                                   # onboarding must finish even when retrieval cannot
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}


def accept_draft(root, force=False):
    """The person's explicit step: the draft becomes <root>/ARCHITECT.md. Refuses to overwrite an existing one unless force."""
    root = os.path.abspath(root)
    src, dst = draft_path(root), os.path.join(root, "ARCHITECT.md")
    if not os.path.isfile(src):
        return {"ok": False, "why": "there is no draft yet; run onboard first"}
    if os.path.isfile(dst) and not force:
        return {"ok": False, "why": "ARCHITECT.md already exists; merge the draft by hand or accept with force"}
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data)
    return {"ok": True, "path": dst}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="draft ARCHITECT.md for a project")
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--accept", action="store_true", help="copy the draft to ARCHITECT.md")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = onboard(a.root, build_index=not a.no_index)
    if not r.get("ok"):
        print(r["why"])
        return 1
    if a.json:
        print(json.dumps({k: v for k, v in r.items() if k != "draft"}, indent=1))
    else:
        print(r["draft"])
        ix = r["index"]
        print(f"Index: {'built, ' + str(ix['chunks']) + ' chunks' if ix and ix['ok'] else (ix or {}).get('why', 'skipped')}")
        print(f"Draft: {r['draft_path']} ({'updated' if r['changed'] else 'unchanged'}); ARCHITECT.md is only written with --accept.")
    if a.accept:
        print(json.dumps(accept_draft(a.root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
