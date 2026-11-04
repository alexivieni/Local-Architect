"""Scan an existing codebase once so every later run starts informed: what it is, how to test it, how healthy it is.

    python architect.py scan [--root PATH] [--run-tests] [--json]

Deterministic (no model needed): language and framework from files, the test command, entry points, the largest files (these set how much
context you need), the fake-code report, and optionally a real test baseline. Writes <root>/.arch/profile.md and .arch/profile.json.
The flows read profile.md as part of their project context, so the model does not rediscover the project each time.
"""
import ast
import json
import os
import subprocess
import sys

import arch_fakecode as fc

SKIP = {".git", ".arch", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache", "hub_data"}
LANGS = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
         ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C", ".cpp": "C++", ".cs": "C#", ".html": "HTML", ".css": "CSS"}
FRAMEWORKS = (("django", "Django"), ("flask", "Flask"), ("fastapi", "FastAPI"), ("pytest", "pytest"), ("react", "React"), ("vue", "Vue"), ("express", "Express"),
              ("next", "Next.js"), ("jest", "Jest"), ("vitest", "Vitest"))
MAX_FILE = 400_000


def walk(root):
    for dp, dn, fs in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP and not d.startswith(".venv")]
        for f in fs:
            p = os.path.join(dp, f)
            yield os.path.relpath(p, root).replace(os.sep, "/"), p


def _read(p, limit=MAX_FILE):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def detect_tests(root, files):
    names = set(files)
    pkg = {}
    if "package.json" in names:
        try:
            pkg = json.loads(_read(os.path.join(root, "package.json")))
        except ValueError:
            pkg = {}
        script = (pkg.get("scripts") or {}).get("test")
        if script and "no test specified" not in script:
            return "npm test"
    if any(f.endswith(".py") and (os.path.basename(f).startswith("test_") or f.endswith("_test.py") or f.startswith("tests/")) for f in files) or "pytest.ini" in names:
        return f"{sys.executable} -m pytest -q"
    if "go.mod" in names:
        return "go test ./..."
    if "Cargo.toml" in names:
        return "cargo test"
    return None


def entry_points(root, files):
    out = []
    for f in files:
        b = os.path.basename(f)
        if b in ("main.py", "app.py", "manage.py", "server.py", "__main__.py", "index.js", "main.go", "main.rs", "server.js", "index.html"):
            out.append(f)
        elif f.endswith(".py") and "__name__" in _read(os.path.join(root, f), 20000) and "__main__" in _read(os.path.join(root, f), 20000):
            out.append(f)
    return sorted(set(out))[:12]


def py_shape(root, files):
    funcs = classes = tests = 0
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            tree = ast.parse(_read(os.path.join(root, f)))
        except (SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs += 1
                tests += 1 if n.name.startswith("test") else 0
            elif isinstance(n, ast.ClassDef):
                classes += 1
    return {"functions": funcs, "classes": classes, "test_functions": tests}


def scan(root, run_tests=False, timeout=300):
    root = os.path.abspath(root)
    files, sizes = [], {}
    for rel, p in walk(root):
        files.append(rel)
        try:
            sizes[rel] = os.path.getsize(p)
        except OSError:
            sizes[rel] = 0
    langs = {}
    for f in files:
        l = LANGS.get(os.path.splitext(f)[1].lower())
        if l:
            langs[l] = langs.get(l, 0) + sizes[f]
    blob = " ".join(_read(os.path.join(root, f), 20000).lower() for f in files if os.path.basename(f) in ("requirements.txt", "pyproject.toml", "package.json", "setup.py", "go.mod"))
    for f in files:                                         # frameworks that are imported but not listed in a manifest
        if f.endswith(".py"):
            blob += " " + " ".join(ln.lower() for ln in _read(os.path.join(root, f), 20000).splitlines() if ln.startswith(("import ", "from ")))
    fws = sorted({label for key, label in FRAMEWORKS if key in blob})
    code_chars = sum(v for k, v in langs.items() if k not in ("HTML", "CSS"))
    big = sorted(((sizes[f], f) for f in files if os.path.splitext(f)[1].lower() in LANGS), reverse=True)[:8]
    fake = fc.summarize(fc.scan(root, [f for f in files if f.endswith(".py")]))
    test_cmd = detect_tests(root, files)
    prof = {"root": root, "files": len(files), "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])), "frameworks": fws,
            "code_bytes": code_chars, "approx_tokens": code_chars // 3, "test_command": test_cmd, "entry_points": entry_points(root, files),
            "largest_files": [{"file": f, "bytes": b} for b, f in big], "python": py_shape(root, files), "fake_code": fake, "baseline": None}
    # whole project in context? -> tells the harness (and the person) whether a big window matters here
    biggest_tokens = (big[0][0] // 3) if big else 0
    prof["context_hint"] = (f"the largest file is about {biggest_tokens} tokens; the whole code base is about {prof['approx_tokens']} tokens"
                            + (" (fits an 8K window only file by file)" if prof["approx_tokens"] > 6000 else " (fits a small window)"))
    if run_tests and test_cmd:
        try:
            r = subprocess.run(test_cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout)
            prof["baseline"] = {"command": test_cmd, "returncode": r.returncode, "tail": (r.stdout + r.stderr)[-600:]}
        except subprocess.TimeoutExpired:
            prof["baseline"] = {"command": test_cmd, "returncode": None, "tail": f"timed out after {timeout}s"}
    return prof


def render(p):
    L = ["# Project profile (written by `architect.py scan`)", "",
         f"- Languages: {', '.join(f'{k} ({v // 1000} KB)' for k, v in list(p['languages'].items())[:5]) or 'none detected'}",
         f"- Frameworks: {', '.join(p['frameworks']) or 'none detected'}",
         f"- Size: {p['files']} files, about {p['approx_tokens']} tokens of code. {p['context_hint']}",
         f"- Test command: {p['test_command'] or 'none found (adding tests is the first job)'}"]
    if p["baseline"]:
        b = p["baseline"]
        L.append(f"- Test baseline: {'passing' if b['returncode'] == 0 else 'FAILING' if b['returncode'] is not None else 'timed out'}")
    if p["entry_points"]:
        L.append(f"- Entry points: {', '.join(p['entry_points'])}")
    py = p["python"]
    if py["functions"]:
        L.append(f"- Python: {py['functions']} functions, {py['classes']} classes, {py['test_functions']} test functions")
    fk = p["fake_code"]
    L.append(f"- Fake-code findings (gate level / total): {fk['gate']} / {fk['total']}" + (f"  ({', '.join(f'{k}: {v}' for k, v in fk['by_rule'].items())})" if fk["by_rule"] else ""))
    if p["largest_files"]:
        L += ["", "Largest files (split or read these carefully):"] + [f"- {x['file']} ({x['bytes'] // 1000} KB)" for x in p["largest_files"][:5]]
    return "\n".join(L) + "\n"


def write(root, prof):
    d = os.path.join(root, ".arch")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "profile.md"), "w", encoding="utf-8") as f:
        f.write(render(prof))
    with open(os.path.join(d, "profile.json"), "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=1)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--run-tests", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    p = scan(a.root, a.run_tests)
    write(os.path.abspath(a.root), p)
    print(json.dumps(p, indent=1) if a.json else render(p) + f"\nWritten to {os.path.join(os.path.abspath(a.root), '.arch', 'profile.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
