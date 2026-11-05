"""Contract smoke: run the FIRST calls the request itself states, the way a user would, before the build is called finished.

Why: 30 live whole-project builds failed. Half was the contract's SHAPE (fixed by arch_flows.wire_contract); the other half was
BEHAVIOUR, and the hidden acceptance checks failed at the very first call the request spells out -- `python -m inventory --db PATH add
NAME QTY` exited 2 for every model's CLI. contract_check only binds signatures; this module actually runs those usages.

Rules (deliberately narrow; a wrong guess here would fail a correct build):
  * Only what the request writes in backticks: `python -m pkg ...` command lines, and `x = Name(args)` / `Name(args)` / `x.meth(args)`
    for names imported with `from pkg import Name`. Nothing from hidden tests, nothing invented.
  * Placeholders are substituted conservatively; anything unknown skips that exercise (never guessed):
      command line: PATH/DB/FILE/DB_PATH -> a new file in a temp folder (only when the request says the file is created if missing);
                    OUT/OUTPUT[.ext] -> a new file in a temp folder; IN/INPUT[.csv] -> a temp file holding only the header row the
                    request names in backticks (skipped when it names none); NAME/ITEM -> "item"; QTY/QUANTITY/N/COUNT -> "1";
                    `<command>` -> each subcommand the request lists after "Commands:", creating ones first and removing ones last;
                    serve/run/start/watch-style commands and requests about servers are never run as a command line.
      python calls: path/file/db-like argument -> a new temp file (only with "created if missing"); port -> 0 (only for a request
                    that describes an http.server server bound to 127.0.0.1); url -> "https://example.com/a" (only when the request
                    does not talk about fetching or downloading); keyword arguments or anything else -> skipped.
  * An http.server-style result is started on its own thread, gets ONE request to the first `GET /path` the request names (expecting
    the status it states, else anything below 500), and is shut down (a shutdown that hangs is reported).
  * "the same X always gets the same Y" for a method whose one argument is X: the method is called twice and must agree.
  * Exit 0 is required only when the request says commands exit 0; otherwise only a crash (a traceback) fails.
Everything runs network-sealed (arch_flows._sealed_env), in a fresh temp folder as the working directory, under core.run_bounded's hard
time limit; a timeout proves nothing (None for that exercise). Three outcomes: (True, text) all exercises ran and passed, (False, text)
one failed, (None, reason) nothing could be exercised.
"""
import ast
import json
import os
import re
import shutil
import sys
import tempfile

import arch_core as core

LABEL = "The request's own usage fails when run:"
CLI_TIMEOUT = 30
CALL_TIMEOUT = 45
MAX_CLI = 8

_SPAN = re.compile(r"`([^`\n]{2,200})`")
_MAIN = re.compile(r"^python3?\s+-m\s+([A-Za-z_][\w.]*)(.*)$")
_FROM = re.compile(r"^from\s+([A-Za-z_][\w.]*)\s+import\s+([A-Za-z_][\w, ]*)$")
_CALL = re.compile(r"^(?:([A-Za-z_]\w*)\s*=\s*)?([A-Za-z_]\w*)\(([^()]*)\)$")
_METH = re.compile(r"^(?:[A-Za-z_]\w*\s*=\s*)?([A-Za-z_]\w*)\.([A-Za-z_]\w*)\(([^()]*)\)$")
_CMD_MARK = re.compile(r"\b(?:sub)?commands?\s*(?::|are\b|is\b)", re.I)
_CMD_TOKEN = re.compile(r"^(?:<\s*(?:sub)?(?:command|cmd)\s*>|(?:SUB)?COMMAND|CMD)$")
_CREATED = re.compile(r"created if (?:it is |it's )?missing|created if (?:it )?(?:does not|doesn't) exist|(?:is|are|gets) created\b|"
                      r"creat(?:es|ed) (?:it|the file) if", re.I)
_EXIT0 = re.compile(r"\bexit(?:s|ing)?\s+(?:with\s+)?(?:(?:status|code)\s+)?0\b|\bexit (?:status|code) (?:of )?0\b|\breturns? (?:exit )?(?:status|code) 0\b", re.I)
_SERVERISH = re.compile(r"\bserver\b|\bserve\b|\bport\b|\blisten", re.I)
_FETCHY = re.compile(r"\bfetch|\bdownload|\bcrawl|\bscrap|\burlopen\b|\brequests\b|\bping\b|\bhttp client\b|\bvisit", re.I)
_CREATORS = ("add", "create", "new", "put", "set", "insert", "push", "init", "register", "save", "store")
_REMOVERS = ("remove", "delete", "del", "rm", "drop", "pop", "clear", "purge", "reset")
_NEVER_RUN = ("serve", "server", "start", "run", "watch", "listen", "daemon", "shell", "repl", "interactive", "loop", "forever")
_PATHY = re.compile(r"^(?:path|file|filename|fname|db|db_path|dbpath|db_file|dbfile|store|store_path|data_file|datafile|json_path|"
                    r"[a-z]+_path|[a-z]+_file)$")


