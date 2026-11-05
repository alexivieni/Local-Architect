"""The Logbook: everything your agents and Claude did on this PC, kept in one SQLite file and turned into things the app can reuse.

Reads (never writes to) the sources, remembers how big each file was, so a second `ingest` only touches what grew:
  data_dir()/cc_runs/*.jsonl (+ .meta.json)   Claude Code runs started from the hub
  data_dir()/fleet/*.json                      multi-agent jobs
  data_dir()/claude_code/projects/**/*.jsonl   Claude Code's own session transcripts
  <project>/.arch/runs/*/trace.jsonl           run traces of the current folder and every portfolio project
  extra_dirs=[...]                             any other folder (e.g. your own Claude Code transcript folder)

Tables (data_dir()/logbook.db): tasks, steps, files (what was read, how big), logbook_fts (FTS5 over prompt + step text).
Every task keeps `source` and `by_claude`. `exclude_from_training()` returns the ids of Claude-made tasks so the training
export can skip them (provider terms: Claude's output must not train other models). Retrieval and playbooks may use them.

    python arch_learn.py ingest | stats | similar "text" | playbooks
"""
import glob
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter

SOURCES = ("cc_run", "fleet", "claude_code", "trace", "extra")
OK_WORDS = {"pass", "passed", "ok", "done", "success", "succeeded", "completed", "complete", "true", "accepted"}
BAD_WORDS = {"fail", "failed", "error", "errored", "false", "cancelled", "canceled", "aborted", "timeout"}
CLAUDE_WORDS = ("claude", "anthropic", "opus", "sonnet", "haiku")
STEP_TEXT_MAX = 2000
MAX_STEPS = 500
BODY_MAX = 20000
STOP = set("the a an and or to of in on for with from this that is are be it as at by into add make please can you "
           "should would use using new fix".split()) - {"fix"}


# ---------------------------------------------------------------- places
def _core():
    try:
        import arch_core
        return arch_core
    except Exception:
        return None


def data_dir():
    d = os.environ.get("ARCHITECT_DATA_DIR")
    if d:
        return d
    c = _core()
    if c is not None:
        return c.data_dir()
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_data")


def db_path():
    return os.path.join(data_dir(), "logbook.db")


def playbooks_path():
    return os.path.join(data_dir(), "memory", "playbooks.md")


def _connect():
    os.makedirs(data_dir(), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, source TEXT, title TEXT, kind TEXT, started REAL,"
                 " cost REAL, status TEXT, turns INTEGER, model TEXT, by_claude INTEGER, tools TEXT, origin TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS steps(task_id TEXT, n INTEGER, kind TEXT, tool TEXT, text TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id, n)")
    conn.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, seen REAL)")
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='logbook_fts'").fetchone()
    if not has:
        try:
            conn.execute("CREATE VIRTUAL TABLE logbook_fts USING fts5(task_id UNINDEXED, body)")
        except sqlite3.Error:                           # a build without FTS5: a plain table, searched by word counts
            conn.execute("CREATE TABLE logbook_fts(task_id TEXT, body TEXT)")
    conn.commit()
    return conn


def _fts_real(conn):
    r = conn.execute("SELECT sql FROM sqlite_master WHERE name='logbook_fts'").fetchone()
    return bool(r and "fts5" in (r[0] or "").lower())


# ---------------------------------------------------------------- reading helpers
def _load(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if isinstance(j, dict):
                    yield j
    except OSError:
        return


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().lstrip("$"))
    except (TypeError, ValueError):
        return None


def _status(v):
    """-> 'pass' | 'fail' | 'unknown' (not_sure and anything unreadable are unknown, as everywhere else in the app)."""
    if isinstance(v, bool):
        return "pass" if v else "fail"
    s = str(v or "").strip().lower()
    if s in OK_WORDS:
        return "pass"
    if s in BAD_WORDS:
        return "fail"
    return "unknown"


def _text_of(v, cap=STEP_TEXT_MAX):
    if v is None:
        return ""
    if isinstance(v, str):
        return v[:cap]
    if isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or json.dumps(b, ensure_ascii=False)))
            else:
                parts.append(str(b))
        return "\n".join(parts)[:cap]
    try:
        return json.dumps(v, ensure_ascii=False)[:cap]
    except (TypeError, ValueError):
        return str(v)[:cap]


