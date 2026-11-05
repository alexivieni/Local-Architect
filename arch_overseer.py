"""The overseer: one place that watches every Claude Code agent working on this machine and turns it into data.

What it reads (formats taken from the real files, 2026-11-04):
  * agent logs      %TEMP%/ag_*.log and <data>/learning/agent_logs/*.log, written by hub_data/learning/briefs/launch.py:
                    one "[type] text" line per event (types system.init, tool, tool_result, assistant, thinking, result; a
                    tool_result's text may run on over several lines) and a last "=== STATUS s | cost $x | turns n | error e".
                    A [tool] line holds the tool's input as JSON (cut at 600 characters, no tool name), so the tool is inferred.
  * transcripts     <data>/claude_code/projects/**/<session>.jsonl, Claude Code's own record: one JSON event per line, assistant
                    lines carry one content block each (text / thinking / tool_use) and the message's usage (repeated on every
                    block of the same message id, so it is counted once), user lines carry tool_result blocks (is_error, and
                    `toolDenialKind` when the permission check refused the call). Subagents live in <session>/subagents/.
  * hub runs        <data>/cc_runs/<id>.jsonl (arch_claudecode.record_event) + <id>.meta.json (status, cost, turns, session).
  * fleets          <data>/fleet/<id>.json (arch_fleet): which run is which named agent, and the files its brief owns.
  * ledger          <data>/learning/ledger.jsonl: {agent, status, cost_usd, turns, logged} per finished agent.

The same agent usually shows up in several of these (a launch.py agent has a log, a hub run and a transcript). Views that share a
session id are folded into one agent: the richest view (transcript > hub run > log) gives the tool counts, the others give the name,
the status and the cost. Nothing is counted twice.

The scan is incremental: byte offsets per file are kept in <data>/learning/overseer/cursor.json; only whole new lines are read; a file
that got shorter (launch.py rewrites its log) is read again from the start and its old figures are dropped.

Outputs:
  * <data>/learning/overseer/state.json     the current picture (summary() plus per-agent detail)
  * <data>/learning/overseer/timeline.jsonl one line per new observation: {t, agent, kind, detail, ...}
  * <data>/learning/LESSONS.md              lessons(): plain-language findings, appended (only when they changed)

Everything here was produced by Claude agents. Every record carries `by_claude: 1` and `use: "analysis"`: it is for the owner's
harness research and for showing in the app, and must never become weight-training data (same rule as arch_learn.exclude_from_training).

    python arch_overseer.py watch [--every 30]   scan every 30 s, one status line per agent
    python arch_overseer.py summary              one scan, the summary as JSON
    python arch_overseer.py lessons              one scan, the lessons (also appended to LESSONS.md)
"""
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time

try:
    import arch_core as core
except Exception:                                        # usable on its own (e.g. pointed at a copied data folder)
    core = None

HERE = os.path.dirname(os.path.abspath(__file__))
STALL_S = 120          # a gap longer than this between two events of one agent is a stall
LIVE_S = 900           # a "running" agent with no event for this long is no longer counted as live
MAX_SAMPLES = 5
MAX_PENDING = 300
MAX_STALLS = 20
USE = "analysis"       # never weight training

