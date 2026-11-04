"""More gates: the code checks behind the newer flows (harden, types, a11y, document, migrate, deps).

Each scanner is plain, deterministic code -- the AST for Python, a small HTML parser for pages -- so what it reports is a fact about the
files, never a model's opinion. Each comes as a before/after pair, like the security scanners:

    <kind>_scan    records the findings before the fix step, and puts them in the prompt
    <kind>_fewer   re-runs the same scanner after the fix, and passes only when fewer findings remain AND the project's checks pass

A scanner that cannot run returns ok=None, which the flow reports as "not sure", never as a pass.
"""
import ast
import os
import re
from html.parser import HTMLParser

import arch_core as core
from arch_gates import GATES, R, _flows


def _f(file, line, rule, msg, severity="medium"):
    return {"file": file, "line": int(line or 0), "rule": rule, "msg": msg, "severity": severity}


def _py_files(e, ctx, exts=(".py",)):
    import arch_flows_pro as pro
    exp = pro.explicit_files(e, ctx["goal"], exts)
    if exp:
        return exp
    return [f for f in core.list_files(e.root, e.cfg) if f.endswith(exts) and not _is_test(f)][:40]


def _is_test(path):
    p = path.replace("\\", "/")
    return p.startswith("tests/") or "/tests/" in p or os.path.basename(p).startswith("test_") or p.endswith("_test.py") or p == "conftest.py"


def _parse(e, f):
    try:
        return ast.parse(core.read_text(os.path.join(e.root, f), 400000)[0])
    except (SyntaxError, OSError, ValueError):
        return None


# ------------------------------------------------------------------ robustness (harden)

