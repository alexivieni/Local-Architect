"""Missing-module detection for code the models write (and for Architect itself).

A small model happily imports `requests` or `yaml` into a project that has neither. The tests then fail with ModuleNotFoundError and the model
"fixes" it by deleting the test. This finds the imports that cannot be satisfied in THIS Python, names the pip package, and reports it as a
finding the fix loop can act on (use the standard library, or the person installs it). Nothing is installed automatically.
"""
import ast
import importlib.util
import os
import sys

# import name -> pip package, where they differ
PIP_NAME = {"yaml": "pyyaml", "cv2": "opencv-python", "PIL": "pillow", "sklearn": "scikit-learn", "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
            "jwt": "pyjwt", "Crypto": "pycryptodome", "serial": "pyserial", "usb": "pyusb", "dateutil": "python-dateutil", "attr": "attrs",
            "git": "gitpython", "OpenSSL": "pyopenssl", "zmq": "pyzmq", "wx": "wxpython", "gi": "pygobject", "ldap": "python-ldap", "MySQLdb": "mysqlclient",
            "psycopg2": "psycopg2-binary", "docx": "python-docx", "pptx": "python-pptx", "fitz": "pymupdf", "magic": "python-magic", "nacl": "pynacl"}
STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {"__future__"}


def imports_of(source):
    """Top-level module names imported by a Python source (absolute imports only). -> sorted set; [] if it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return sorted(names)


def available(name, root=None):
    """Can `import name` work here: the standard library, an installed package, or a module/package inside the project?"""
    if name in STDLIB or name in sys.builtin_module_names:
        return True
    if root:
        for cand in (os.path.join(root, name + ".py"), os.path.join(root, name, "__init__.py"), os.path.join(root, "src", name + ".py"), os.path.join(root, "src", name, "__init__.py"),
                     os.path.join(root, name)):
            if os.path.exists(cand):
                return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def missing(root, files):
    """-> [{"file", "module", "pip"}] for imports in these project files that this Python cannot satisfy."""
    out, seen = [], set()
    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for name in imports_of(src):
            if (rel, name) in seen or available(name, root):
                continue
            seen.add((rel, name))
            out.append({"file": rel, "module": name, "pip": PIP_NAME.get(name, name)})
    return out


def feedback(found):
    """Plain text for the model and the report."""
    if not found:
        return ""
    # Seen live (2026-11-04): a test imported `slugify`, the module the run itself was meant to write. Calling it "not installed" sent
    # a small model looking for a package, and it never created slugify.py. A missing module imported by a TEST is far more often the
    # project's own file that does not exist yet, so that is said first.
    own = [m for m in found if os.path.basename(str(m.get("file", ""))).startswith("test") or "/tests/" in "/" + str(m.get("file", ""))]
    lines = []
    if own:
        lines.append("These modules do not exist yet (the tests will fail with ModuleNotFoundError until they do):")
        for m in own:
            lines.append(f"  - {m['file']} imports `{m['module']}`: if this is the project's own code, CREATE `{m['module'].replace('.', '/')}.py` "
                         "with what the tests use. Only if it is an outside library: use the standard library instead.")
    rest = [m for m in found if m not in own]
    if rest:
        lines.append("Imports that are NOT installed in this Python (the tests will fail with ModuleNotFoundError):")
        for m in rest:
            lines.append(f"  - {m['file']} imports `{m['module']}`" + (f" (pip package `{m['pip']}`)" if m["pip"] != m["module"] else ""))
        lines.append("Use the standard library instead, or make the dependency optional.")
    lines.append("Do not delete the tests to make this pass.")
    return "\n".join(lines)


def self_check(here=None):
    """Architect's own faulty-code check: every arch module compiles and imports. -> [{"module", "error"}], empty when healthy."""
    here = here or os.path.dirname(os.path.abspath(__file__))
    bad = []
    skip = {"train_lora.py", "localintel_arch.py", "apply_localintel_layer.py", "start.py"}
    for fn in sorted(os.listdir(here)):
        if not fn.endswith(".py") or fn in skip or fn.startswith("arch_selftest") or fn == "arch_hub_check.py":
            continue
        path = os.path.join(here, fn)
        try:
            with open(path, encoding="utf-8") as f:
                compile(f.read(), path, "exec")
        except (SyntaxError, ValueError, OSError) as e:
            bad.append({"module": fn, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        try:
            import importlib
            if here not in sys.path:
                sys.path.insert(0, here)
            importlib.import_module(fn[:-3])                                   # by its real name, so modules that import each other still work
        except Exception as e:                                                 # any import-time failure is the point of this check
            bad.append({"module": fn, "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return bad
