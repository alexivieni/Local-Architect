"""Risky Python calls found through the AST (comments and strings never match)."""
import ast

from .findings import Finding


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _kw(call, name):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _is_const(node, value):
    return isinstance(node, ast.Constant) and node.value is value


def scan_python(path, source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn, line = _name(node.func), node.lineno
        if fn in ("eval", "exec"):
            out.append(Finding("PY001", "high", path, line, f"{fn}() runs arbitrary code"))
        if fn == "os.system" or (fn.startswith("subprocess.") and _is_const(_kw(node, "shell"), True)):
            out.append(Finding("PY002", "high", path, line, f"{fn} through a shell"))
        if fn in ("pickle.load", "pickle.loads"):
            out.append(Finding("PY003", "high", path, line, f"{fn} on untrusted data runs code"))
        if fn == "yaml.load" and not (_kw(node, "Loader") is not None and "Safe" in _name(_kw(node, "Loader"))):
            out.append(Finding("PY003", "high", path, line, "yaml.load without SafeLoader"))
        if fn in ("hashlib.md5", "hashlib.sha1"):
            out.append(Finding("PY004", "low", path, line, f"{fn} is weak for security uses"))
        if _is_const(_kw(node, "verify"), False):
            out.append(Finding("PY005", "medium", path, line, "TLS verification turned off"))
    return out