# ---------------------------------------------------------------------------------------------------------------- planning
def _header(goal):
    """The CSV header row the request spells out in backticks next to the word "header" (`date,region,amount`), or None."""
    m = re.search(r"header(?:\s+row)?\b[^`.]{0,40}`([A-Za-z_][\w ]*(?:\s*,\s*[A-Za-z_][\w ]*)+)`", goal, re.I)
    return m.group(1).replace(" ", "") if m else None


def _cli_value(tok, goal, header):
    """-> ("lit", text) | ("file", None) (new temp file) | ("input", header text) | ("skip", why)."""
    if tok.startswith("-") or re.match(r"^[a-z][\w.-]*$", tok):
        return "lit", tok
    if tok.startswith("<") or not re.match(r"^[A-Z][A-Z0-9_]*(?:\.[A-Za-z0-9]+)?$", tok):
        return "skip", f"`{tok}` is not a placeholder this check knows"
    stem, _, ext = tok.partition(".")
    if stem in ("NAME", "ITEM"):
        return "lit", "item"
    if stem in ("QTY", "QUANTITY", "N", "COUNT"):
        return "lit", "1"
    if stem in ("OUT", "OUTPUT", "OUTFILE", "DEST", "DST"):
        return "file", None
    if stem in ("PATH", "DB", "FILE", "DB_PATH", "DBFILE", "STORE", "DATA"):
        if _CREATED.search(goal):
            return "file", None
        return "skip", f"`{tok}` would be a file that does not exist yet, and the request does not say it is created if missing"
    if stem in ("IN", "INPUT", "INFILE", "SRC", "SOURCE"):
        if ext.lower() == "csv" and header:
            return "input", header
        return "skip", f"`{tok}` needs input content the request does not spell out"
    return "skip", f"`{tok}` is not a placeholder this check knows"


def _order(cmds):
    def rank(c):
        v = c[0].lower()
        return 0 if v in _CREATORS else 2 if v in _REMOVERS else 1
    return sorted(cmds, key=rank)                       # stable: the request's own order inside each group