READ_TOOLS = {"Read", "NotebookRead", "view"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit_lines"}
SEARCH_TOOLS = {"Glob", "Grep", "LS", "ls"}
SHELL_TOOLS = {"Bash", "PowerShell", "Shell"}
SOURCE_RANK = {"transcript": 3, "run": 2, "log": 1}

LOG_LINE = re.compile(r"^\[([a-z_.]+)\] ?(.*)$")
STATUS_LINE = re.compile(r"^=== STATUS (\S+) \| cost \$([\d.eE+-]+|None) \| turns (\S+) \| error ?(.*)$")
INIT_TEXT = re.compile(r"session (\S+) · model (\S+) · .*?mode (\S+)")
LOG_ERROR = re.compile(r"^\s*(?:<tool_use_error>|Error\b|Permission\b|Claude requested permission|This command requires approval)", re.I)
REFUSAL = re.compile(r"requires approval|haven't granted|permission|denied|not allowed|blocked|Command contains"
                     r"|can't be checked before it runs|rejected", re.I)
TEST_CMD = re.compile(r"\b(?:pytest|unittest|arch_selftest\w*|selftest|arch_hub_check|test_\w+\.py|\w+_test\.py|npm (?:run )?test"
                      r"|node --check)\b", re.I)
FILE_TOKEN = re.compile(r"[\w./\\:-]+\.(?:py|html|md|jsonl?|ya?ml|txt|log|js|css|bat|toml|ini|cfg)\b")
ROLE = re.compile(r"You are (?:the |an? )?([\w\- ]{2,40}?) agent\b")
LOOSE_KEYS = ("file_path", "notebook_path", "path", "pattern", "command", "url", "query", "old_string", "content", "edits",
              "subagent_type", "skill", "output_mode", "glob", "description", "prompt")
LOOSE = re.compile(r'"(' + "|".join(LOOSE_KEYS) + r')"\s*:\s*("(?:[^"\\]|\\.)*"?|\[)')


# ---- small helpers --------------------------------------------------------------------------------------------------------------
def _data_dir():
    if core is not None:
        return core.data_dir()
    return os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(HERE, "hub_data")


def _iso(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _loose(text):
    """A tool's input as a dict, also from JSON that was cut off (the agent logs keep 600 characters)."""
    if isinstance(text, dict):
        return text
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        pass
    out = {}
    for k, v in LOOSE.findall(str(text or "")):
        if k in out:
            continue
        if v == "[":
            out[k] = []
            continue
        try:
            out[k] = json.loads(v if v.endswith('"') and len(v) > 1 else v + '"')
        except ValueError:
            out[k] = v.strip('"')
    return out


def _guess_tool(inp):
    """The agent logs keep a tool's input but not its name: name it from the keys it has."""
    if "command" in inp:
        return "Shell"
    if "old_string" in inp or "edits" in inp:
        return "Edit"
    if "notebook_path" in inp:
        return "NotebookEdit"
    if "file_path" in inp:
        return "Write" if "content" in inp else "Read"
    if "pattern" in inp:
        if any(k in inp for k in ("output_mode", "glob", "type", "-n", "-i", "-A", "-B", "-C", "multiline", "head_limit")):
            return "Grep"
        p = str(inp.get("pattern") or "")
        return "Glob" if re.search(r"[*?]", p) and not re.search(r"[|\\^$()+]", p) else "Grep"
    if "url" in inp:
        return "WebFetch"
    if "query" in inp:
        return "WebSearch"
    if "subagent_type" in inp or ("prompt" in inp and "description" in inp):
        return "Agent"
    if "skill" in inp:
        return "Skill"
    return "Other"


def _rel(path, base):
    """A file path as the app shows it: relative to the project when inside it, forward slashes, case-folded on Windows."""
    p = str(path or "").strip().strip('"').replace("\\", "/")
    if not p:
        return ""
    b = str(base or "").replace("\\", "/").rstrip("/")
    if b and len(p) > len(b) and p[:len(b)].lower() == b.lower() and p[len(b)] == "/":
        p = p[len(b) + 1:]
    while p.startswith("./"):
        p = p[2:]
    return p.lower() if os.name == "nt" else p


def _bump(d, k, n=1):
    if k:
        d[k] = d.get(k, 0) + n


def _role(text):
    m = ROLE.search(str(text or "")[:400])
    return (m.group(1).strip() + " agent") if m else None


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _new(key, source, path):
    return {"key": key, "source": source, "path": path, "name": None, "role": None, "session": None, "parent_session": None,
            "model": None, "mode": None, "cwd": None, "status": None, "cost": None, "turns": None, "turns_counted": 0,
            "started": None, "ended": None, "last_t": None, "events": 0, "tool_calls": {}, "files_read": {}, "files_edited": {},
            "edits": 0, "searched": {}, "shell_files": {}, "errors": 0, "refusals": 0, "refused_by_tool": {}, "tests_refused": 0,
            "error_samples": [], "refusal_samples": [], "thinking": 0,
            "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "stalls": [], "max_gap_s": 0,
            "by_claude": 1, "use": USE, "_pending": {}, "_last_tool": None, "_last_msg": None, "_last_usage": None}


# ---- the overseer ---------------------------------------------------------------------------------------------------------------
class Overseer:
    def __init__(self, data_dir=None, temp_dir=None, project=None, clock=time.time):
        self.data = os.path.abspath(data_dir or _data_dir())
        self.temp = temp_dir if temp_dir is not None else tempfile.gettempdir()
        self.project = os.path.abspath(project or HERE)
        self.clock = clock
        self.out = os.path.join(self.data, "learning", "overseer")
        self._lock = threading.Lock()
        self._obs = []
        self.cur = self._load_cursor()

    # -- persistence ---------------------------------------------------------------------------------------------------------
    def _load_cursor(self):
        try:
            with open(os.path.join(self.out, "cursor.json"), encoding="utf-8") as f:
                c = json.load(f)
            if isinstance(c, dict) and c.get("version") == 1:
                return c
        except (OSError, ValueError):
            pass
        return {"version": 1, "files": {}, "records": {}, "fleets": {}, "ledger": {}, "status": {}, "quiet": {},
                "conflicts": [], "lessons_hash": None}

    def _write_json(self, name, obj):
        os.makedirs(self.out, exist_ok=True)
        path = os.path.join(self.out, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False, default=str)
        os.replace(tmp, path)

    def _observe(self, t, agent, kind, detail, **extra):
        self._obs.append({"t": round(t if t is not None else self.clock(), 3), "agent": agent, "kind": kind,
                          "detail": str(detail or "")[:240], **extra, "by_claude": 1, "use": USE})

    def _flush_timeline(self):
        if not self._obs:
            return 0
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "timeline.jsonl"), "a", encoding="utf-8") as f:
            for o in self._obs:
                f.write(json.dumps(o, ensure_ascii=False, default=str) + "\n")
        n, self._obs = len(self._obs), []
        return n

    # -- the sources ---------------------------------------------------------------------------------------------------------
    def sources(self):
        """-> [(kind, path)] for every file worth reading right now."""
        out = []
        logs = []
        if self.temp:
            logs += glob.glob(os.path.join(self.temp, "ag_*.log"))
        logs += glob.glob(os.path.join(self.data, "learning", "agent_logs", "*.log"))
        out += [("log", p) for p in logs]
        out += [("transcript", p) for p in glob.glob(os.path.join(self.data, "claude_code", "projects", "**", "*.jsonl"),
                                                     recursive=True)]
        out += [("run", p) for p in glob.glob(os.path.join(self.data, "cc_runs", "*.jsonl"))]
        out += [("run", p[:-len(".meta.json")] + ".jsonl")                 # a run that has a meta file but no events yet
                for p in glob.glob(os.path.join(self.data, "cc_runs", "*.meta.json"))
                if not os.path.exists(p[:-len(".meta.json")] + ".jsonl")]
        out += [("ledger", p) for p in glob.glob(os.path.join(self.data, "learning", "ledger.jsonl"))]
        return sorted(set((k, os.path.abspath(p)) for k, p in out))

    def _new_lines(self, path):
        """-> (whole new lines, reset?) since the last scan; a file that got shorter is read again from the start."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return [], False
        fc = self.cur["files"].get(path) or {"pos": 0}
        reset = size < fc["pos"]
        pos = 0 if reset else fc["pos"]
        if size == pos:
            return [], reset
        with open(path, "rb") as f:
            f.seek(pos)
            chunk = f.read(size - pos)
        end = chunk.rfind(b"\n")
        if end < 0:
            self.cur["files"][path] = {"pos": pos}
            return [], reset
        self.cur["files"][path] = {"pos": pos + end + 1}
        return chunk[:end].decode("utf-8", "replace").split("\n"), reset

    def _record(self, kind, path, reset):
        key = f"{kind}:{path}"
        r = self.cur["records"].get(key)
        if r is None or reset:
            r = self.cur["records"][key] = _new(key, kind, path)
            stem = os.path.basename(path)
            stem = stem[:-6] if stem.endswith(".jsonl") else os.path.splitext(stem)[0]
            if kind == "log":
                r["name"] = stem
            elif kind == "transcript":
                if os.path.basename(os.path.dirname(path)) == "subagents":
                    r["subagent"] = stem
                else:
                    r["session"] = stem
            elif kind == "run":
                r["run"] = stem
        return r

    # -- the events ----------------------------------------------------------------------------------------------------------
    def _label(self, r):
        return r.get("name") or r.get("role") or r.get("run") or (r.get("session") or r.get("subagent") or "?")[:8]

    def _tick(self, r, t):
        """Time between events: a gap over STALL_S is a stall, noted with the tool that was running."""
        if t is None:
            return
        if r["started"] is None or t < r["started"]:
            r["started"] = t
        last = r["last_t"]
        if last is not None and t - last > STALL_S:
            gap = round(t - last, 1)
            after = (r["_last_tool"] or [None])[0]
            r["stalls"] = (r["stalls"] + [{"at": round(last, 3), "gap_s": gap, "after": after}])[-MAX_STALLS:]
            self._observe(t, self._label(r), "stall", f"quiet for {gap:.0f} s" + (f" after {after}" if after else ""), gap_s=gap)
        if last is not None:
            r["max_gap_s"] = max(r["max_gap_s"], round(t - last, 1))
        if last is None or t > last:
            r["last_t"] = t

    def _tool(self, r, name, inp, arg, tid, t):
        inp = inp if isinstance(inp, dict) else {}
        name = name or _guess_tool(inp)
        base = r.get("cwd") or self.project
        _bump(r["tool_calls"], name)
        path = inp.get("file_path") or inp.get("notebook_path")
        if not path and name in READ_TOOLS | EDIT_TOOLS:
            path = inp.get("path") or arg
        short = ""
        if name in READ_TOOLS and path:
            short = _rel(path, base)
            _bump(r["files_read"], short)
        elif name in EDIT_TOOLS and path:
            short = _rel(path, base)
            _bump(r["files_edited"], short)
            r["edits"] += 1
            self._observe(t, self._label(r), "edit", f"{name} {short}", tool=name, file=short)
        elif name in SEARCH_TOOLS:
            short = _rel(inp.get("path") or ".", base) or "."
            _bump(r["searched"], short)
        elif name in SHELL_TOOLS:
            short = str(inp.get("command") or arg or "")[:160]
            for tok in set(FILE_TOKEN.findall(short)):
                _bump(r["shell_files"], _rel(tok, base))
        else:
            short = str(arg or inp.get("description") or inp.get("query") or inp.get("url") or "")[:160]
        r["_last_tool"] = [name, short]
        if tid:
            r["_pending"][tid] = [name, short]
            if len(r["_pending"]) > MAX_PENDING:
                for k in list(r["_pending"])[:len(r["_pending"]) - MAX_PENDING]:
                    del r["_pending"][k]
        if name not in EDIT_TOOLS:
            self._observe(t, self._label(r), "tool", f"{name} {short}".strip(), tool=name)

    def _result(self, r, tid, text, is_error, denial, t):
        """A tool's answer: a refusal (the permission check said no), an error, or fine."""
        text = str(text or "")
        tool = r["_pending"].pop(tid, None) if tid else None
        tool = tool or r["_last_tool"] or ["?", ""]
        if is_error is None:                                   # the logs have no error flag: judge by the opening words
            is_error = bool(LOG_ERROR.search(text[:300]))
        if not (is_error or denial):
            return
        first = text.strip().splitlines()[0][:160] if text.strip() else ""
        if denial or REFUSAL.search(text[:600]):
            r["refusals"] += 1
            _bump(r["refused_by_tool"], tool[0])
            test = tool[0] in SHELL_TOOLS and bool(TEST_CMD.search(tool[1] or ""))
            if test:
                r["tests_refused"] += 1
            if len(r["refusal_samples"]) < MAX_SAMPLES:
                r["refusal_samples"].append({"tool": tool[0], "arg": tool[1][:120], "why": first})
            self._observe(t, self._label(r), "refusal", f"{tool[0]} refused: {first}", tool=tool[0], test=test)
        else:
            r["errors"] += 1
            if len(r["error_samples"]) < MAX_SAMPLES:
                r["error_samples"].append({"tool": tool[0], "arg": tool[1][:120], "why": first})
            self._observe(t, self._label(r), "error", f"{tool[0]}: {first}", tool=tool[0])

    def _usage(self, r, u):
        if not isinstance(u, dict):
            return
        tk = r["tokens"]
        tk["in"] += int(_num(u.get("input_tokens")) or 0)
        tk["out"] += int(_num(u.get("output_tokens")) or 0)
        tk["cached"] += int(_num(u.get("cache_read_input_tokens")) or 0)
        tk["cache_write"] += int(_num(u.get("cache_creation_input_tokens")) or 0)

    def _status(self, r, status, t, cost=None, turns=None, error=None):
        if status and status != r["status"]:
            r["status"] = status
            if status != "running":
                r["ended"] = t or r["last_t"]
        if cost is not None:
            r["cost"] = cost
        if turns is not None:
            r["turns"] = turns
        if error:
            r["error"] = str(error)[:300]

    # agent log -------------------------------------------------------------------------------------------------------------
    def _log_line(self, r, line, mtime):
        r["events"] += 1
        s = STATUS_LINE.match(line)
        if s:
            st, cost, turns, err = s.groups()
            self._status(r, st, mtime, _num(cost), int(turns) if turns.isdigit() else None,
                         None if err.strip() in ("", "None") else err.strip())
            return
        m = LOG_LINE.match(line)
        if not m:
            return                                             # the rest of a multi-line tool_result
        typ, text = m.groups()
        if typ == "system.init":
            i = INIT_TEXT.search(text)
            if i:
                r["session"], r["model"], r["mode"] = i.groups()
            r["status"] = r["status"] or "running"
        elif typ == "tool":
            self._tool(r, None, _loose(text), None, None, mtime)
        elif typ == "tool_result":
            self._result(r, None, text, None, None, mtime)
        elif typ == "assistant":
            r["turns_counted"] += 1
        elif typ == "thinking":
            r["thinking"] += 1

    # transcript ------------------------------------------------------------------------------------------------------------
    def _transcript_line(self, r, ev):
        if not isinstance(ev, dict):
            return
        r["events"] += 1
        t = _iso(ev.get("timestamp"))
        typ = ev.get("type")
        if ev.get("sessionId"):
            if r.get("subagent"):
                r["parent_session"] = r["parent_session"] or ev["sessionId"]
            else:
                r["session"] = r["session"] or ev["sessionId"]
        if ev.get("cwd") and not r["cwd"]:
            r["cwd"] = ev["cwd"]
        if ev.get("permissionMode"):
            r["mode"] = ev["permissionMode"]
        if typ == "queue-operation" and not r["role"]:
            r["role"] = _role(ev.get("content"))
            return
        if typ in ("assistant", "user", "result", "system"):
            self._tick(r, t)
        if typ == "assistant":
            msg = ev.get("message") or {}
            r["model"] = msg.get("model") or r["model"]
            mid = msg.get("id")
            if not mid or mid != r["_last_msg"]:
                r["_last_msg"] = mid
                r["turns_counted"] += 1
                self._usage(r, msg.get("usage"))
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "thinking":
                    r["thinking"] += 1
                elif b.get("type") == "tool_use":
                    self._tool(r, b.get("name"), b.get("input"), None, b.get("id"), t)
        elif typ == "user":
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, str):
                if not r["role"]:
                    r["role"] = _role(content)
                return
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = " ".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                    self._result(r, b.get("tool_use_id"), c, bool(b.get("is_error")), ev.get("toolDenialKind"), t)
        elif typ == "result":
            self._status(r, "error" if ev.get("is_error") else "done", t, _num(ev.get("total_cost_usd")),
                         ev.get("num_turns") if isinstance(ev.get("num_turns"), int) else None)

    # hub run ---------------------------------------------------------------------------------------------------------------
    def _run_line(self, r, ev):
        if not isinstance(ev, dict):
            return
        r["events"] += 1
        t = _num(ev.get("t"))
        self._tick(r, t)
        typ = ev.get("type")
        if typ == "system.init":
            i = INIT_TEXT.search(str(ev.get("text") or ""))
            if i:
                r["session"], r["model"], r["mode"] = i.groups()
        elif typ == "assistant":
            r["turns_counted"] += 1
            u = ev.get("usage")
            key = json.dumps(u, sort_keys=True, default=str) if isinstance(u, dict) else None
            if key and key != r["_last_usage"]:                # one usage per message, repeated on each of its text blocks
                r["_last_usage"] = key
                self._usage(r, u)
            r["model"] = ev.get("model") or r["model"]
        elif typ == "thinking":
            r["thinking"] += 1
        elif typ == "tool":
            self._tool(r, ev.get("name"), _loose(ev.get("text")), ev.get("arg"), ev.get("id"), t)
        elif typ == "tool_result":
            self._result(r, ev.get("id"), ev.get("text"), bool(ev.get("error")), None, t)

    def _run_meta(self, r, path):
        meta_path = path[:-6] + ".meta.json"
        try:
            mt = os.path.getmtime(meta_path)
        except OSError:
            return
        fc = self.cur["files"].setdefault(meta_path, {"pos": 0})
        if fc.get("mtime") == mt and r.get("_meta_seen"):
            return
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return
        fc["mtime"], r["_meta_seen"] = mt, True
        r["role"] = r["role"] or _role(meta.get("prompt")) or meta.get("tag")
        r["session"] = meta.get("session") or r["session"]
        r["mode"] = meta.get("mode") or r["mode"]
        r["model"] = r["model"] or meta.get("model")
        r["cwd"] = meta.get("root") or r["cwd"]
        if _num(meta.get("started")) and (r["started"] is None or meta["started"] < r["started"]):
            r["started"] = float(meta["started"])
        self._status(r, meta.get("status"), _num(meta.get("ended")), _num(meta.get("cost")),
                     meta.get("turns") if isinstance(meta.get("turns"), int) else None, meta.get("error"))
        if _num(meta.get("ended")):
            r["ended"] = float(meta["ended"])

    # fleets and ledger -----------------------------------------------------------------------------------------------------
    def _fleets(self):
        for path in glob.glob(os.path.join(self.data, "fleet", "*.json")):
            try:
                mt = os.path.getmtime(path)
                fc = self.cur["files"].setdefault(path, {"pos": 0})
                if fc.get("mtime") == mt:
                    continue
                with open(path, encoding="utf-8") as f:
                    st = json.load(f)
                fc["mtime"] = mt
            except (OSError, ValueError):
                continue
            if not isinstance(st, dict):
                continue
            fid = st.get("id") or os.path.splitext(os.path.basename(path))[0]
            self.cur["fleets"][fid] = {"phase": st.get("phase"), "verdict": st.get("verdict"), "root": st.get("root"),
                                       "agents": [{"name": a.get("name"), "run": a.get("run"), "kind": a.get("kind") or "claude",
                                                   "files": list(a.get("files") or []), "status": a.get("status")}
                                                  for a in st.get("agents") or [] if isinstance(a, dict)]}

    def _ledger_line(self, ev):
        if isinstance(ev, dict) and ev.get("agent"):
            self.cur["ledger"][str(ev["agent"])] = {"status": ev.get("status"), "cost": _num(ev.get("cost_usd")),
                                                    "turns": int(ev["turns"]) if str(ev.get("turns", "")).isdigit() else None,
                                                    "logged": ev.get("logged")}

    # -- one scan ------------------------------------------------------------------------------------------------------------
    def scan(self):
        """Read what is new in every source, update the picture, write state.json and the timeline. -> summary()."""
        with self._lock:
            for kind, path in self.sources():
                try:
                    self._scan_file(kind, path)
                except Exception as e:                         # one bad file never stops the watch
                    self._observe(None, "overseer", "read_error", f"{os.path.basename(path)}: {type(e).__name__}: {e}")
            self._fleets()
            s = self._summarise(record=True)
            self._flush_timeline()
            self._write_json("cursor.json", self.cur)
            self._write_json("state.json", {"t": round(self.clock(), 3), "by_claude": 1, "use": USE,
                                            "note": "made by Claude agents: for analysis and the app, never for weight training",
                                            **s})
            return s

    def _scan_file(self, kind, path):
        lines, reset = self._new_lines(path)
        if kind == "ledger":
            for ln in lines:
                try:
                    self._ledger_line(json.loads(ln))
                except ValueError:
                    pass
            return
        if not lines and not reset and f"{kind}:{path}" in self.cur["records"]:
            if kind == "run":
                self._run_meta(self.cur["records"][f"{kind}:{path}"], path)
            return
        r = self._record(kind, path, reset)
        if kind == "log":
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = self.clock()
            if lines:
                self._tick(r, mt)
            for ln in lines:
                self._log_line(r, ln.rstrip("\r"), mt)
            return
        for ln in lines:
            if not ln.strip():
                continue
            try:
                ev = json.loads(ln)
            except ValueError:
                continue                                       # a torn line is skipped, like arch_claudecode.load_run
            (self._transcript_line if kind == "transcript" else self._run_line)(r, ev)
        if kind == "run":
            self._run_meta(r, path)

    # -- folding the views into agents ---------------------------------------------------------------------------------------
    def _agents(self, now):
        recs = list(self.cur["records"].values())
        fleet_of = {}
        for fid, fl in self.cur["fleets"].items():
            for a in fl.get("agents") or []:
                if a.get("run"):
                    fleet_of[a["run"]] = dict(a, fleet=fid)
        groups = {}
        for r in recs:
            if r.get("subagent"):
                gid = "sub:" + r["subagent"]
            elif r.get("session"):
                gid = "session:" + r["session"]
            elif r["source"] == "log":
                gid = "log:" + (r.get("name") or r["key"])
            else:
                gid = r["key"]
            groups.setdefault(gid, []).append(r)
        names_by_session = {}
        out = []
        for gid, rs in groups.items():
            p = max(rs, key=lambda x: (SOURCE_RANK.get(x["source"], 0), x["events"]))
            a = {k: (json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v) for k, v in p.items() if not k.startswith("_")}
            a["id"], a["sources"] = gid, sorted({x["source"] for x in rs})
            del a["key"], a["path"], a["source"]
            fl = next((fleet_of[x["run"]] for x in rs if x.get("run") in fleet_of), None)
            log = next((x for x in rs if x["source"] == "log"), None)
            a["name"] = (fl or {}).get("name") or (log or {}).get("name") or next((x["role"] for x in rs if x.get("role")), None) \
                or next((x.get("run") for x in rs if x.get("run")), None) or gid.split(":", 1)[-1][:12]
            for x in rs:
                for k in ("session", "model", "mode", "cwd", "role", "run"):
                    a[k] = a.get(k) or x.get(k)
                if _num(x.get("cost")) is not None:
                    a["cost"] = max(a.get("cost") or 0.0, x["cost"])
                if isinstance(x.get("turns"), int):
                    a["turns"] = max(a.get("turns") or 0, x["turns"])
                if x.get("started") is not None:
                    a["started"] = min(a["started"], x["started"]) if a.get("started") is not None else x["started"]
                if x.get("last_t") is not None:
                    a["last_t"] = max(a["last_t"] or 0, x["last_t"])
            finals = [x for x in rs if x.get("status") and x["status"] != "running" and x["source"] in ("run", "log")]
            if finals:
                f = max(finals, key=lambda x: x.get("ended") or 0)
                a["status"], a["ended"], a["error"] = f["status"], f.get("ended") or a.get("ended"), f.get("error")
            elif any(x.get("status") == "running" for x in rs):
                a["status"] = "running"
            led = self.cur["ledger"].get(a["name"])
            if led:
                a["status"] = a["status"] if a.get("status") not in (None, "running") else (led.get("status") or a["status"])
                if led.get("cost") is not None:
                    a["cost"] = max(a.get("cost") or 0.0, led["cost"])
                if led.get("turns") is not None and not a.get("turns"):
                    a["turns"] = led["turns"]
            if not a.get("status"):
                a["status"] = "running" if a.get("last_t") and now - a["last_t"] < LIVE_S else "idle"
            a["turns"] = a.get("turns") or a.get("turns_counted") or None
            a["cost"] = round(a["cost"], 4) if a.get("cost") is not None else None
            a["quiet_s"] = round(now - a["last_t"], 1) if a.get("last_t") else None
            a["live"] = a["status"] == "running" and a["quiet_s"] is not None and a["quiet_s"] < LIVE_S
            a["tools_total"] = sum(a["tool_calls"].values())
            a["owned"] = (fl or {}).get("files") or []
            a["fleet"] = (fl or {}).get("fleet")
            base = a.get("cwd") or self.project
            owned = [_rel(f, base) for f in a["owned"]]
            a["strays"] = sorted(f for f in a["files_edited"]
                                 if owned and not any(f == o or f.startswith(o.rstrip("/") + "/") for o in owned))
            a["by_claude"], a["use"] = 1, USE
            if a.get("session"):
                names_by_session[a["session"]] = a["name"]
            out.append(a)
        for a in out:                                          # a subagent is named after the agent that started it
            if a["id"].startswith("sub:") and a.get("parent_session") in names_by_session:
                a["name"] = f"{names_by_session[a['parent_session']]} (subagent)"
        for name in set(self.cur["ledger"]) - {a["name"] for a in out}:     # finished agents whose logs are gone
            led = self.cur["ledger"][name]
            a = _new("ledger:" + name, "ledger", None)
            a = {k: v for k, v in a.items() if not k.startswith("_") and k not in ("key", "path", "source")}
            a.update(id="ledger:" + name, name=name, sources=["ledger"], status=led.get("status"), cost=led.get("cost"),
                     turns=led.get("turns"), live=False, quiet_s=None, tools_total=0, owned=[], fleet=None, strays=[])
            out.append(a)
        out.sort(key=lambda a: (not a["live"], -(a.get("started") or 0)))
        return out

    @staticmethod
    def _overlap(a, b, now):
        s1, s2 = a.get("started") or 0, b.get("started") or 0
        e1 = a.get("ended") or a.get("last_t") or now
        e2 = b.get("ended") or b.get("last_t") or now
        # an agent that never reported an end is still open, even once it has gone quiet past LIVE_S: its last event is not
        # its end (a log has no timestamps, so the other agent's start is only when its file was seen, which can be later)
        if a.get("live") or (a.get("status") == "running" and not a.get("ended")):
            e1 = now
        if b.get("live") or (b.get("status") == "running" and not b.get("ended")):
            e2 = now
        return s1 <= e2 and s2 <= e1

    def _summarise(self, record=False):
        now = self.clock()
        agents = self._agents(now)
        conflicts, overlaps = [], []
        editors = {}
        for a in agents:
            for f in a["files_edited"]:
                editors.setdefault(f, []).append(a)
        for f, eds in sorted(editors.items()):
            for i in range(len(eds)):
                for j in range(i + 1, len(eds)):
                    x, y = eds[i], eds[j]
                    if x["name"] == y["name"] or not self._overlap(x, y, now):
                        continue
                    if (x.get("parent_session") and x["parent_session"] == y.get("session")) or \
                            (y.get("parent_session") and y["parent_session"] == x.get("session")):
                        continue                               # an agent and the subagent it started work together
                    item = {"file": f, "agents": sorted([x["name"], y["name"]])}
                    (conflicts if x["live"] and y["live"] else overlaps).append(item)
        stalls = []
        for a in agents:
            for s in (a.get("stalls") or [])[-3:]:
                stalls.append({"agent": a["name"], "at": s["at"], "gap_s": s["gap_s"], "after": s.get("after"), "now": False})
            if a["live"] and (a["quiet_s"] or 0) > STALL_S:
                stalls.append({"agent": a["name"], "at": a["last_t"], "gap_s": a["quiet_s"], "after": None, "now": True})
        stalls.sort(key=lambda s: -(s["at"] or 0))
        tools, files = {}, {}
        tot = {"cost": 0.0, "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "tools": 0, "edits": 0, "errors": 0,
               "refusals": 0, "tests_refused": 0, "thinking": 0, "agents": len(agents), "live": 0}
        for a in agents:
            tot["cost"] += a.get("cost") or 0.0
            for k in tot["tokens"]:
                tot["tokens"][k] += (a.get("tokens") or {}).get(k, 0)
            tot["tools"] += a["tools_total"]
            for k in ("edits", "errors", "refusals", "tests_refused", "thinking"):
                tot[k] += a.get(k) or 0
            tot["live"] += 1 if a["live"] else 0
            for k, n in a["tool_calls"].items():
                _bump(tools, k, n)
            for f, n in a["files_read"].items():
                files.setdefault(f, {"file": f, "reads": 0, "edits": 0, "agents": set()})
                files[f]["reads"] += n
                files[f]["agents"].add(a["name"])
            for f, n in a["files_edited"].items():
                files.setdefault(f, {"file": f, "reads": 0, "edits": 0, "agents": set()})
                files[f]["edits"] += n
                files[f]["agents"].add(a["name"])
        tot["cost"] = round(tot["cost"], 4)
        busiest = sorted(files.values(), key=lambda d: -(d["reads"] + d["edits"]))[:10]
        for d in busiest:
            d["agents"] = sorted(d["agents"])
        if record:
            self._note_changes(agents, conflicts, now)
        return {"agents": agents, "totals": tot, "conflicts": conflicts, "overlaps": overlaps, "stalls": stalls[:30],
                "top_tools": sorted(tools.items(), key=lambda kv: -kv[1])[:10], "busiest_files": busiest,
                "fleets": self.cur["fleets"], "by_claude": 1, "use": USE}

    def _note_changes(self, agents, conflicts, now):
        """Timeline entries that only show between scans: a new agent, a status change, a live silence, a new conflict."""
        for a in agents:
            old = self.cur["status"].get(a["id"])
            if old is None:
                self._observe(a.get("started") or now, a["name"], "seen", f"{', '.join(a['sources'])} · {a['status']}",
                              id=a["id"])
            elif old != a["status"]:
                self._observe(now, a["name"], "status", f"{old} -> {a['status']}"
                              + (f" · ${a['cost']}" if a.get("cost") is not None else ""), id=a["id"])
            self.cur["status"][a["id"]] = a["status"]
            if a["live"] and (a["quiet_s"] or 0) > STALL_S and self.cur["quiet"].get(a["id"]) != a["last_t"]:
                self.cur["quiet"][a["id"]] = a["last_t"]
                self._observe(now, a["name"], "stall", f"no event for {a['quiet_s']:.0f} s (still running)", gap_s=a["quiet_s"],
                              live=True)
        seen = {json.dumps(c, sort_keys=True) for c in self.cur["conflicts"]}
        for c in conflicts:
            if json.dumps(c, sort_keys=True) not in seen:
                self._observe(now, " + ".join(c["agents"]), "conflict", f"both editing {c['file']}", file=c["file"])
        self.cur["conflicts"] = conflicts

    def summary(self):
        """-> {agents, totals, conflicts, overlaps, stalls, top_tools, busiest_files, fleets} from what was scanned so far."""
        with self._lock:
            return self._summarise()

    # -- lessons -------------------------------------------------------------------------------------------------------------
    def lessons(self, write=True):
        """Plain-language findings from the data. Appended to <data>/learning/LESSONS.md when they differ from the last set."""
        s = self.summary()
        agents, tot = s["agents"], s["totals"]
        out = []
        tr = [a for a in agents if a.get("tests_refused")]
        if tr:
            n = sum(a["tests_refused"] for a in tr)
            modes = sorted({a.get("mode") or "unknown" for a in tr})
            mode_s = "accept-edits mode" if modes == ["acceptEdits"] else "mode " + "/".join(modes)
            out.append(f"agents in {mode_s} tried to run tests {n} time{'s' * (n != 1)} and were refused ({len(tr)} agent"
                       f"{'s' * (len(tr) != 1)}): the orchestrator should run the tests and hand the output back")
        shell_ref = sum(sum(n for k, n in a.get("refused_by_tool", {}).items() if k in SHELL_TOOLS) for a in agents) \
            - tot["tests_refused"]
        if shell_ref > 0:
            sample = next((x["arg"] for a in agents for x in a.get("refusal_samples") or []
                           if x["tool"] in SHELL_TOOLS and not TEST_CMD.search(x["arg"] or "")), "")
            out.append(f"shell commands other than tests were refused {shell_ref} time{'s' * (shell_ref != 1)}"
                       + (f" (e.g. `{sample[:70]}`)" if sample else "")
                       + ": say in the brief which commands are unavailable, so turns are not spent trying")
        for a in agents:
            n = a["tools_total"]
            if n >= 10 and a["files_read"]:
                f, k = max(a["files_read"].items(), key=lambda kv: kv[1])
                if k / n >= 0.3:
                    out.append(f"{a['name']} spent {round(100 * k / n)}% of its tool calls ({k} of {n}) reading {f}: "
                               "put the part it needs (or an outline) in the brief")
                elif k >= 5:
                    out.append(f"{a['name']} read {f} {k} times")
            if n >= 8 and a.get("errors", 0) / n > 0.25:
                out.append(f"{a['name']}: {a['errors']} of {n} tool calls failed"
                           + (f" (first: {a['error_samples'][0]['why'][:80]})" if a.get("error_samples") else ""))
            if a.get("stalls"):
                longest = max(a["stalls"], key=lambda x: x["gap_s"])
                out.append(f"{a['name']} went quiet {len(a['stalls'])} time{'s' * (len(a['stalls']) != 1)} for more than "
                           f"{STALL_S} s (longest {longest['gap_s']:.0f} s"
                           + (f", after {longest['after']}" if longest.get("after") else "") + ")")
            if a.get("strays"):
                out.append(f"{a['name']} edited {', '.join(a['strays'][:4])} outside the files its brief named")
            if a.get("status") == "done" and a.get("mode") == "acceptEdits" and not a.get("edits") and (a.get("cost") or 0) > 0.5 \
                    and a["tools_total"]:
                out.append(f"{a['name']} finished without editing anything after ${a['cost']:.2f}")
        for c in s["conflicts"] + s["overlaps"]:
            out.append(f"{c['file']} was edited by {' and '.join(c['agents'])} while both were running: give each file to one "
                       "agent, or queue the second")
        t = tot["tokens"]
        inp = t["in"] + t["cached"] + t["cache_write"]
        if inp >= 50000:
            pct = round(100 * t["cached"] / inp)
            out.append(f"the prompt cache served {pct}% of input tokens ({t['cached']:,} of {inp:,}, measured)"
                       + (": briefs with a shared stable opening would raise it" if pct < 50 else ""))
        paid = [a for a in agents if a.get("edits") and (a.get("cost") or 0) > 0 and a.get("status") == "done"]
        if len(paid) >= 2:
            per = sorted(paid, key=lambda a: a["cost"] / max(1, len(a["files_edited"])))
            lo, hi = per[0], per[-1]
            out.append(f"cost per edited file ran from ${lo['cost'] / max(1, len(lo['files_edited'])):.2f} ({lo['name']}) to "
                       f"${hi['cost'] / max(1, len(hi['files_edited'])):.2f} ({hi['name']})")
        if write and out:
            self._append_lessons(out, tot)
        return out

    def _append_lessons(self, items, tot):
        h = hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()
        with self._lock:
            if h == self.cur.get("lessons_hash"):
                return False
            path = os.path.join(self.data, "learning", "LESSONS.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new = not os.path.exists(path)
            when = datetime.datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d %H:%M")
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write("# Lessons from watching the agents\n\nWritten by arch_overseer.py from the agents' own records. "
                            "Made from Claude agents' work: for harness research, never for weight training.\n")
                f.write(f"\n## {when} · {tot['agents']} agents, {tot['tools']} tool calls, ${tot['cost']:.2f}\n")
                for x in items:
                    f.write(f"- {x}\n")
            self.cur["lessons_hash"] = h
            self._write_json("cursor.json", self.cur)
            return True


# ---- module-level conveniences (one shared overseer per data folder) ------------------------------------------------------------
_SHARED = {}


def overseer(data_dir=None):
    d = os.path.abspath(data_dir or _data_dir())
    if d not in _SHARED:
        _SHARED[d] = Overseer(d)
    return _SHARED[d]


def scan(data_dir=None):
    return overseer(data_dir).scan()


def summary(data_dir=None):
    return overseer(data_dir).summary()


def lessons(data_dir=None, write=True):
    return overseer(data_dir).lessons(write=write)


def status_line(a):
    """One line per agent for `watch`."""
    cost = f"${a['cost']:.2f}" if a.get("cost") is not None else "  -  "
    quiet = f" quiet {a['quiet_s']:.0f}s" if a.get("live") and (a.get("quiet_s") or 0) > STALL_S else ""
    return (f"{str(a['name'])[:24]:<24} {str(a['status'])[:8]:<8} {cost:>7}  turns {a.get('turns') or 0:<4} "
            f"tools {a['tools_total']:<4} edits {a.get('edits') or 0:<3} err {a.get('errors') or 0:<3} "
            f"refused {a.get('refusals') or 0:<3}{quiet}")


def _chore(fn):
    """`watch` scans are a background chore: nobody waits on any one of them, so each goes through arch_placement.run_chore (the
    efficiency cores at below-normal priority; behavior.chores_on_ecores = off: right here, as before). The hub's Monitor page and the
    one-shot `summary` / `lessons` are waited for by a person and call scan() directly."""
    try:
        import arch_placement
    except Exception:                                    # usable on its own, without the rest of the app
        return fn()
    return arch_placement.run_chore(fn)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "summary"
    o = overseer()
    if cmd == "watch":
        every = 30.0
        if "--every" in argv:
            try:
                every = max(2.0, float(argv[argv.index("--every") + 1]))
            except (IndexError, ValueError):
                pass
        try:
            while True:
                s = _chore(o.scan)
                tot = s["totals"]
                print(f"\n{time.strftime('%H:%M:%S')}  {tot['live']} live of {tot['agents']} · ${tot['cost']:.2f} · "
                      f"{tot['tools']} tools · {tot['edits']} edits · {tot['refusals']} refused")
                for a in s["agents"]:
                    if a["live"] or (a.get("last_t") and o.clock() - a["last_t"] < 3600):
                        print("  " + status_line(a))
                for c in s["conflicts"]:
                    print(f"  ! conflict: {c['file']} edited by {' and '.join(c['agents'])}")
                sys.stdout.flush()
                time.sleep(every)
        except KeyboardInterrupt:
            return 0
    if cmd == "summary":
        s = o.scan()
        print(json.dumps({k: v for k, v in s.items() if k != "fleets"}, indent=1, ensure_ascii=False, default=str))
        return 0
    if cmd == "lessons":
        o.scan()
        items = o.lessons()
        print("\n".join(f"- {x}" for x in items) or "(nothing to learn yet)")
        return 0
    print("usage: python arch_overseer.py watch [--every 30] | summary | lessons")
    return 2


if __name__ == "__main__":
    sys.exit(main())
