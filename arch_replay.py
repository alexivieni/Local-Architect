"""Trajectory replay: the agent tasks of this project's own Claude orchestration, turned into LOCAL training tasks.

What an agent did is in the transcripts `arch_orchestration` already reads: which repo files it edited, which `arch_selftest_*` suites
it ran. From that, with git read-only and no model:

  fixture  the files the agent edited, as they stood in the last commit BEFORE it started (`git log --until`, `git show`), at most
           6 files / 1500 lines; the suite it ran is added as read-only context when it fits;
  goal     CODE-WRITTEN: a template + the file names + the check to pass + what that check prints against the fixture + the names
           of the definitions that differ in the finished version. Never the agent's brief, never a sentence of it;
  judge    the suite(s) that FAIL against the fixture and PASS against the finished files, executed in a throwaway copy of the
           project with the local model's files laid over it. Offline, one suite < 60 s, the whole judge < 60 s.

Tasks are tagged "training", "source:replay" and (where it fits) "lesson:<id>"; whole task KINDS (the module family) are held out
(~15%, tag "holdout"). Each task carries the finished files as its reference solution and the unchanged fixture as the wrong
answer, and `build()` proves both against the real judge before it keeps the task. Nothing here calls Claude, loads a model or
writes a setting; git is only asked `log`/`show`/`rev-parse`.

    py -3.12 arch_replay.py --build      # measure the suites, build and save <data>/replay/tasks.json (minutes)
    py -3.12 arch_replay.py --list
    py -3.12 arch_replay.py --summary
"""
import ast
import concurrent.futures
import datetime
import difflib
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FLOW = "feature"
PREFIX = "train-replay"
TAG = "replay"
MAX_FILES = 6
MAX_LINES = 1500
SUITE_SECS = 60               # one suite, and the whole judge
HOLDOUT_SHARE = 0.15
LESSONS = ("wiring", "contract_first", "repair_focus", "edge_cases", "tests_present")
SOURCE_EXT = (".py", ".yaml", ".yml", ".json", ".md")
GIT_OK = ("log", "show", "rev-parse")             # read-only git, nothing else is ever asked
COPY_SKIP = {"bin", "hub_data", "__pycache__", ".git", ".arch", "node_modules", "._guard"}
_SUITE_RE = re.compile(r"arch_selftest_\w+(?:\.py)?|selftest\s+--suite\s+\w+")
SHELL = ("Bash", "PowerShell")


# ------------------------------------------------------------------ small helpers
def _data_dir():
    try:
        import arch_core as core
        return core.data_dir()
    except Exception:
        return os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(HERE, "hub_data")


def _sha(s):
    return hashlib.sha1(s.encode("utf-8", "replace") if isinstance(s, str) else s).hexdigest()


def git(root, *args, timeout=60):
    """Read-only git. -> stdout text, or None when git or the object is not there."""
    if not args or args[0] not in GIT_OK:
        raise ValueError("replay only runs read-only git: " + " ".join(args[:1]))
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _lines(text):
    return len((text or "").splitlines())


def _rel(root, path):
    """Absolute path -> repo-relative with forward slashes, or None when it is outside the project."""
    try:
        r = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        return None
    if r.startswith("..") or os.path.isabs(r):
        return None
    return r.replace("\\", "/")