def plan(goal):
    """-> (exercises, skipped). An exercise is {"kind": "cli", "module", "tokens": [(token, (kind, value))], "display"} or
    {"kind": "py", "steps": [...], "display"}; skipped is [why]. Pure text work: nothing runs here."""
    goal = goal or ""
    spans = [(m.start(), m.group(1).strip().rstrip(";")) for m in _SPAN.finditer(goal)]
    header = _header(goal)
    exs, skipped = [], []

    # ---- command lines
    mark = _CMD_MARK.search(goal)
    subs = []
    if mark:
        for pos, s in spans:
            if pos < mark.start() or _MAIN.match(s) or "(" in s or "=" in s:
                continue
            toks = s.split()
            if toks and re.match(r"^[a-z][\w-]*$", toks[0]) and all(t.startswith("-") or re.match(r"^[a-z][\w-]*$|^[A-Z][A-Z0-9_]*$", t) for t in toks):
                if toks not in subs:
                    subs.append(toks)
    serverish = bool(_SERVERISH.search(goal))
    for pos, s in spans:
        m = _MAIN.match(s)
        if not m:
            continue
        mod, rest = m.group(1), m.group(2).split()
        if serverish:
            skipped.append(f"`{s}`: the request describes a server, so its command line is not run here")
            continue
        holes = [i for i, t in enumerate(rest) if _CMD_TOKEN.match(t)]
        if len(holes) > 1:
            skipped.append(f"`{s}`: more than one command placeholder")
            continue
        if holes:
            if not subs:
                skipped.append(f"`{s}`: the request names no commands to put in place of {rest[holes[0]]}")
                continue
            lines = [rest[:holes[0]] + sub + rest[holes[0] + 1:] for sub in _order(subs)]
        else:
            if any(t.startswith("<") for t in rest):
                skipped.append(f"`{s}`: a placeholder this check does not fill")
                continue
            lines = [rest]
        for toks in lines:
            verb = next((t for t in toks if re.match(r"^[a-z][\w-]*$", t)), "")
            if verb.lower() in _NEVER_RUN:
                skipped.append(f"`python -m {mod} {' '.join(toks)}`: a long-running command is not run here")
                continue
            vals = [(t, _cli_value(t, goal, header)) for t in toks]
            bad = [v[1] for _, v in vals if v[0] == "skip"]
            if bad:
                skipped.append(f"`python -m {mod} {' '.join(toks)}`: {bad[0]}")
                continue
            exs.append({"kind": "cli", "module": mod, "tokens": vals,
                        "display": "python -m " + mod + " " + " ".join(t if v[0] in ("file", "input") else v[1] for t, v in vals)})
    exs_cli = [x for x in exs if x["kind"] == "cli"][:MAX_CLI]

    # ---- python calls on imported names
    imported = {}
    for _, s in spans:
        m = _FROM.match(s)
        if m:
            for n in m.group(2).split(","):
                if n.strip():
                    imported[n.strip()] = m.group(1)
    steps, have, display = [], {}, []
    server_ok = bool(re.search(r"http\.server", goal) and re.search(r"127\.0\.0\.1|localhost", goal))
    get = re.search(r"\bGET\s+(/[\w/.\-]*)(?![\w/<{.\-])(?:\s*(?:->|=>|returns?|:)\s*(\d{3}))?", goal)
    same = {m.group(1) for m in re.finditer(r"\bthe same (\w+) always (?:gets|returns|gives|yields|maps to) the same\b", goal, re.I)}
    for _, s in spans:
        if _MAIN.match(s) or _FROM.match(s):
            continue
        m = _CALL.match(s)
        if m and m.group(2) in imported:
            var, name, argtext = m.group(1), m.group(2), m.group(3)
            args, why, kinds = _py_args(argtext, goal, server_ok)
            if why:
                skipped.append(f"`{s}`: {why}")
                continue
            step = {"op": "call", "var": var or "_", "mod": imported[name], "name": name, "args": args, "text": s}
            steps.append(step)
            display.append(s)
            have[var or "_"] = name
            if "port" in kinds:
                if not get:
                    skipped.append(f"`{s}`: the request names no GET endpoint to ask the server")
                    steps.pop(); display.pop(); have.pop(var or "_", None)
                    continue
                steps.append({"op": "server", "var": var or "_", "path": get.group(1), "status": int(get.group(2)) if get.group(2) else None,
                              "text": f"GET {get.group(1)}"})
                display.append(f"GET {get.group(1)}")
            continue
        m = _METH.match(s)
        if m and m.group(1) in have:
            var, meth, argtext = m.group(1), m.group(2), m.group(3)
            args, why, kinds = _py_args(argtext, goal, False)
            if why:
                skipped.append(f"`{s}`: {why}")
                continue
            names = [a.strip() for a in argtext.split(",") if a.strip()]
            steps.append({"op": "method", "var": var, "meth": meth, "args": args, "text": s,
                          "twice": len(names) == 1 and names[0] in same})
            display.append(s + (" (twice: the request says the same input always gets the same result)" if steps[-1]["twice"] else ""))
    if steps:
        exs_cli.append({"kind": "py", "steps": steps, "display": "; ".join(display)})
    return exs_cli, skipped


def _py_args(argtext, goal, server_ok):
    """-> (args as [("file"|"lit", value)], why-skipped or "", kinds used)."""
    out, kinds = [], set()
    for a in [x.strip() for x in argtext.split(",") if x.strip()]:
        if "=" in a:
            return [], "keyword arguments are not filled in by this check", kinds
        low = a.lower()
        if _PATHY.match(low):
            if not _CREATED.search(goal):
                return [], f"`{a}` would be a file that does not exist yet, and the request does not say it is created if missing", kinds
            out.append(("file", None)); kinds.add("path")
        elif low == "port":
            if not server_ok:
                return [], "a port is only exercised for an http.server server the request binds to 127.0.0.1", kinds
            out.append(("lit", 0)); kinds.add("port")
        elif low == "url":
            if _FETCHY.search(goal):
                return [], "the request may fetch the url, and this check never leaves the machine", kinds
            out.append(("lit", "https://example.com/a")); kinds.add("url")
        else:
            try:
                v = ast.literal_eval(a)
            except (ValueError, SyntaxError):
                return [], f"`{a}` is not a value this check knows how to fill", kinds
            if isinstance(v, str) and (os.path.isabs(v) or ".." in v or "/" in v or "\\" in v):
                return [], f"{a} points outside the temp folder", kinds
            if not isinstance(v, (str, int, float, bool)) or v is None:
                return [], f"`{a}` is not a simple value", kinds
            out.append(("lit", v))
    return out, "", kinds