def _is_claude(*names):
    s = " ".join(str(n or "") for n in names).lower()
    return any(w in s for w in CLAUDE_WORDS)


def _first_words(text, n=2):
    ws = re.findall(r"[a-z0-9]+", str(text or "").lower())
    ws = [w for w in ws if w not in STOP] or ws
    return " ".join(ws[:n]) or "other"


def _new_task(source, tid, origin, mtime):
    return {"id": tid, "source": source, "title": "", "kind": "", "started": mtime, "cost": 0.0, "status": "unknown",
            "turns": 0, "model": "", "by_claude": 0, "steps": [], "origin": origin}


def _step(task, kind, text="", tool=""):
    if len(task["steps"]) < MAX_STEPS:
        task["steps"].append((kind, str(tool or ""), _text_of(text)))


# ---------------------------------------------------------------- parsers
def _block_steps(task, msg, role, ev):
    """One Claude-transcript message: its text blocks, tool calls and tool results."""
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    for b in content or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            txt = b.get("text") or ""
            if role == "user" and not task["title"] and txt.strip():
                task["title"] = txt.strip()[:300]
            _step(task, "message", txt)
        elif bt == "tool_use":
            _step(task, "tool", b.get("input"), b.get("name"))
        elif bt == "tool_result":
            _step(task, "result", b.get("content"))
    if role == "assistant":
        task["turns"] += 1
        if msg.get("model"):
            task["model"] = str(msg["model"])
            if _is_claude(msg["model"]):
                task["by_claude"] = 1


