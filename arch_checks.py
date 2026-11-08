"""Verifiers: checks that are CODE, not opinion. Each returns a list of {"name", "ok", "detail"}.
A flow trusts these first; an LLM judge is only used for what code cannot decide (design quality).
Standard library only; Playwright is optional (used when installed).
"""
import ast
import json
import os
import re
import sqlite3
from html.parser import HTMLParser

import arch_core


def R(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": detail}


def summarize(results):
    bad = [r for r in results if not r["ok"]]
    return {"ok": not bad, "passed": len(results) - len(bad), "total": len(results),
            "failures": "\n".join(f"- {r['name']}" + (f": {r['detail']}" if r["detail"] else "") for r in bad)}


# ---------------------------------------------------------------------------------- python
def check_python(root, files):
    out = []
    for f in files:
        if f.endswith(".py"):
            try:
                text, _ = arch_core.read_text(os.path.join(root, f))
                ast.parse(text)
                out.append(R(f"{f} parses", True))
            except SyntaxError as e:
                out.append(R(f"{f} parses", False, f"line {e.lineno}: {e.msg}"))
            except OSError:
                pass
    return out


def metrics_python(root, files):
    """Simple structure numbers used to show a refactor did not make things worse."""
    funcs, longest, total_lines, deepest = 0, 0, 0, 0
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            text, _ = arch_core.read_text(os.path.join(root, f))
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        total_lines += len(text.splitlines())

        def depth(n, d=0):
            m = d
            for c in ast.iter_child_nodes(n):
                inc = 1 if isinstance(c, (ast.If, ast.For, ast.While, ast.Try, ast.With)) else 0
                m = max(m, depth(c, d + inc))
            return m
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs += 1
                longest = max(longest, (n.end_lineno or n.lineno) - n.lineno + 1)
                deepest = max(deepest, depth(n))
    return {"functions": funcs, "longest_function": longest, "lines": total_lines, "max_nesting": deepest}


# ---------------------------------------------------------------------------------- html
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _H(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems, self.ids, self.tags = [], [], [], []
        self.labels_for, self.inputs, self.buttons, self.imgs = set(), [], [], []
        self.in_label, self.in_button, self.btn_text, self.h = 0, 0, "", []
        self.lang = self.viewport = self.title = self.doctype = False
        self.style_text, self._in_style, self.external = [], False, []
        self.cur_input_in_label = False

    def handle_decl(self, d):
        if d.lower().startswith("doctype"):
            self.doctype = True

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if tag == "html" and a.get("lang"):
            self.lang = True
        if tag == "meta" and (a.get("name") or "").lower() == "viewport":
            self.viewport = True
        if tag == "title":
            self.title = True
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "label":
            self.in_label += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag == "input" and (a.get("type") or "text") not in ("hidden", "submit", "button", "reset", "image"):
            self.inputs.append({"id": a.get("id"), "aria": a.get("aria-label") or a.get("aria-labelledby"), "in_label": self.in_label > 0, "placeholder": a.get("placeholder")})
        if tag in ("select", "textarea"):
            self.inputs.append({"id": a.get("id"), "aria": a.get("aria-label") or a.get("aria-labelledby"), "in_label": self.in_label > 0, "placeholder": None})
        if tag == "button":
            self.in_button += 1
            self.btn_text = ""
            self._btn_aria = a.get("aria-label")
        if tag == "img":
            self.imgs.append("alt" in a)
        if re.match(r"h[1-6]$", tag):
            self.h.append(int(tag[1]))
        if tag == "style":
            self._in_style = True
        for k in ("src", "href"):
            if re.match(r"https?://", a.get(k) or ""):
                self.external.append(a[k])
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag == "label":
            self.in_label = max(0, self.in_label - 1)
        if tag == "style":
            self._in_style = False
        if tag == "button":
            self.in_button = max(0, self.in_button - 1)
            self.buttons.append(bool(self.btn_text.strip() or getattr(self, "_btn_aria", None)))
        if tag in VOID:
            return
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.problems.append(f"<{self.stack.pop()}> is never closed")
            self.stack.pop()
        else:
            self.problems.append(f"stray </{tag}>")

    def handle_data(self, data):
        if self.in_button:
            self.btn_text += data
        if self._in_style:
            self.style_text.append(data)


def _lum(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError:
        return None
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = map(f, rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    if la is None or lb is None:
        return None
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def check_html(text, name="page", extra_css=""):
    """extra_css: CSS text from a linked stylesheet (<link rel=stylesheet>) the caller already read from
    disk, folded into the focus/responsive/contrast checks so a page using an external .css file isn't
    penalised for styles that are not inline."""
    p = _H()
    try:
        p.feed(text)
        p.close()
    except Exception as e:
        return [R(f"{name} is parseable HTML", False, str(e))]
    out = [R(f"{name}: has doctype, lang and title", p.doctype and p.lang and p.title,
             "" if (p.doctype and p.lang and p.title) else "needs <!doctype html>, <html lang>, <title>"),
           R(f"{name}: mobile viewport meta", p.viewport),
           R(f"{name}: tags balanced", not p.problems and not p.stack, "; ".join((p.problems + [f"<{t}> never closed" for t in p.stack])[:3]))]
    dup = {i for i in p.ids if p.ids.count(i) > 1}
    out.append(R(f"{name}: ids unique", not dup, ", ".join(sorted(dup))))
    unl = [i for i in p.inputs if not (i["in_label"] or i["aria"] or (i["id"] and i["id"] in p.labels_for))]
    out.append(R(f"{name}: every field has a label", not unl, f"{len(unl)} field(s) without a label (placeholder is not a label)"))
    out.append(R(f"{name}: buttons have text", all(p.buttons) if p.buttons else True))
    out.append(R(f"{name}: images have alt", all(p.imgs) if p.imgs else True))
    out.append(R(f"{name}: one h1, headings do not skip", p.h.count(1) <= 1 and all(b - a <= 1 for a, b in zip(p.h, p.h[1:])),
                 f"headings: {p.h[:8]}"))
    css = (" ".join(p.style_text) + " " + (extra_css or "")).lower()
    full = text.lower() + " " + (extra_css or "").lower()
    out.append(R(f"{name}: visible keyboard focus style", ":focus" in full or ":focus-visible" in full, "add :focus-visible styles"))
    out.append(R(f"{name}: responsive (media query, flex or grid)", any(k in full for k in ("@media", "display:flex", "display: flex", "display:grid", "display: grid", "grid-template"))))
    cols = re.findall(r"(?<![-\w])color\s*:\s*(#[0-9a-f]{3,6})\b", css)
    bgs = re.findall(r"background(?:-color)?\s*:\s*(#[0-9a-f]{3,6})\b", css)
    if cols and bgs:
        c = contrast(cols[0], bgs[0])
        out.append(R(f"{name}: text contrast >= 4.5", bool(c and c >= 4.5), f"{c:.1f}:1" if c else "unreadable colours"))
    if p.external:
        out.append(R(f"{name}: no external resources (works offline)", False, ", ".join(p.external[:3])))
    return out


# ---------------------------------------------------------------------------------- openapi
HTTP = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def check_openapi(text, name="openapi.json"):
    try:
        doc = json.loads(text)
    except ValueError as e:
        return [R(f"{name} is valid JSON", False, str(e)[:120])]
    out = [R(f"{name} is valid JSON", True)]
    out.append(R(f"{name}: openapi 3.x with info.title and info.version",
                 str(doc.get("openapi", "")).startswith("3.") and (doc.get("info") or {}).get("title") and (doc.get("info") or {}).get("version")))
    paths = doc.get("paths") or {}
    out.append(R(f"{name}: has paths", bool(paths)))
    ids, bad_resp, bad_params, bad_body, refs = [], [], [], [], []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        declared = {(p.get("name"), p.get("in")) for p in item.get("parameters", []) if isinstance(p, dict)}
        for verb in HTTP:
            op = item.get(verb)
            if not isinstance(op, dict):
                continue
            if op.get("operationId"):
                ids.append(op["operationId"])
            resp = op.get("responses") or {}
            if not any(str(c).startswith("2") or c == "default" for c in resp):
                bad_resp.append(f"{verb.upper()} {path}")
            have = declared | {(p.get("name"), p.get("in")) for p in op.get("parameters", []) if isinstance(p, dict)}
            for pname in re.findall(r"{([^}]+)}", path):
                if (pname, "path") not in have:
                    bad_params.append(f"{verb.upper()} {path} missing path param {pname}")
            if verb in ("post", "put", "patch") and "requestBody" not in op and not any(x[1] == "query" for x in have):
                bad_body.append(f"{verb.upper()} {path}")
    out.append(R(f"{name}: every operation has a 2xx response", not bad_resp, ", ".join(bad_resp[:3])))
    out.append(R(f"{name}: path parameters are declared", not bad_params, "; ".join(bad_params[:3])))
    out.append(R(f"{name}: write operations define a request body", not bad_body, ", ".join(bad_body[:3])))
    out.append(R(f"{name}: operationIds are unique", len(ids) == len(set(ids)) and (not paths or bool(ids)), "missing or duplicate operationId"))

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "$ref" and isinstance(v, str):
                    refs.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(doc)
    broken = []
    for r in refs:
        if r.startswith("#/"):
            cur = doc
            for part in r[2:].split("/"):
                cur = cur.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(cur, dict) else None
            if cur is None:
                broken.append(r)
    out.append(R(f"{name}: all $ref targets exist", not broken, ", ".join(broken[:3])))
    return out


# ---------------------------------------------------------------------------------- sql
def _schema_checks(con, tables, name):
    """PK / FK / index / NOT NULL checks against an already-populated connection. Shared by check_sql
    (one standalone schema file) and check_sql_project (a full migrations/ directory applied in order)."""
    out = [R(f"{name}: defines tables", bool(tables))]
    nopk = []
    for t in tables:
        cols = con.execute(f"pragma table_info('{t}')").fetchall()
        if not any(c[5] for c in cols):
            nopk.append(t)
    out.append(R(f"{name}: every table has a primary key", not nopk, ", ".join(nopk)))
    fk_missing_idx, fk_bad = [], []
    for t in tables:
        for fk in con.execute(f"pragma foreign_key_list('{t}')").fetchall():
            if fk[2] not in tables:
                fk_bad.append(f"{t}->{fk[2]}")
            idx_cols = set()
            for ix in con.execute(f"pragma index_list('{t}')").fetchall():
                cols = [c[2] for c in con.execute(f"pragma index_info('{ix[1]}')").fetchall()]
                if cols:
                    idx_cols.add(cols[0])
            pk = [c[1] for c in con.execute(f"pragma table_info('{t}')").fetchall() if c[5] == 1]
            if fk[3] not in idx_cols and fk[3] not in pk:
                fk_missing_idx.append(f"{t}.{fk[3]}")
    out.append(R(f"{name}: foreign keys point at real tables", not fk_bad, ", ".join(fk_bad)))
    out.append(R(f"{name}: foreign key columns are indexed", not fk_missing_idx, ", ".join(fk_missing_idx[:4])))
    notnull = sum(1 for t in tables for c in con.execute(f"pragma table_info('{t}')").fetchall() if c[3] or c[5])
    out.append(R(f"{name}: uses NOT NULL / keys (constraints present)", notnull > 0))
    return out


def check_sql(schema_text, name="schema.sql"):
    """One standalone schema file (CREATE TABLE/INDEX statements that are self-contained)."""
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(schema_text)
    except sqlite3.Error as e:
        return [R(f"{name} runs on SQLite", False, str(e)[:160])]
    out = [R(f"{name} runs on SQLite", True)]
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    out += _schema_checks(con, tables, name)
    return out


def check_sql_project(root, dbdir, migrations, others):
    """A db/ folder with versioned migrations (applied IN ORDER to one connection), plus any sibling
    seed/queries/other .sql files checked against the resulting schema rather than in isolation.
    migrations, others: relative paths (root-relative), already sorted by caller for `migrations`.
    -> [{"name","ok","detail"}]"""
    out = []
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    ok_all = True
    for m in migrations:
        try:
            text, _ = arch_core.read_text(os.path.join(root, m))
            con.executescript(text)
            out.append(R(f"{m} applies cleanly", True))
        except (OSError, sqlite3.Error) as e:
            out.append(R(f"{m} applies cleanly", False, str(e)[:160]))
            ok_all = False
    out.append(R(f"{dbdir}: migrations apply in order", ok_all))
    if not ok_all:
        return out
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    out += _schema_checks(con, tables, dbdir)
    for f in others:
        try:
            text, _ = arch_core.read_text(os.path.join(root, f))
        except OSError as e:
            out.append(R(f"{f} is readable", False, str(e)[:120]))
            continue
        base = os.path.basename(f).lower()
        if "query" in base or "queries" in base:
            qs = dict(re.findall(r"--\s*name:\s*(\w+)\s*\n(.*?)(?=\n--\s*name:|\Z)", text, re.S))
            if not qs:
                out.append(R(f"{f}: has named queries", False, "no '-- name: <id>' blocks found"))
            for qname, sql in qs.items():
                clean = sql.strip().rstrip(";")
                params = {p: None for p in re.findall(r"[:@]([A-Za-z_]\w*)", clean)}
                try:
                    con.execute("EXPLAIN QUERY PLAN " + clean, params)
                    out.append(R(f"{f}: query '{qname}' plans against the migrated schema", True))
                except sqlite3.Error as e:
                    out.append(R(f"{f}: query '{qname}' plans against the migrated schema", False, str(e)[:160]))
        else:
            try:
                con.executescript(text)
                out.append(R(f"{f} runs on the migrated schema", True))
            except sqlite3.Error as e:
                out.append(R(f"{f} runs on the migrated schema", False, str(e)[:160]))
    return out


def run_sql_assertions(schema_text, assertions):
    """assertions: [{"sql": "...", "expect": [[..],..]}] run against the schema plus optional "setup" statements."""
    out = []
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(schema_text)
    except sqlite3.Error as e:
        return [R("schema loads", False, str(e)[:120])]
    for i, a in enumerate(assertions or []):
        try:
            if a.get("setup"):
                con.executescript(a["setup"])
            rows = [list(r) for r in con.execute(a["sql"]).fetchall()]
            exp = a.get("expect")
            ok = exp is None or rows == exp
            out.append(R(f"query {i + 1} returns expected rows", ok, "" if ok else f"got {rows[:3]}"))
        except sqlite3.Error as e:
            out.append(R(f"query {i + 1} runs", False, str(e)[:120]))
    return out


# ---------------------------------------------------------------------------------- docs
def mermaid_blocks(text):
    return re.findall(r"```mermaid\s*\n(.*?)```", text, re.S)


MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram", "stateDiagram-v2", "erDiagram", "C4Context", "C4Container", "C4Component", "journey", "gantt")


def check_mermaid(text, name="doc"):
    blocks = mermaid_blocks(text)
    if not blocks:
        return [R(f"{name}: has a Mermaid diagram", False, "add a ```mermaid block")]
    out = [R(f"{name}: has a Mermaid diagram", True)]
    for i, b in enumerate(blocks):
        lines = [l for l in b.strip().splitlines() if l.strip() and not l.strip().startswith("%%")]
        first = lines[0].split()[0] if lines else ""
        ok_type = first in MERMAID_TYPES
        bal = all(b.count(a) == b.count(c) for a, c in (("[", "]"), ("(", ")"), ("{", "}")))
        body_ok = len(lines) >= 3
        edges = any(t in b for t in ("-->", "---", "->>", "-->>", "||--", "}o--", "--|>", "<|--")) or first in ("C4Context", "C4Container", "C4Component", "journey", "gantt")
        out.append(R(f"{name}: diagram {i + 1} is well-formed", ok_type and bal and body_ok and edges,
                     "" if (ok_type and bal and body_ok and edges) else f"type={first!r} balanced={bal} lines={len(lines)} edges={edges}"))
    return out


def _sections(text):
    secs, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^#{1,4}\s*(.+?)\s*$", line)
        if m:
            cur = m.group(1).lower()
            secs[cur] = ""
        elif cur is not None:
            secs[cur] += line + "\n"
    return secs


def _has(secs, *keys, minlen=25):
    for k, v in secs.items():
        if any(x in k for x in keys) and len(v.strip()) >= minlen:
            return True
    return False


def check_adr(text, name="ADR"):
    s = _sections(text)
    return [R(f"{name}: status", _has(s, "status", minlen=3)), R(f"{name}: context", _has(s, "context")),
            R(f"{name}: decision", _has(s, "decision")), R(f"{name}: alternatives considered", _has(s, "alternative", "options", "considered")),
            R(f"{name}: consequences / trade-offs", _has(s, "consequence", "trade"))]


def check_spec(text, name="spec.md"):
    s = _sections(text)
    reqs = re.findall(r"^\s*(?:\d+[.)]|-|\*)\s+(?:\*\*)?(?:R|REQ|FR|NFR)?-?\d*", text, re.M)
    return [R(f"{name}: goals", _has(s, "goal", "objective", "purpose")), R(f"{name}: non-goals / out of scope", _has(s, "non-goal", "out of scope", "not in scope", minlen=10)),
            R(f"{name}: requirements listed", _has(s, "requirement", "feature", "stories") and len(reqs) >= 3),
            R(f"{name}: no TBD left", "tbd" not in text.lower())]


# ---------------------------------------------------------------------------------- browser (optional)
def playwright_smoke(root, rel_html, run_dir=None, timeout=20000):
    """Open the page in headless Chromium: console errors, failed requests, horizontal overflow at phone width."""
    if not arch_core.has_module("playwright"):
        return [R("browser smoke test", True, "skipped: playwright is not installed (optional)")]
    from playwright.sync_api import sync_playwright
    url = "file://" + os.path.abspath(os.path.join(root, rel_html))
    errs, failed = [], []
    out = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 800})
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("requestfailed", lambda r: failed.append(r.url))
            pg.goto(url, timeout=timeout)
            if run_dir:
                pg.screenshot(path=os.path.join(run_dir, os.path.basename(rel_html) + ".png"), full_page=True)
            pg.set_viewport_size({"width": 390, "height": 800})
            over = pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            b.close()
        out = [R(f"{rel_html}: no console errors", not errs, "; ".join(errs[:2])),
               R(f"{rel_html}: nothing fails to load", not failed, ", ".join(failed[:2])),
               R(f"{rel_html}: no sideways scroll at phone width", not over)]
    except Exception as e:
        out = [R(f"{rel_html}: opens in a browser", False, f"{type(e).__name__}: {str(e)[:120]}")]
    return out


# ---------------------------------------------------------------------------------- dispatch
def _migration_groups(root, files):
    """{dbdir: (sorted migration paths, sorted other .sql paths)} for every 'migrations' directory touched
    by `files`. A migrated schema only makes sense as a whole, so once a dbdir is implicated (even by a
    single new/changed file in it) every migration and sibling .sql file is read fresh from disk -- not
    just the ones in `files` -- or e.g. checking one new migration in isolation would see no earlier tables."""
    dbdirs = set()
    for f in files:
        m = re.match(r"^(.*?)[\\/]migrations[\\/][^\\/]+\.sql$", f.replace("\\", "/"))
        if m:
            dbdirs.add(m.group(1))
    groups = {}
    for dbdir in dbdirs:
        migdir = os.path.join(root, dbdir, "migrations")
        migs = sorted(os.path.join(dbdir, "migrations", n).replace(os.sep, "/") for n in os.listdir(migdir) if n.lower().endswith(".sql")) if os.path.isdir(migdir) else []
        base = os.path.join(root, dbdir)
        others = sorted(os.path.join(dbdir, n).replace(os.sep, "/") for n in os.listdir(base)
                         if n.lower().endswith(".sql") and os.path.isfile(os.path.join(base, n))) if os.path.isdir(base) else []
        groups[dbdir] = (migs, others)
    return groups


def check_files(root, files, cfg, run_dir=None):
    """Run every check that applies to these files. Returns a flat list (an extension may add its own: arch_core.ext_hooks)."""
    for h in arch_core.ext_hooks("check_files"):
        r = h(root, files, cfg, run_dir, _base_check_files)
        if r is not None:
            return r
    return _base_check_files(root, files, cfg, run_dir)


def _base_check_files(root, files, cfg, run_dir=None):
    """Run every check that applies to these files. Returns a flat list."""
    out = []
    use_pw = cfg["behavior"].get("playwright", "auto") != "off"
    sql_groups = _migration_groups(root, files)
    handled_sql = set()
    for dbdir, (migs, others) in sql_groups.items():
        out += check_sql_project(root, dbdir or ".", migs, others)
        handled_sql.update(migs)
        handled_sql.update(others)
    for f in files:
        if f in handled_sql:
            continue
        full = os.path.join(root, f)
        if not os.path.isfile(full):
            continue
        try:
            text, _ = arch_core.read_text(full)
        except OSError:
            continue
        low = f.lower()
        if low.endswith(".py"):
            out += check_python(root, [f])
        elif low.endswith((".html", ".htm")):
            extra_css = ""
            for href in re.findall(r'<link\b[^>]*\brel=["\']?stylesheet["\']?[^>]*\bhref=["\']([^"\']+)["\']', text, re.I) \
                    + re.findall(r'<link\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']?stylesheet["\']?', text, re.I):
                if re.match(r"https?://", href):
                    continue
                css_path = os.path.normpath(os.path.join(os.path.dirname(full), href.lstrip("/")))
                if os.path.isfile(css_path):
                    try:
                        extra_css += "\n" + arch_core.read_text(css_path)[0]
                    except OSError:
                        pass
            out += check_html(text, f, extra_css)
            if use_pw and os.path.basename(low) in ("index.html",) or (use_pw and cfg["behavior"].get("playwright") == "always"):
                out += playwright_smoke(root, f, run_dir)
        elif low.endswith(".sql"):
            out += check_sql(text, f)
        elif low.endswith(".json") and re.search(r"openapi|swagger", low + text[:300], re.I) and '"openapi"' in text:
            out += check_openapi(text, f)
        elif low.endswith(".json"):
            try:
                json.loads(text)
                out.append(R(f"{f} is valid JSON", True))
            except ValueError as e:
                out.append(R(f"{f} is valid JSON", False, str(e)[:100]))
        elif low.endswith(".md"):
            if "adr" in low or "decision" in low:
                out += check_adr(text, f)
            if "```mermaid" in text:
                out += check_mermaid(text, f)
    return out