# ---------------------------------------------------------------------------------------------------------------- running
_DRIVER = r'''
import json, sys, threading, traceback
PLAN = json.loads(sys.argv[1])
objs, servers = {}, []
def fail(text, why):
    print("SMOKE-FAIL " + json.dumps({"text": text, "why": why[-1500:]}))
    sys.stdout.flush()
    for s in servers:
        try:
            s.server_close()
        except Exception:
            pass
    sys.exit(3)
for st in PLAN:
    try:
        if st["op"] == "call":
            import importlib
            fn = getattr(importlib.import_module(st["mod"]), st["name"])
            objs[st["var"]] = fn(*st["args"])
        elif st["op"] == "method":
            o = objs[st["var"]]
            r1 = getattr(o, st["meth"])(*st["args"])
            if st["twice"]:
                r2 = getattr(o, st["meth"])(*st["args"])
                if r1 != r2:
                    fail(st["text"], "called twice with the same argument it returned %r, then %r; the request says the same input always gets the same result" % (r1, r2))
        elif st["op"] == "server":
            srv = objs[st["var"]]
            if not all(hasattr(srv, a) for a in ("serve_forever", "shutdown", "server_address")):
                fail(st["text"], "the call returned %r, not an http.server server (no serve_forever/shutdown/server_address)" % (type(srv).__name__,))
            servers.append(srv)
            host, port = srv.server_address[0], srv.server_address[1]
            if str(host) not in ("127.0.0.1", "localhost", "::1"):
                fail(st["text"], "the server is bound to %r; the request says 127.0.0.1" % (host,))
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            import urllib.request, urllib.error
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with op.open("http://127.0.0.1:%d%s" % (port, st["path"]), timeout=10) as r:
                    code, body = r.status, r.read(2000)
            except urllib.error.HTTPError as ex:
                code, body = ex.code, ex.read(2000) if hasattr(ex, "read") else b""
            except Exception as ex:
                fail(st["text"], "no HTTP answer (%s: %s): the request handler probably raised; its error is printed above" % (type(ex).__name__, ex))
            done = threading.Thread(target=srv.shutdown, daemon=True)
            done.start(); done.join(5)
            if done.is_alive():
                fail(st["text"], "srv.shutdown() did not return within 5 s (serve_forever is not running on its thread?)")
            srv.server_close()
            servers.remove(srv)
            want = st["status"]
            if (want is not None and code != want) or code >= 500:
                fail(st["text"], "answered %s%s; body: %s" % (code, "" if want is None else ", the request says %d" % want, body[:300].decode("utf-8", "replace")))
    except SystemExit:
        raise
    except BaseException:
        fail(st.get("text", "?"), traceback.format_exc())
print("SMOKE-OK")
'''