def _parse_jsonl(path, source, tid, hint=None):
    """One .jsonl of events of any of the three shapes this PC produces (hub Claude runs, Claude transcripts, run traces)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = time.time()
    task = _new_task(source, tid, path, mtime)
    cost_total, cost_sum, first_t = None, 0.0, None
    verdict, marks, calls = None, [], 0
    for j in _lines(path):
        t = str(j.get("type") or "")
        if first_t is None and _num(j.get("t") if "t" in j else j.get("timestamp")) is not None:
            first_t = _num(j.get("t") if "t" in j else j.get("timestamp"))
        msg = j.get("message")
        if isinstance(msg, dict):                               # Claude Code transcript line
            role = msg.get("role") or t
            _block_steps(task, msg, role, j)
            continue
        if t == "call":
            calls += 1
            task["turns"] += 1
            if j.get("model") and not task["model"]:
                task["model"] = str(j["model"])
            if j.get("transport") == "claude" or _is_claude(j.get("model")):
                task["by_claude"] = 1
            c = _num(j.get("cost"))
            if c:
                cost_sum += c
            if not task["title"]:
                task["title"] = str(j.get("goal") or j.get("request") or "")[:300]
            _step(task, "message", "%s %s" % (j.get("role") or "", j.get("label") or ""))
        elif t == "mark":
            marks.append(j.get("ok"))
            _step(task, "check", "%s ok=%s" % (j.get("label") or j.get("gate") or j.get("id"), j.get("ok")))
        elif t == "verdict":
            verdict = j
            _step(task, "check", "verdict %s" % (j.get("verdict") or j.get("status") or j.get("ok")))
        elif t in ("tool", "tool_use"):
            _step(task, "tool", j.get("args") or j.get("input") or j.get("result") or "", j.get("tool") or j.get("name"))
        elif t in ("tool_result", "result"):
            tc = _num(j.get("total_cost_usd"))
            if tc is not None:
                cost_total = tc
            _step(task, "result", j.get("text") or j.get("result") or j.get("content") or j.get("output") or "")
            if j.get("is_error"):
                task["status"] = "fail"
        elif t in ("edit", "apply"):
            _step(task, "edit", j.get("text") or j.get("path") or j.get("file") or "")
        elif t in ("check", "test", "gate"):
            _step(task, "check", j.get("text") or j.get("name") or "")
        elif t in ("escalation", "start", "end", "usage"):
            pass
        else:                                                   # text / assistant / user / message / system
            txt = j.get("text") or j.get("content") or j.get("prompt") or ""
            if txt:
                if t in ("user", "prompt") and not task["title"]:
                    task["title"] = _text_of(txt, 300).strip()
                _step(task, "message", txt)
                if t in ("assistant", "text", "message"):
                    task["turns"] += 1
        if j.get("model") and not task["model"]:
            task["model"] = str(j["model"])
        if _is_claude(j.get("model")):
            task["by_claude"] = 1
        c = _num(j.get("cost"))
        if c and t != "call":
            cost_sum += c
        tc = _num(j.get("total_cost_usd"))
        if tc is not None:
            cost_total = tc
    task["cost"] = cost_total if cost_total is not None else cost_sum
    if first_t and first_t > 1e8:
        task["started"] = first_t if first_t < 1e11 else first_t / 1000.0
    if verdict is not None:
        st = _status(verdict.get("verdict") if verdict.get("verdict") is not None else
                     (verdict.get("status") if verdict.get("status") is not None else verdict.get("ok")))
        task["status"] = st
    elif task["status"] == "unknown" and marks:
        task["status"] = "unknown" if None in marks else ("pass" if all(marks) else "fail")
    if source in ("claude_code",) or (hint == "claude"):
        task["by_claude"] = 1
    return task


def _parse_cc_run(path):
    base = path[:-len(".jsonl")]
    task = _parse_jsonl(path, "cc_run", "cc_run:" + os.path.basename(base))
    meta = _load(base + ".meta.json")
    if isinstance(meta, dict):
        if meta.get("prompt"):
            task["title"] = str(meta["prompt"])[:300]
        if meta.get("status") is not None:
            task["status"] = _status(meta["status"])
        c = _num(meta.get("cost"))
        if c is not None:
            task["cost"] = c
        n = _num(meta.get("turns"))
        if n is not None:
            task["turns"] = int(n)
        if meta.get("model"):
            task["model"] = str(meta["model"])
    task["by_claude"] = 1
    return task


def _parse_fleet(path):
    data = _load(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = time.time()
    task = _new_task("fleet", "fleet:" + os.path.basename(path), path, mtime)
    if isinstance(data, list):
        data = {"steps": data}
    if not isinstance(data, dict):
        return task
    task["title"] = str(data.get("prompt") or data.get("task") or data.get("goal") or data.get("title") or "")[:300]
    task["status"] = _status(data.get("status") if data.get("status") is not None else data.get("ok"))
    task["cost"] = _num(data.get("cost")) or _num(data.get("total_cost_usd")) or 0.0
    task["model"] = str(data.get("model") or "")
    t = _num(data.get("started") if "started" in data else data.get("t"))
    if t and t > 1e8:
        task["started"] = t
    names = [data.get("model"), data.get("agent"), data.get("backend")]
    for key in ("agents", "steps", "results", "tasks", "events", "jobs"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict):
                names += [it.get("model"), it.get("agent"), it.get("backend")]
                if not task["model"] and it.get("model"):
                    task["model"] = str(it["model"])
                if it.get("tool"):
                    _step(task, "tool", it.get("args") or it.get("text") or "", it.get("tool"))
                else:
                    _step(task, "message", it.get("text") or it.get("output") or it.get("result") or
                          it.get("prompt") or it)
                c = _num(it.get("cost"))
                if c and not _num(data.get("cost")):
                    task["cost"] += c
            else:
                _step(task, "message", it)
        task["turns"] += len(items)
    n = _num(data.get("turns"))
    if n is not None:
        task["turns"] = int(n)
    task["by_claude"] = 1 if _is_claude(*names) else 0
    return task


def _run_title(run_dir):
    for name in ("run.json", "meta.json", "state.json", "plan.json", "request.json"):
        d = _load(os.path.join(run_dir, name))
        if isinstance(d, dict):
            for k in ("goal", "request", "task", "prompt", "title"):
                if d.get(k):
                    return str(d[k])[:300]
    return ""


def _parse_trace(path):
    run_dir = os.path.dirname(path)
    task = _parse_jsonl(path, "trace", "trace:" + os.path.abspath(run_dir))
    rt = _run_title(run_dir)
    if rt:
        task["title"] = rt
    if not task["title"]:
        task["title"] = os.path.basename(run_dir)
    d = _load(os.path.join(run_dir, "run.json"))
    if isinstance(d, dict) and d.get("flow"):
        task["kind"] = str(d["flow"])
    return task


def _sniff(path):
    for i, j in enumerate(_lines(path)):
        if isinstance(j.get("message"), dict):
            return "claude"
        if j.get("type") in ("call", "mark", "verdict"):
            return "trace"
        if i >= 20:
            break
    return "run"


def _parse_extra(path):
    kind = _sniff(path)
    tid = "extra:" + os.path.abspath(path)
    if os.path.basename(path) == "trace.jsonl" or kind == "trace":
        t = _parse_jsonl(path, "extra", tid)
    elif kind == "claude":
        t = _parse_jsonl(path, "extra", tid, hint="claude")
    else:
        t = _parse_jsonl(path, "extra", tid)
    if not t["title"]:
        t["title"] = os.path.basename(path)
    return t


# ---------------------------------------------------------------- store
def _store(conn, task):
    tid = task["id"]
    conn.execute("DELETE FROM steps WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM logbook_fts WHERE task_id=?", (tid,))
    title = task["title"] or (task["steps"][0][2][:120] if task["steps"] else "") or tid
    kind = task["kind"] or _first_words(title)
    tools = _tool_sequence([s[1] for s in task["steps"] if s[0] == "tool"])
    conn.execute("INSERT OR REPLACE INTO tasks(id, source, title, kind, started, cost, status, turns, model, by_claude,"
                 " tools, origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (tid, task["source"], title, kind, task["started"], task["cost"], task["status"], task["turns"],
                  task["model"], 1 if task["by_claude"] else 0, " > ".join(tools), task["origin"]))
    conn.executemany("INSERT INTO steps(task_id, n, kind, tool, text) VALUES(?,?,?,?,?)",
                     [(tid, i, k, tl, tx) for i, (k, tl, tx) in enumerate(task["steps"])])
    body = (title + "\n" + "\n".join(s[2] for s in task["steps"]))[:BODY_MAX]
    conn.execute("INSERT INTO logbook_fts(task_id, body) VALUES(?,?)", (tid, body))


def _tool_sequence(names, cap=12):
    out = []
    for n in names:
        if n and (not out or out[-1] != n):
            out.append(n)
    return out[:cap]


def _portfolio_roots():
    c = _core()
    home = c.HOME if c is not None else os.path.dirname(os.path.abspath(__file__))
    pf = _load(os.path.join(home, "portfolio.json")) or {}
    out = []
    items = pf.get("apps") or pf.get("portfolio") or [] if isinstance(pf, dict) else []
    for app in items:
        p = (app or {}).get("path") or (app or {}).get("folder") if isinstance(app, dict) else None
        if p and os.path.isdir(p):
            out.append(os.path.abspath(p))
    return out


def _jobs(extra_dirs, projects, portfolio, include_cwd):
    """-> [(path, fingerprint_paths, parser)]"""
    dd = data_dir()
    jobs = []
    for p in sorted(glob.glob(os.path.join(dd, "cc_runs", "*.jsonl"))):
        jobs.append((p, [p, p[:-6] + ".meta.json"], _parse_cc_run))
    for p in sorted(glob.glob(os.path.join(dd, "fleet", "*.json"))):
        jobs.append((p, [p], _parse_fleet))
    for p in sorted(glob.glob(os.path.join(dd, "claude_code", "projects", "**", "*.jsonl"), recursive=True)):
        jobs.append((p, [p], lambda q: _parse_jsonl(q, "claude_code", "claude_code:" + os.path.abspath(q), hint="claude")))
    roots = []
    if include_cwd:
        roots.append(os.path.abspath(os.getcwd()))
    roots += [os.path.abspath(r) for r in (projects or [])]
    if portfolio:
        roots += _portfolio_roots()
    seen = set()
    for r in roots:
        if r in seen:
            continue
        seen.add(r)
        for p in sorted(glob.glob(os.path.join(r, ".arch", "runs", "*", "trace.jsonl"))):
            jobs.append((p, [p], _parse_trace))
    done = {j[0] for j in jobs}
    for d in extra_dirs or []:
        for p in sorted(glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True)):
            if p not in done:
                done.add(p)
                jobs.append((p, [p], _parse_extra))
    return jobs


def ingest(extra_dirs=None, projects=None, portfolio=True, include_cwd=True):
    """Read what is new or grown. -> {"added", "updated", "skipped", "files"}. A file that has the same size as last time is not opened."""
    conn = _connect()
    res = {"added": 0, "updated": 0, "skipped": 0, "files": 0}
    try:
        for path, fp, parser in _jobs(extra_dirs, projects, portfolio, include_cwd):
            res["files"] += 1
            key = os.path.abspath(path)
            size = sum(max(_size(p), 0) for p in fp)
            row = conn.execute("SELECT size FROM files WHERE path=?", (key,)).fetchone()
            if row is not None and row["size"] == size:
                res["skipped"] += 1
                continue
            try:
                task = parser(path)
            except Exception:                                   # one odd file never stops the rest
                res["skipped"] += 1
                continue
            _store(conn, task)
            conn.execute("INSERT OR REPLACE INTO files(path, size, seen) VALUES(?,?,?)", (key, size, time.time()))
            res["updated" if row is not None else "added"] += 1
        conn.commit()
    finally:
        conn.close()
    return res


# ---------------------------------------------------------------- reading it back
def _terms(text):
    out = []
    for w in re.findall(r"[a-z0-9_]{3,}", str(text or "").lower()):
        if w not in STOP and w not in out:
            out.append(w)
    return out[:12]


def similar(task_text, k=5, include_claude=True):
    """Past tasks most like this one: [{id, source, title, status, cost, turns, model, by_claude, tools, score}]. Best first."""
    terms = _terms(task_text)
    if not terms:
        return []
    conn = _connect()
    try:
        rows = []
        if _fts_real(conn):
            q = " OR ".join('"%s"' % t for t in terms)
            try:
                rows = conn.execute(
                    "SELECT t.*, bm25(logbook_fts) AS s FROM logbook_fts JOIN tasks t ON t.id = logbook_fts.task_id"
                    " WHERE logbook_fts MATCH ? ORDER BY s LIMIT ?", (q, max(k * 4, 20))).fetchall()
            except sqlite3.Error:
                rows = []
            scored = [(-(r["s"]), r) for r in rows]
        else:
            scored = []
            for r in conn.execute("SELECT t.*, f.body FROM tasks t JOIN logbook_fts f ON f.task_id = t.id"):
                low = (r["body"] or "").lower()
                n = sum(1 for t in terms if t in low)
                if n:
                    scored.append((float(n), r))
        out = []
        for s, r in scored:
            if not include_claude and r["by_claude"]:
                continue
            boost = 0.5 if r["status"] == "pass" else (-0.25 if r["status"] == "fail" else 0.0)
            out.append((s + boost, r))
        out.sort(key=lambda x: -x[0])
        res = []
        for s, r in out[:k]:
            res.append({"id": r["id"], "source": r["source"], "title": r["title"], "status": r["status"],
                        "cost": r["cost"], "turns": r["turns"], "model": r["model"], "by_claude": bool(r["by_claude"]),
                        "tools": [x for x in (r["tools"] or "").split(" > ") if x], "score": round(s, 4)})
        return res
    finally:
        conn.close()


def stats():
    """Counts, cost by source and model, success rate, top tools, how much is Claude-made."""
    conn = _connect()
    try:
        out = {"tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
               "steps": conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]}
        out["by_source"] = {r[0]: r[1] for r in conn.execute("SELECT source, COUNT(*) FROM tasks GROUP BY source")}
        out["cost_by_source"] = {r[0]: round(r[1] or 0.0, 6) for r in
                                 conn.execute("SELECT source, SUM(cost) FROM tasks GROUP BY source")}
        out["cost_by_model"] = {(r[0] or "unknown"): round(r[1] or 0.0, 6) for r in
                                conn.execute("SELECT model, SUM(cost) FROM tasks GROUP BY model")}
        out["total_cost"] = round(conn.execute("SELECT COALESCE(SUM(cost),0) FROM tasks").fetchone()[0], 6)
        p = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pass'").fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='fail'").fetchone()[0]
        out["passed"], out["failed"], out["unknown"] = p, f, out["tasks"] - p - f
        out["success_rate"] = round(p / (p + f), 4) if (p + f) else None      # unknown results are not counted either way
        out["by_claude"] = conn.execute("SELECT COUNT(*) FROM tasks WHERE by_claude=1").fetchone()[0]
        out["top_tools"] = [(r[0], r[1]) for r in conn.execute(
            "SELECT tool, COUNT(*) c FROM steps WHERE kind='tool' AND tool != '' GROUP BY tool ORDER BY c DESC, tool LIMIT 10")]
        return out
    finally:
        conn.close()


def exclude_from_training():
    """Ids of tasks Claude or Claude Code made. The training export skips these (provider terms)."""
    conn = _connect()
    try:
        return {r[0] for r in conn.execute("SELECT id FROM tasks WHERE by_claude=1")}
    finally:
        conn.close()


def playbooks(include_claude=True, min_tasks=1):
    """Write data_dir()/memory/playbooks.md: per kind of task, what usually worked. -> the path. The file is rebuilt each time."""
    conn = _connect()
    try:
        q = "SELECT * FROM tasks" + ("" if include_claude else " WHERE by_claude=0")
        groups = {}
        for r in conn.execute(q):
            groups.setdefault(r["kind"] or "other", []).append(r)
        today = time.strftime("%Y-%m-%d")
        lines = ["# Playbooks", "",
                 "What the Logbook learned from past runs. One line per kind of task; rebuilt by `arch_learn.py playbooks`.", ""]
        for kind in sorted(groups, key=lambda x: (-len(groups[x]), x)):
            rows = groups[kind]
            if len(rows) < min_tasks:
                continue
            good = [r for r in rows if r["status"] == "pass"]
            bad = [r for r in rows if r["status"] == "fail"]
            known = len(good) + len(bad)
            rate = ("%d%% worked" % round(100.0 * len(good) / known)) if known else "outcome not known"
            avg = sum((r["cost"] or 0.0) for r in rows) / len(rows)
            seqs = Counter(r["tools"] for r in (good or rows) if r["tools"])
            usual = seqs.most_common(1)[0][0] if seqs else "no tool calls recorded"
            line = "- %s: [%s] %d task%s, %s; usual way: %s; average cost $%.4f" % (
                today, kind, len(rows), "" if len(rows) == 1 else "s", rate, usual, avg)
            fails = Counter()
            for r in bad:
                last = conn.execute("SELECT text FROM steps WHERE task_id=? AND kind IN ('check','result')"
                                    " ORDER BY n DESC LIMIT 1", (r["id"],)).fetchone()
                if last and (last[0] or "").strip():
                    fails[" ".join(last[0].split())[:80]] += 1
            if fails:
                line += "; common failure: " + fails.most_common(1)[0][0]
            lines.append(line)
        path = playbooks_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
        return path
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI
def _chore(fn, *args, **kwargs):
    """The command-line ingest and playbooks rebuild are background chores: arch_placement.run_chore puts them on the efficiency cores
    at below-normal priority (behavior.chores_on_ecores = off: right here, as before). Same result either way. The hub's Logbook
    button calls ingest() directly: a person is waiting for that page."""
    try:
        import arch_placement
    except Exception:
        return fn(*args, **kwargs)
    return arch_placement.run_chore(fn, *args, **kwargs)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "stats"
    if cmd == "ingest":
        extra = [x for x in a[1:] if os.path.isdir(x)]
        print(json.dumps(_chore(ingest, extra_dirs=extra)))
    elif cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif cmd == "similar":
        for r in similar(" ".join(a[1:])):
            print("%-7s %-9s $%.4f %s  [%s]" % (r["status"], r["source"], r["cost"] or 0.0, r["title"][:70],
                                                 " > ".join(r["tools"])))
    elif cmd == "playbooks":
        print(_chore(playbooks))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