def _iso(ts):
    try:
        return datetime.datetime.strptime(ts.replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ reading the trajectories
def _first_prompt(path):
    """The agent's whole first user message (the brief). Used only to prove no goal copies it."""
    import arch_orchestration as o
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if isinstance(j, dict) and j.get("type") == "user" and not j.get("isMeta"):
                    t = o._text((j.get("message") or {}).get("content"))
                    if t.strip():
                        return t
    except OSError:
        pass
    return ""


def _suite_files(root, mentions):
    out = []
    for m in mentions:
        m = m.strip()
        if m.startswith("selftest"):
            name = "arch_selftest_" + m.split()[-1] + ".py"
        else:
            name = m if m.endswith(".py") else m + ".py"
        if os.path.isfile(os.path.join(root, name)) and name not in out:
            out.append(name)
    return out


def _commands(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                m = j.get("message") if isinstance(j, dict) and isinstance(j.get("message"), dict) else {}
                for b in (m.get("content") or []) if isinstance(m.get("content"), list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in SHELL:
                        out.append(str((b.get("input") or {}).get("command") or ""))
    except OSError:
        pass
    return out


def agents(root=HERE, claude_base=None, near=None):
    """-> every Claude agent that edited project files and ran a selftest suite:
    {id, label, start, edited [repo-relative], suites [files], prompt (full brief, never put in a task), session}."""
    import arch_orchestration as o
    out = []
    for pdir in o.project_dirs(claude_base, near or root):
        for p in sorted(glob.glob(os.path.join(pdir, "**", "agent-*.jsonl"), recursive=True)):
            r = o.parse_transcript(p)
            edited = []
            for e in r.get("edited") or []:
                rel = _rel(root, e)
                if rel and rel not in edited:
                    edited.append(rel)
            if not edited or not r.get("start"):
                continue
            counts = {}
            for c in _commands(p):
                for s in _suite_files(root, _SUITE_RE.findall(c)):
                    counts[s] = counts.get(s, 0) + 1
            if not counts:
                continue
            meta = o._load_json(p[:-len(".jsonl")] + ".meta.json") or {}
            aid = os.path.basename(p)[len("agent-"):-len(".jsonl")]
            out.append({"id": aid, "label": meta.get("description") or "", "start": r["start"], "edited": edited,
                        "suites": [s for s, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
                        "prompt": _first_prompt(p), "session": r.get("session_id")})
    out.sort(key=lambda a: (a["start"], a["id"]))
    return out


def briefs(root=HERE, claude_base=None, near=None, ags=None):
    """Every brief text (and agent label) of the trajectories, for the no-Claude-text proof."""
    out = []
    for a in (ags if ags is not None else agents(root, claude_base, near)):
        out += [t for t in (a.get("prompt"), a.get("label")) if t]
    return out


def leaks(text, texts, n=20):
    """True when any n-character window of `text` also occurs in one of `texts` (case and whitespace folded)."""
    def fold(s):
        return re.sub(r"\s+", " ", s.lower())
    t = fold(text)
    if len(t) < n:
        return False
    grams = {t[i:i + n] for i in range(len(t) - n + 1)}
    for b in texts:
        b = fold(b)
        if len(b) >= n and any(b[i:i + n] in grams for i in range(len(b) - n + 1)):
            return True
    return False


# ------------------------------------------------------------------ the fixture: files before the work
def before_state(root, rel, start):
    """The text of `rel` in the last commit made at or before `start` (a transcript timestamp). None = not in git then."""
    when = _iso(start)
    if when is None:
        return None
    rev = git(root, "log", "-1", "--until=" + when.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--format=%H", "--", rel)
    rev = (rev or "").strip()
    if not rev:
        return None
    return git(root, "show", f"{rev}:{rel}")


def _defs(src):
    """{qualified name: source segment} of the top-level functions, classes and their methods."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}

    def walk(body, prefix):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + n.name
                out[q] = ast.get_source_segment(src, n) or ""
                if isinstance(n, ast.ClassDef):
                    walk(n.body, q + ".")
    walk(tree.body, "")
    return out


def changed_defs(pre, post, cap=10):
    """Names of the definitions that are new or different in `post`, biggest change first."""
    a, b = _defs(pre), _defs(post)
    rows = []
    for name, seg in b.items():
        if name not in a:
            rows.append((len(seg), name))
        elif a[name] != seg:
            r = difflib.SequenceMatcher(None, a[name], seg, autojunk=False).quick_ratio() if len(seg) < 4000 else 0.5
            rows.append(((1 - r) * len(seg) + 1, name))
    rows.sort(key=lambda x: (-x[0], x[1]))
    names = [n for _, n in rows]
    parents = {n.split(".")[0] for n in names if "." in n}
    names = [n for n in names if not (n in parents and any(m.startswith(n + ".") for m in names))]     # a class whose methods are listed
    return names[:cap]


# ------------------------------------------------------------------ the judge: suites in a throwaway copy
def _env(tmp):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ARCHITECT_EVAL", "ARCHITECT_EVENTS")}
    home = os.path.join(tmp, "_home")
    os.makedirs(home, exist_ok=True)
    env.update({"ARCHITECT_HOME": home, "ARCHITECT_DATA_DIR": os.path.join(home, "data"), "ARCHITECT_NO_CACHE": "1",
                "ARCHITECT_NO_NOTIFY": "1", "ARCHITECT_HEAVY_HELD": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def _ignore(d, names):
    skip = set()
    for n in names:
        if n in COPY_SKIP or n.startswith("architect_config.json") or n.endswith((".pyc", ".bak")):
            skip.add(n)
    return skip


def copy_project(repo, dest):
    """The project as it stands now, minus models, measured data, caches and every settings file (never the real config)."""
    shutil.copytree(repo, dest, ignore=_ignore, dirs_exist_ok=True)


def run_suite(work, suite, timeout=SUITE_SECS):
    """Run one suite file inside the throwaway copy `work`. -> (ok True/False/None, seconds, output tail). None = could not run."""
    env = _env(work)
    t0 = time.time()
    argv = [sys.executable, "-B", suite]
    try:
        try:
            import arch_core as core
            rc, out = core.run_bounded(argv, work, timeout, env)
        except ImportError:
            p = subprocess.run(argv, cwd=work, env=env, capture_output=True, timeout=timeout)
            rc, out = p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return False, round(time.time() - t0, 1), f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as e:
        return None, round(time.time() - t0, 1), str(e)
    secs = round(time.time() - t0, 1)
    if rc is None:
        return False, secs, f"timed out after {timeout}s\n" + (out or "")[-400:]
    return rc == 0, secs, (out or "")[-3000:]


def run_judge(root, spec, repo=HERE):
    """The judge of a replay task. `root` = the folder holding the model's files; `spec` = {"suites": [...], "sources": [...]}.
    The model's version of each source file is laid over a copy of the project, then each suite runs there.
    -> (ok True/False/None, [(suite, ok, seconds, tail)], reason)."""
    work = tempfile.mkdtemp(prefix="replayjudge_")
    try:
        copy_project(repo, work)
        for rel in spec.get("sources") or []:
            src = os.path.join(root, rel)
            if os.path.isfile(src):
                dst = os.path.join(work, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
            else:
                return False, [], f"{rel} is missing"
        res = []
        for s in spec.get("suites") or []:
            ok, secs, tail = run_suite(work, s)
            res.append((s, ok, secs, tail))
            if ok is not True:
                break
        if any(r[1] is None for r in res):
            return None, res, "a check could not run"
        bad = [r for r in res if not r[1]]
        if bad:
            last = [l for l in bad[0][3].strip().splitlines() if l.strip()][-1:] or ["no output"]
            return False, res, f"{bad[0][0]} fails: {last[0][:160]}"
        return True, res, ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def judge_main(root, spec_json, repo=HERE):
    """Entry point of the task's python_assert: exit 0 = every suite passed against the model's files."""
    ok, res, why = run_judge(root, json.loads(spec_json), repo)
    for s, g, secs, _ in res:
        print(f"{s}: {'ok' if g else 'FAIL' if g is False else 'could not run'} ({secs}s)")
    if ok is not True:
        print(why or "failed")
    return 0 if ok else 1


def judge_code(spec, repo=HERE):
    return ("import sys\nsys.path.insert(0, %r)\nimport arch_replay\n"
            "sys.exit(arch_replay.judge_main('.', %r, %r))\n" % (HERE, json.dumps(spec, sort_keys=True), repo))


# ------------------------------------------------------------------ the goal: code, not Claude
def failure_lines(tail, cap=5):
    """What a failing suite printed that matters: its FAIL/Error/assert lines (from the suite's own code), with paths cut."""
    keep = []
    for l in (tail or "").splitlines():
        l = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*[\\/]", "", l).strip()
        l = re.sub(r"replayjudge_\w+|famjudge_\w+|archcur_\w+", "<tmp>", l)
        if re.match(r"\W*(pass|ok|passed)(\W|$)", l, re.I):
            continue
        if l and re.search(r"fail|error|assert|expected|not ok|✗|✘|missing|cannot|no module|has no attribute", l, re.I) and l not in keep:
            keep.append(l[:150])
    return keep[-cap:] if keep else [l.strip()[:150] for l in (tail or "").strip().splitlines()[-2:] if l.strip()]


def goal_text(files, suites, failing, names, context=()):
    fl = ", ".join(f"`{f}`" for f in files)
    ck = " and ".join(f"`python {s}`" for s in suites)
    g = f"Change {fl} so that the project check {ck} passes. Everything else must keep working; use only Python's built-in modules."
    if failing:
        g += " Against the code as it is now the check prints: " + " | ".join(failing) + "."
    if names:
        g += " The finished version differs from the current one in these definitions: " + ", ".join(f"`{n}`" for n in names) + "."
    if context:
        g += " " + ", ".join(f"`{c}`" for c in context) + (" is" if len(context) == 1 else " are") + " there to read; do not edit " + \
             ("it." if len(context) == 1 else "them.")
    return g


def lesson_for(failing):
    t = " ".join(failing).lower()
    if re.search(r"importerror|cannot import|no module|has no attribute|nameerror|not defined|missing", t):
        return "contract_first"
    if re.search(r"expected|!=|assert", t):
        return "edge_cases"
    return "repair_focus"


def kind_of(task_files):
    """The task kind = the module family of the biggest edited source: arch_index.py -> "index"."""
    stem = os.path.splitext(os.path.basename(task_files[0]))[0]
    stem = re.sub(r"^arch_", "", stem)
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "misc"


def pick_holdout(kinds, share=HOLDOUT_SHARE):
    """kinds: {kind: task count}. -> set of kinds held out WHOLE (about `share` of the tasks, chosen by a stable hash of the name)."""
    total = sum(kinds.values())
    if total < 4:
        return set()
    want = max(1, round(total * share))
    held, n = set(), 0
    for k in sorted(kinds, key=lambda k: (_sha("holdout:" + k), k)):
        if n >= want:
            break
        if n + kinds[k] > want + 1 and n:
            continue
        held.add(k)
        n += kinds[k]
    return held


# ------------------------------------------------------------------ building the tasks
def fingerprint(repo=HERE):
    h = hashlib.sha1()
    for p in sorted(glob.glob(os.path.join(repo, "*"))):
        if os.path.isfile(p) and not os.path.basename(p).startswith("architect_config"):
            try:
                st = os.stat(p)
            except OSError:
                continue
            h.update(f"{os.path.basename(p)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def _task_dir(data_dir=None):
    d = os.path.join(data_dir or _data_dir(), "replay")
    os.makedirs(d, exist_ok=True)
    return d


def _cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
            return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(path, cache):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass


def candidates(root=HERE, claude_base=None, near=None, ags=None):
    """Agents -> candidate tasks before any suite runs: files before the work, within the size limits, something actually changed."""
    out = []
    for a in (ags if ags is not None else agents(root, claude_base, near)):
        src, pre, post = [], {}, {}
        for rel in a["edited"]:
            base = os.path.basename(rel)
            if base.startswith(("arch_selftest", "arch_hub_check")) or not rel.lower().endswith(SOURCE_EXT) or "/" in rel and rel.split("/")[0] in COPY_SKIP:
                continue
            cur = _read(os.path.join(root, rel))
            old = before_state(root, rel, a["start"])
            if cur is None or old is None or cur == old:
                continue
            src.append(rel)
            pre[rel], post[rel] = old.replace("\r\n", "\n"), cur.replace("\r\n", "\n")
        if not src or len(src) > MAX_FILES:
            continue
        if sum(_lines(t) for t in pre.values()) > MAX_LINES:
            continue
        src.sort(key=lambda r: (-len(list(difflib.unified_diff(pre[r].splitlines(), post[r].splitlines(), lineterm="", n=0))), r))
        out.append({"agent": a["id"], "start": a["start"], "sources": src, "pre": pre, "post": post, "suites": a["suites"]})
    return out


MEASURE_TTL = 12 * 3600


def measure(suites, root=HERE, cache=None, workers=3, log=None):
    """Run each suite once against the project as it stands. -> {suite: {"ok", "secs"}}; `cache` keeps a result for 12 hours
    (the project changes while agents work, a suite's speed does not; the judge is proven again when a task is made)."""
    cache = cache if cache is not None else {}
    now = time.time()
    todo = [s for s in suites if not (isinstance(cache.get("m:" + s), dict) and now - cache["m:" + s].get("ts", 0) < MEASURE_TTL)]
    if todo:
        work = tempfile.mkdtemp(prefix="replaymeasure_")
        try:
            copy_project(root, work)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                for s, (ok, secs, tail) in zip(todo, ex.map(lambda s: run_suite(work, s), todo)):
                    cache["m:" + s] = {"ok": ok, "secs": secs, "ts": time.time()}
                    if log:
                        log(f"  base {s}: {ok} {secs}s")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return {s: cache["m:" + s] for s in suites}


def prose(goal):
    """A goal without its `code spans`: file names, check commands and definition names must be named, everything else is compared."""
    return re.sub(r"`[^`]*`", " ", goal)


def _against_fixture(root, cand, suites):
    """Each suite against the project with the fixture (pre-work) files laid over it. -> {suite: (ok, secs, tail)}."""
    work = tempfile.mkdtemp(prefix="replaypre_")
    try:
        copy_project(root, work)
        for rel, text in cand["pre"].items():
            with open(os.path.join(work, rel), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        out = {}
        for s in suites:
            out[s] = run_suite(work, s)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def make_task(cand, suites, failing, root=HERE, holdout=False):
    files = {rel: cand["pre"][rel] for rel in cand["sources"]}
    cand = dict(cand, context=[])
    text = (_read(os.path.join(root, suites[0])) or "").replace("\r\n", "\n")      # the first judging suite is there to read, when it fits
    if text and len(files) < MAX_FILES and sum(_lines(t) for t in files.values()) + _lines(text) <= MAX_LINES:
        files[suites[0]] = text
        cand["context"] = [suites[0]]
    names = []
    for rel in cand["sources"]:
        for n in changed_defs(cand["pre"][rel], cand["post"][rel], 6):
            if n not in names:
                names.append(n)
    kind = kind_of(cand["sources"])
    lesson = lesson_for(failing)
    spec = {"suites": list(suites), "sources": list(cand["sources"])}
    tags = ["training", "source:replay", "replay", "lesson:" + lesson, "kind:" + kind] + (["holdout"] if holdout else [])
    return {"id": f"{PREFIX}-{kind}-{_sha(cand['agent'] + cand['start'])[:6]}", "template": "replay_" + kind, "kind": kind,
            "family": TAG, "flow": FLOW, "lesson": lesson, "tags": tags,
            "goal": goal_text(cand["sources"], suites, failing, names, cand["context"]),
            "params": {"domains": ["api"]}, "fixture": {"files": files},
            "budget": {"calls": 24, "tokens": 90000, "seconds": 1500},
            "judge": [{"type": "files_unchanged", "paths": list(cand["context"])} if cand["context"] else None,
                      {"type": "python_assert", "code": judge_code(spec, HERE if root == HERE else root), "timeout": 150}],
            "replay": {"suites": list(suites), "sources": list(cand["sources"]), "context": list(cand["context"]), "agent": cand["agent"]},
            "solution": {"files": {rel: cand["post"][rel] for rel in cand["sources"]}},
            "wrong": {"files": {rel: cand["pre"][rel] for rel in cand["sources"]}}}


def build(root=HERE, claude_base=None, near=None, data_dir=None, ags=None, log=print, save=True, workers=3):
    """-> {"tasks": [...], "stats": {...}}. Runs the suites (minutes on the real project), keeps only tasks whose judge is proven:
    the finished files pass, the fixture fails, every suite finishes in under SUITE_SECS and all together under SUITE_SECS too."""
    ags = ags if ags is not None else agents(root, claude_base, near)
    cands = candidates(root, ags=ags)
    stats = {"agents": len(ags), "candidates": len(cands), "dropped": {}}

    def drop(why):
        stats["dropped"][why] = stats["dropped"].get(why, 0) + 1

    cpath = os.path.join(_task_dir(data_dir), "measure.json")
    cache = _cache(cpath)
    allsuites = sorted({s for c in cands for s in c["suites"]})
    log(f"replay: {len(ags)} agents with suites, {len(cands)} fit the size limits, {len(allsuites)} suites to time")
    base = measure(allsuites, root, cache, workers, log)
    _save_cache(cpath, cache)
    good = {s for s, m in base.items() if m["ok"] is True and m["secs"] < SUITE_SECS}
    stats["suites_runnable"] = sorted(good)
    stats["suites_rejected"] = sorted(set(allsuites) - good)
    kept, seen = [], set()
    for c in cands:
        use = [s for s in c["suites"] if s in good]
        if not use:
            drop("no runnable suite")
            continue
        res = {}
        for s in use:
            ck = f"pre:{c['agent']}:{s}"
            if isinstance(cache.get(ck), dict) and time.time() - cache[ck].get("ts", 0) < MEASURE_TTL:
                res[s] = tuple(cache[ck]["r"])
        if len(res) < len(use):
            for s, r in _against_fixture(root, c, [x for x in use if x not in res]).items():
                res[s] = r
                cache[f"pre:{c['agent']}:{s}"] = {"r": list(r), "ts": time.time()}
        _save_cache(cpath, cache)
        fail = [s for s in use if res[s][0] is False and "timed out" not in res[s][2][:20]]
        if not fail:
            drop("fixture already passes (no signal)")
            continue
        fail.sort(key=lambda s: (base[s]["secs"], s))
        pick, tot = [], 0.0
        for s in fail[:3]:
            if tot + base[s]["secs"] < SUITE_SECS:
                pick.append(s)
                tot += base[s]["secs"]
        if not pick:
            drop("judge too slow")
            continue
        key = (tuple(sorted((r, _sha(t)) for r, t in c["pre"].items())), tuple(pick))
        if key in seen:
            drop("duplicate")
            continue
        seen.add(key)
        failing = failure_lines(res[pick[0]][2])
        kept.append((c, pick, failing))
    kinds = {}
    for c, _, _ in kept:
        k = kind_of(c["sources"])
        kinds[k] = kinds.get(k, 0) + 1
    held = pick_holdout(kinds)
    tasks = []
    texts = briefs(ags=ags)
    for c, pick, failing in kept:
        t = make_task(c, pick, failing, root, kind_of(c["sources"]) in held)
        if leaks(prose(t["goal"]), texts):
            failing = failing[:0]                       # a line echoing a brief goes; the rest of the goal is code-written
            t = make_task(c, pick, failing, root, kind_of(c["sources"]) in held)
            if leaks(prose(t["goal"]), texts):
                drop("goal echoes a brief")
                continue
        t["judge"] = [j for j in t["judge"] if j]
        # proof against the real judge: finished files pass, the fixture fails
        spec = {"suites": pick, "sources": c["sources"]}
        for label, files, want in (("solution", t["solution"]["files"], True), ("wrong", t["wrong"]["files"], False)):
            d = tempfile.mkdtemp(prefix="replayproof_")
            try:
                for rel, text in files.items():
                    p = os.path.join(d, rel)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text)
                ok, _, _ = run_judge(d, spec, root)
            finally:
                shutil.rmtree(d, ignore_errors=True)
            if ok is not want:
                drop(f"{label} not proven")
                t = None
                break
        if t:
            tasks.append(t)
    stats.update(tasks=len(tasks), holdout=sum(1 for t in tasks if "holdout" in t["tags"]),
                 kinds=sorted({t["kind"] for t in tasks}), holdout_kinds=sorted(held))
    res = {"built": time.time(), "fingerprint": fingerprint(root), "tasks": tasks, "stats": stats}
    if save:
        with open(os.path.join(_task_dir(data_dir), "tasks.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    return res


def load_tasks(data_dir=None):
    """The saved tasks (what the eval family serves); [] when `--build` has not run in this data folder."""
    j = _cache(os.path.join(_task_dir(data_dir), "tasks.json"))
    return [t for t in (j.get("tasks") or []) if isinstance(t, dict)]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args(argv)
    if a.build:
        r = build()
        print(json.dumps(r["stats"], indent=1))
        return 0
    ts = load_tasks()
    if a.summary or not a.list:
        j = _cache(os.path.join(_task_dir(), "tasks.json"))
        print(json.dumps(j.get("stats") or {"tasks": 0, "note": "run --build"}, indent=1))
    if a.list:
        for t in ts:
            print(f"  {t['id']:40} {t['lesson']:15} {'HOLD ' if 'holdout' in t['tags'] else ''}{t['goal'][:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