def _materialize(kind, value, tmp, n, tok=""):
    if kind == "file":
        ext = tok.partition(".")[2].lower() if "." in tok else "json"
        return os.path.join(tmp, f"smoke_{n}.{ext if re.match(r'^[a-z0-9]{1,6}$', ext) else 'json'}")
    if kind == "input":
        p = os.path.join(tmp, f"smoke_in_{n}.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(value + "\n")
        return p
    return value


def _argparse_hint(ex, out):
    pre = []
    for t, (k, _v) in ex["tokens"]:
        if not t.startswith("-") and k == "lit" and re.match(r"^[a-z]", t):
            break                                          # the first command word: what comes before it is the main parser's
        if t.startswith("-"):
            pre.append(t)
    if pre and re.search(r"usage:", out) and re.search(r"unrecognized arguments|the following arguments are required|invalid choice", out):
        return ("\nThe request writes " + ", ".join(f"`{p}`" for p in pre) + " BEFORE the command, so it belongs to the main parser "
                "(add_argument on the top-level ArgumentParser, not on each subcommand's parser): the command line must work exactly as "
                "the request writes it.")
    return ""


def smoke_contract(e, goal, detail=None, timeout=None):
    """Run the few usages the request states (see the module doc). -> (True, text) | (False, text starting with LABEL) | (None, why).
    `detail`, when a list, receives one dict per exercise: {"what", "ok", "why", "paths"}."""
    import arch_flows
    exs, skipped = plan(goal)
    if not exs:
        return None, ("nothing in the request could be exercised" + (": " + "; ".join(skipped[:4]) if skipped else " (it states no usage)"))
    detail = detail if isinstance(detail, list) else []
    env = arch_flows._sealed_env(e)
    tmp = tempfile.mkdtemp(prefix="arch_smoke_")
    ran, unsure = [], []
    try:
        exit0 = bool(_EXIT0.search(goal or ""))
        cli_files = {}                                     # one PATH placeholder = one file across the command sequence
        for i, ex in enumerate(exs):
            if ex["kind"] == "cli":
                argv, paths = [], []
                for t, (k, v) in ex["tokens"]:
                    if k in ("file", "input"):
                        key = (k, t)
                        if key not in cli_files:
                            cli_files[key] = _materialize(k, v, tmp, len(cli_files), t)
                        argv.append(cli_files[key])
                        paths.append(cli_files[key])
                    else:
                        argv.append(v)
                rc, out = core.run_bounded([sys.executable, "-B", "-m", ex["module"], *argv], tmp, timeout or CLI_TIMEOUT, env)
                rec = {"what": ex["display"], "paths": paths, "rc": rc}
                if rc is None:
                    rec.update(ok=None, why="timed out")
                    detail.append(rec)
                    unsure.append(ex["display"])
                    break                                   # later commands depend on this one
                crashed = "Traceback (most recent call last)" in out
                if (exit0 and rc != 0) or crashed:
                    why = (f"exited {rc}" + (", the request says successful commands exit 0" if exit0 and rc != 0 else "")
                           + (" with a traceback" if crashed else ""))
                    rec.update(ok=False, why=why)
                    detail.append(rec)
                    note = "".join(f"\n({t} = a new file in a temp folder)" for t, (k, _v) in ex["tokens"] if k == "file")
                    note += "".join(f"\n({t} = a temp file holding only the header row the request names)" for t, (k, _v) in ex["tokens"] if k == "input")
                    return False, (f"{LABEL}\n`{ex['display']}` {why}:{note}\n" + out.strip()[-1200:] + _argparse_hint(ex, out)
                                   + "\nThis is the request's own first usage: make it work exactly as written.")
                rec.update(ok=True, why="")
                detail.append(rec)
                ran.append(ex["display"])
            else:
                steps, paths = [], []
                for st in ex["steps"]:
                    st = dict(st)
                    if "args" in st:
                        vals = []
                        for k, v in st["args"]:
                            if k == "file":
                                p = _materialize("file", None, tmp, f"py{len(paths)}")
                                paths.append(p)
                                vals.append(p)
                            else:
                                vals.append(v)
                        st["args"] = vals
                    steps.append(st)
                rc, out = core.run_bounded([sys.executable, "-B", "-c", _DRIVER, json.dumps(steps)], tmp, timeout or CALL_TIMEOUT, env)
                rec = {"what": ex["display"], "paths": paths, "rc": rc}
                if rc is None:
                    rec.update(ok=None, why="timed out")
                    detail.append(rec)
                    unsure.append(ex["display"])
                    continue
                if "SMOKE-OK" in out and rc == 0:
                    rec.update(ok=True, why="")
                    detail.append(rec)
                    ran.append(ex["display"])
                    continue
                m = re.search(r"SMOKE-FAIL (\{.*\})", out)
                info = {}
                if m:
                    try:
                        info = json.loads(m.group(1))
                    except ValueError:
                        info = {}
                where = info.get("text") or ex["display"]
                why = info.get("why") or out.strip()[-1200:] or f"exit status {rc}"
                if m and out[:m.start()].strip():
                    why = why.rstrip() + "\nOutput before it:\n" + out[:m.start()].strip()[-800:]
                rec.update(ok=False, why=why)
                detail.append(rec)
                said = [x for x in ("path = a new file in a temp folder" if paths else "",
                                    "port = 0" if any(st.get("op") == "server" for st in steps) else "") if x]
                note = ("\n(" + "; ".join(said) + ")") if said else ""
                return False, (f"{LABEL}\n`{where}` (run as: {ex['display']}) failed:{note}\n{why.strip()[-1200:]}"
                               "\nThis is the request's own first usage: make it work exactly as written.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not ran:
        return None, "the request's usage could not be checked: " + ("; ".join(f"`{u}` timed out" for u in unsure) or "nothing ran")
    return True, "ran: " + "; ".join(f"`{r}`" for r in ran) + (f" (not sure: {'; '.join(unsure)})" if unsure else "")