def scan_robustness(e, files):
    """Error handling and input safety that can be seen in the code itself."""
    out = []
    for f in files:
        tree = _parse(e, f)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    out.append(_f(f, node.lineno, "bare-except", "a bare `except:` also catches KeyboardInterrupt and SystemExit; name the exception", "high"))
                body = [b for b in node.body if not isinstance(b, ast.Expr) or not isinstance(getattr(b, "value", None), ast.Constant)]
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    out.append(_f(f, node.lineno, "swallowed-error", "an error is caught and silently ignored; handle it, log it, or let it propagate", "high"))
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                kws = {k.arg for k in node.keywords}
                if name == "urlopen" and "timeout" not in kws and len(node.args) < 3:
                    out.append(_f(f, node.lineno, "no-timeout", "a network call with no timeout can hang forever"))
                if name in ("get", "post", "put", "delete", "request") and isinstance(fn, ast.Attribute) and \
                        getattr(fn.value, "id", "") == "requests" and "timeout" not in kws:
                    out.append(_f(f, node.lineno, "no-timeout", "a requests call with no timeout can hang forever"))
                if name in ("run", "call", "check_output", "Popen") and any(k.arg == "shell" and getattr(k.value, "value", False) is True for k in node.keywords):
                    out.append(_f(f, node.lineno, "shell-true", "shell=True turns arguments into shell commands; pass a list instead", "high"))
            elif isinstance(node, ast.Assert) and not _is_test(f):
                out.append(_f(f, node.lineno, "assert-for-validation", "assert is removed under `python -O`; raise a real exception for invalid input", "low"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "open":
                out.append(_f(f, node.lineno, "open-without-with", "a file opened outside `with` is not closed if an error happens", "low"))
    return out


# ------------------------------------------------------------------ type hints (types)

def scan_typing(e, files):
    """Public functions whose parameters or return value carry no annotation."""
    out = []
    for f in files:
        tree = _parse(e, f)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                args = [a for a in node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
                missing = [a.arg for a in args if a.annotation is None]
                if missing or node.returns is None:
                    what = ("parameters " + ", ".join(missing)) if missing else ""
                    what += (" and " if what and node.returns is None else "") + ("the return value" if node.returns is None else "")
                    out.append(_f(f, node.lineno, "untyped", f"{node.name}(): {what} not annotated", "low"))
    return out


# ------------------------------------------------------------------ accessibility (a11y)

class _A11y(HTMLParser):
    INTERACTIVE = {"a", "button", "input", "select", "textarea", "summary"}

    def __init__(self, path):
        super().__init__(convert_charrefs=True)
        self.path, self.out = path, []
        self.labels_for, self.inputs = set(), []
        self.stack, self.headings, self.has_title, self.html_lang = [], [], False, None
        self.text_for = {}

    def _add(self, rule, msg, sev="medium"):
        self.out.append(_f(self.path, self.getpos()[0], rule, msg, sev))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.html_lang = a.get("lang") or ""
        if tag == "img" and "alt" not in a:
            self._add("img-no-alt", "an image has no alt text (use alt=\"\" if it is decorative)", "high")
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button", "reset", "image"):
            self.inputs.append((self.getpos()[0], a.get("id"), bool(a.get("aria-label") or a.get("aria-labelledby") or a.get("title")),
                                bool(self.stack and "label" in self.stack)))
        if tag == "label" and a.get("for"):
            self.labels_for.add(a["for"])
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append((int(tag[1]), self.getpos()[0]))
        if a.get("tabindex") and a["tabindex"].lstrip("-").isdigit() and int(a["tabindex"]) > 0:
            self._add("positive-tabindex", "a positive tabindex breaks the natural keyboard order", "low")
        if tag in ("div", "span") and "onclick" in a and not a.get("role"):
            self._add("click-on-div", f"a clickable <{tag}> cannot be reached by keyboard; use a <button>", "high")
        if tag in ("button", "a"):
            self.text_for[len(self.stack)] = [tag, bool(a.get("aria-label") or a.get("title")), "", self.getpos()[0]]
        if tag == "title":
            self.has_title = True
        if tag not in ("img", "input", "br", "hr", "meta", "link"):
            self.stack.append(tag)

    def handle_data(self, data):
        for v in self.text_for.values():
            v[2] += data.strip()

    def handle_endtag(self, tag):
        if tag in ("button", "a"):
            k = len(self.stack) - 1
            rec = self.text_for.pop(k, None)
            if rec and not rec[1] and not rec[2]:
                self.out.append(_f(self.path, rec[3], "empty-control", f"a <{rec[0]}> has no text a screen reader can say", "high"))
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def finish(self):
        if self.html_lang == "":
            self.out.append(_f(self.path, 1, "no-lang", "<html> has no lang attribute, so a screen reader guesses the language", "medium"))
        if self.html_lang is not None and not self.has_title:
            self.out.append(_f(self.path, 1, "no-title", "the page has no <title>", "low"))
        for line, iid, aria, wrapped in self.inputs:
            if not aria and not wrapped and (not iid or iid not in self.labels_for):
                self.out.append(_f(self.path, line, "unlabelled-input", "a form field has no label a screen reader can announce", "high"))
        last = 0
        for lvl, line in self.headings:
            if last and lvl > last + 1:
                self.out.append(_f(self.path, line, "heading-skip", f"heading jumps from h{last} to h{lvl}", "low"))
            last = lvl
        return self.out


def scan_a11y(e, files):
    out = []
    for f in files:
        p = _A11y(f)
        try:
            p.feed(core.read_text(os.path.join(e.root, f), 600000)[0])
            p.close()
        except Exception:
            continue
        out += p.finish()
    return out


def _html_files(e, ctx):
    import arch_flows_pro as pro
    return pro.explicit_files(e, ctx["goal"], (".html", ".htm")) or \
        [f for f in core.list_files(e.root, e.cfg) if f.endswith((".html", ".htm"))][:30]


# ------------------------------------------------------------------ documentation (document)

def scan_docs(e, files):
    """Public functions and classes with no docstring."""
    out = []
    for f in files:
        tree = _parse(e, f)
        if tree is None:
            continue
        if not ast.get_docstring(tree):
            out.append(_f(f, 1, "no-module-doc", "the module has no docstring saying what it is for", "low"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_") \
                    and not ast.get_docstring(node):
                out.append(_f(f, node.lineno, "no-docstring", f"{node.name} has no docstring", "low"))
    return out


# ------------------------------------------------------------------ migration (migrate)

_OLD = re.compile(r"""(?:replace|migrate|move|switch|port)\s+(?:all\s+)?(?:uses?\s+of\s+|calls?\s+to\s+)?[`'"]([^`'"]{2,80})[`'"]""", re.I)


def old_pattern(ctx):
    """What is being migrated away from: params['old'] if given, else the first quoted name after 'replace'/'migrate' in the goal."""
    p = (ctx.get("params") or {}).get("old")
    if p:
        return str(p)
    m = _OLD.search(ctx.get("goal") or "")
    return m.group(1) if m else ""


def scan_pattern(e, files, pattern):
    out = []
    if not pattern:
        return out
    for f in files:
        try:
            text = core.read_text(os.path.join(e.root, f), 400000)[0]
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern in line:
                out.append(_f(f, i, "old-usage", f"still uses {pattern!r}: {line.strip()[:90]}", "medium"))
    return out


# ------------------------------------------------------------------ the pairs

def _fmt(x):
    return f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}"


def _pair(kind, files_fn, scan_fn, need_tests=True):
    def before(e, ctx, step, changed):
        try:
            files = files_fn(e, ctx)
            if not files:
                return R(None, f"there are no files in scope for the {kind} check")
            finds = scan_fn(e, files, ctx)
        except Exception as ex:
            return R(None, f"the {kind} check could not run: {type(ex).__name__}: {ex}")
        ctx.setdefault("scan", {})[kind] = {"files": files, "before": finds, "after": finds}
        ctx["scope"] = sorted({x["file"] for x in finds}) or files
        ctx["findings_text"] = "\n".join(_fmt(x) for x in finds[:30]) or "(nothing found)"
        e.say(f"[scan] {len(finds)} {kind} finding(s) in {len(files)} file(s)")
        e.report(f"- Scanned ({kind}): {len(files)} file(s); findings: {len(finds)}", *[("  " + _fmt(x)) for x in finds[:12]], "")
        return R(True, "", {"findings": len(finds), "files": len(files)}, ctx["findings_text"])

    def fewer(e, ctx, step, changed):
        rec = (ctx.get("scan") or {}).get(kind)
        if not rec:
            return R(None, f"no {kind} check was recorded before this step")
        if not changed:
            return R(False, "nothing was applied")
        if need_tests:
            v = _flows().verify(e, changed, run_tests=step.get("run_tests", True))
        else:
            # Pages: the scanner IS the check. The general page checklist (viewport, responsive layout) is a design bar for new pages,
            # and holding an accessibility fix to it would fail a correct fix for reasons outside its scope.
            empty = [f for f in changed if not (core.read_text(os.path.join(e.root, f), 2000)[0] or "").strip()]
            v = {"ok": not empty, "text": ("these files were emptied: " + ", ".join(empty)) if empty else ""}
        try:
            after = scan_fn(e, rec["files"], ctx)
        except Exception as ex:
            return R(None, f"the {kind} check could not run: {ex}")
        rec["after"] = after
        gone = len(rec["before"]) - len(after)
        good = v["ok"] and (gone > 0 or not rec["before"]) and len(after) <= len(rec["before"])
        why = v["text"] or (f"{gone} finding(s) gone, {len(after)} remain" if gone <= 0 else "")
        return R(good, "" if good else why, {"before": len(rec["before"]), "after": len(after)}, "\n".join(_fmt(x) for x in after[:12]))
    GATES[f"{kind}_scan"] = before
    GATES[f"{kind}_fewer"] = fewer


_pair("harden", _py_files, lambda e, files, ctx: scan_robustness(e, files))
_pair("types", _py_files, lambda e, files, ctx: scan_typing(e, files))
_pair("a11y", _html_files, lambda e, files, ctx: scan_a11y(e, files), need_tests=False)
_pair("docs", _py_files, lambda e, files, ctx: scan_docs(e, files))
_pair("migrate", lambda e, ctx: [f for f in core.list_files(e.root, e.cfg) if f.endswith((".py", ".js", ".ts", ".html"))][:200],
      lambda e, files, ctx: scan_pattern(e, files, old_pattern(ctx)))


def g_deps_check(e, ctx, step, changed):
    """Imports the code uses that are not installed, and the requirements files' view of them."""
    try:
        import arch_deps
        files = [f for f in core.list_files(e.root, e.cfg) if f.endswith(".py")]
        miss = arch_deps.missing(e.root, files)
    except Exception as ex:
        return R(None, f"the dependency check could not run: {ex}")
    ctx["findings_text"] = arch_deps.feedback(miss) if miss else "every import the code uses is installed"
    return R(not miss, "" if not miss else ctx["findings_text"], {"missing": len(miss)})


GATES["deps_check"] = g_deps_check
