"""Git flow for the PERSON'S project: one branch per run, one commit after Accept, a PR-style summary. Never pushes.

    start_run(root, run_id)                    branch architect/<run> from the current HEAD (the working tree is not touched)
    commit_run(root, run_id, files, goal)      one commit of exactly the files the run changed
    pr_summary(root, run_id, goal=...)         a Markdown summary you can paste into a pull request
    return_to_base(root, run_id)               switch back to the branch the run started from

Safety rules (each one tested in arch_selftest_gitflow):
  * no function here talks to a remote: there is no push, fetch or pull anywhere in this module;
  * a dirty working tree is refused at start_run unless `allow_dirty`; the files that were dirty before are remembered, and a commit
    refuses to mix them with the run's files;
  * only the files the run changed are staged and committed (`git commit --only`), anything else the person staged stays staged;
  * the commit message is drafted by code from the goal and the diff stats; a local model may polish it through an injected
    `polish(draft, context) -> str`, and its answer is checked in code (one short subject, no secret-looking text) or dropped;
  * the person's own hooks and signing settings apply; a failing hook is reported, not bypassed;
  * Architect's own folder `.arch/` is never committed and never counts as dirty.
State lives in .arch/gitflow/<run>.json. Python standard library only.
"""
import json
import os
import re
import subprocess

MAX_SUBJECT = 72
MAX_BODY_FILES = 20
_SECRET = re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|private[_-]?key)\b\s*[:=]\s*\S{8,}")


def _safe(name):
    return re.sub(r"[^\w.-]+", "-", str(name or "run")).strip(".-_") or "run"


def _git(root, args, check=False, identity=False, inp=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = ["git"]
    if identity and not _has_identity(root):
        cmd += ["-c", "user.name=architect", "-c", "user.email=architect@localhost"]
    p = subprocess.run(cmd + args, cwd=root, env=env, capture_output=True, input=inp, timeout=120)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise RuntimeError((err or out).strip()[:400] or f"git {args[0]} failed")
    return p.returncode, out, err


def _has_identity(root):
    rc1, n, _ = _git(root, ["config", "user.name"])
    rc2, e, _ = _git(root, ["config", "user.email"])
    return rc1 == 0 and rc2 == 0 and n.strip() != "" and e.strip() != ""


def available(root):
    """-> (ok, why). A git repository with at least one commit."""
    try:
        _git(root, ["--version"])
    except (OSError, subprocess.SubprocessError):
        return False, "git is not installed"
    rc, out, _ = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.strip() != "true":
        return False, "this folder is not a git repository"
    rc, _, _ = _git(root, ["rev-parse", "--verify", "-q", "HEAD"])
    if rc != 0:
        return False, "the repository has no commit yet; make a first commit so there is something to branch from"
    return True, ""


def _prefix(root):
    """Path of root relative to the repo top ('' at the top, else 'sub/')."""
    rc, out, _ = _git(root, ["rev-parse", "--show-prefix"])
    return out.strip() if rc == 0 else ""


def status(root):
    """-> {path (relative to root, '/'-separated): code} for every changed or untracked path, `.arch/` excluded."""
    rc, out, _ = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."])
    res, parts, i = {}, out.split("\0"), 0
    pre = _prefix(root)
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        code, path = e[:2], e[3:]
        if code[0] in "RC":
            i += 1                                    # the old name follows
        if pre and path.startswith(pre):
            path = path[len(pre):]
        if path == ".arch" or path.startswith(".arch/"):
            continue
        res[path] = "?" if code == "??" else (code.strip() or "M")
    return res


def _state_path(root, run_id):
    return os.path.join(root, ".arch", "gitflow", _safe(run_id) + ".json")


def load_state(root, run_id):
    try:
        with open(_state_path(root, run_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_state(root, run_id, st):
    p = _state_path(root, run_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, indent=1)


def current_branch(root):
    rc, out, _ = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if rc == 0 else ""


def _head(root):
    rc, out, _ = _git(root, ["rev-parse", "HEAD"])
    return out.strip() if rc == 0 else ""


def _branch_exists(root, name):
    return _git(root, ["show-ref", "--verify", "-q", "refs/heads/" + name])[0] == 0


def start_run(root, run_id, allow_dirty=False):
    """Create architect/<run> from the current HEAD and switch to it. The working tree and index are untouched (a switch to a new
    branch at the same commit changes nothing on disk). -> {"ok", "branch", "base_branch", "base_sha", "dirty_before"} or {"ok": False, "why"}."""
    root = os.path.abspath(root)
    ok, why = available(root)
    if not ok:
        return {"ok": False, "why": why}
    dirty = sorted(status(root))
    if dirty and not allow_dirty:
        return {"ok": False, "dirty": dirty, "why": f"the working tree has {len(dirty)} uncommitted change(s) that are not part of this run "
                f"({', '.join(dirty[:5])}{'...' if len(dirty) > 5 else ''}); commit or stash them, or start with allow_dirty"}
    base_branch, base_sha = current_branch(root), _head(root)
    name = "architect/" + _safe(run_id)
    if _git(root, ["check-ref-format", "--branch", name])[0] != 0:
        return {"ok": False, "why": f"'{name}' is not a valid branch name"}
    n, cand = 2, name
    while _branch_exists(root, cand):
        cand, n = f"{name}-{n}", n + 1
    rc, _, err = _git(root, ["switch", "-c", cand])
    if rc != 0:
        return {"ok": False, "why": "could not create the branch: " + err.strip()[:300]}
    st = {"run": str(run_id), "branch": cand, "base_branch": base_branch, "base_sha": base_sha, "dirty_before": dirty, "commit": None}
    _save_state(root, run_id, st)
    return dict(st, ok=True)


def _clean_files(root, files):
    out = []
    for f in files or []:
        rel = str(f).replace("\\", "/").strip()
        if os.path.isabs(rel):
            try:
                rel = os.path.relpath(rel, root).replace(os.sep, "/")
            except ValueError:
                continue
        rel = os.path.normpath(rel).replace(os.sep, "/")
        if rel.startswith("../") or rel in ("..", ".", ".arch") or rel.startswith(".arch/") or rel.startswith(".git/"):
            continue
        if rel not in out:
            out.append(rel)
    return out


def _count_lines(path):
    try:
        with open(path, "rb") as f:
            data = f.read(2_000_000)
    except OSError:
        return 0
    if b"\0" in data:
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def diff_stats(root, files, against="HEAD"):
    """-> {"files": [{"path","added","deleted","status"}], "added", "deleted"} for the working tree vs `against` (untracked files count as added)."""
    st = status(root)
    rows, seen = [], set()
    if files:
        rc, out, _ = _git(root, ["diff", "--numstat", against, "--"] + list(files))
        for ln in out.splitlines():
            a, d, path = (ln.split("\t") + ["", "", ""])[:3]
            seen.add(path)
            rows.append({"path": path, "added": int(a) if a.isdigit() else 0, "deleted": int(d) if d.isdigit() else 0, "status": st.get(path, "M")})
    for f in files or []:
        if f not in seen and st.get(f) == "?":
            rows.append({"path": f, "added": _count_lines(os.path.join(root, f)), "deleted": 0, "status": "A"})
    rows.sort(key=lambda r: r["path"])
    return {"files": rows, "added": sum(r["added"] for r in rows), "deleted": sum(r["deleted"] for r in rows)}


def _subject_from(goal):
    s = " ".join(str(goal or "").split()).strip()
    s = re.sub(r"^/\w+\s+", "", s)                           # a saved command prefix such as "/fix"
    s = s.split(". ")[0].rstrip(".;: ") or "Update project"
    s = s[0].upper() + s[1:]
    if len(s) > MAX_SUBJECT:
        cut = s[:MAX_SUBJECT].rsplit(" ", 1)[0]
        s = cut if len(cut) >= 20 else s[:MAX_SUBJECT]
    return s


def draft_message(goal, stats, run_id="", polish=None):
    """Code drafts the message from the goal and the diff stats. `polish(draft, context) -> str` (a local model, injected) may reword
    the subject; its answer is kept only if it is one short line with nothing secret-looking, else the draft stands. -> str."""
    subject = _subject_from(goal)
    rows = stats.get("files", [])
    body = [f"Run {run_id} by Architect." if run_id else "Made by Architect.", "",
            f"{len(rows)} file(s) changed, +{stats.get('added', 0)} -{stats.get('deleted', 0)}:"]
    body += [f"- {r['path']} (+{r['added']} -{r['deleted']})" for r in rows[:MAX_BODY_FILES]]
    if len(rows) > MAX_BODY_FILES:
        body.append(f"- and {len(rows) - MAX_BODY_FILES} more")
    if polish:
        try:
            got = polish(subject + "\n\n" + "\n".join(body) + "\n", {"goal": goal, "stats": stats, "run_id": run_id})
        except Exception:
            got = None
        if isinstance(got, str) and got.strip():
            first = got.strip().splitlines()[0].strip()
            first = re.sub(r"^(subject|commit message)\s*:\s*", "", first, flags=re.I).strip("`\"' ")
            if 8 <= len(first) <= MAX_SUBJECT and not _SECRET.search(got) and "\0" not in got:
                subject = first                                       # only the subject is taken from the model
    return subject + "\n\n" + "\n".join(body) + "\n"


def commit_run(root, run_id, files, goal="", polish=None, allow_dirty=False, message=None):
    """Call once, after the person pressed Accept. Commits exactly `files` on the run's branch.
    -> {"ok", "sha", "branch", "files", "message", "stats"} or {"ok": False, "why"}. Nothing else is staged, nothing is pushed."""
    root = os.path.abspath(root)
    st = load_state(root, run_id)
    if not st:
        return {"ok": False, "why": "this run has no branch; call start_run first"}
    if st.get("commit"):
        return {"ok": True, "already": True, "sha": st["commit"], "branch": st["branch"], "files": st.get("files", [])}
    if current_branch(root) != st["branch"]:
        return {"ok": False, "why": f"you are on '{current_branch(root)}', not on the run's branch '{st['branch']}'; switch back or restart the run"}
    want = _clean_files(root, files)
    now = status(root)
    changed = [f for f in want if f in now]
    if not changed:
        return {"ok": False, "why": "none of the run's files has changes to commit"}
    before = set(st.get("dirty_before", []))
    mixed = [f for f in changed if f in before]
    if mixed and not allow_dirty:
        return {"ok": False, "mixed": mixed, "why": f"{', '.join(mixed[:5])} had your own uncommitted edits before the run; committing would mix them with the run's"}
    extra = sorted(p for p in now if p not in want and p not in before)
    if extra and not allow_dirty:
        return {"ok": False, "unrelated": extra, "why": f"the tree has changes the run did not report ({', '.join(extra[:5])}); "
                "pass them as the run's files or commit with allow_dirty"}
    stats = diff_stats(root, changed)
    msg = message or draft_message(goal, stats, run_id, polish)
    try:
        _git(root, ["add", "-A", "--"] + changed, check=True)
        rc, out, err = _git(root, ["commit", "--only", "-q", "-F", "-", "--"] + changed, identity=True, inp=msg.encode("utf-8"))
    except RuntimeError as e:
        return {"ok": False, "why": str(e)}
    if rc != 0:
        return {"ok": False, "why": "git commit failed (a hook of yours may have refused it): " + (err or out).strip()[:400]}
    sha = _head(root)
    st.update(commit=sha, files=changed, goal=str(goal)[:300])
    _save_state(root, run_id, st)
    return {"ok": True, "sha": sha, "branch": st["branch"], "files": changed, "message": msg, "stats": stats}


def pr_summary(root, run_id, goal="", checks=None, verdict="", notes="", write=True):
    """-> Markdown for a pull request. `checks` = [(name, True/False/None)]; None renders as 'could not run'. Written to
    .arch/gitflow/<run>.PR.md. Reads the run's commit when there is one, else the working tree."""
    root = os.path.abspath(root)
    st = load_state(root, run_id) or {}
    goal = goal or st.get("goal", "")
    if st.get("commit") and st.get("base_sha"):
        rc, out, _ = _git(root, ["diff", "--numstat", st["base_sha"], st["commit"]])
        rows = []
        for ln in out.splitlines():
            a, d, path = (ln.split("\t") + ["", "", ""])[:3]
            if path.startswith(".arch/"):
                continue
            rows.append({"path": path, "added": int(a) if a.isdigit() else 0, "deleted": int(d) if d.isdigit() else 0})
        stats = {"files": rows, "added": sum(r["added"] for r in rows), "deleted": sum(r["deleted"] for r in rows)}
    else:
        stats = diff_stats(root, sorted(status(root)))
    L = [f"# {_subject_from(goal)}", "", f"Branch `{st.get('branch', '?')}` from `{st.get('base_branch') or 'HEAD'}`"
         + (f" ({st['base_sha'][:8]})" if st.get("base_sha") else "") + f", run `{run_id}`."]
    if st.get("commit"):
        L.append(f"Commit `{st['commit'][:10]}`.")
    L += ["", "## What was asked", "", " ".join(str(goal).split()) or "(no goal recorded)", "", "## What changed", "",
          f"{len(stats['files'])} file(s), +{stats['added']} -{stats['deleted']}.", ""]
    if stats["files"]:
        L += ["| File | Added | Deleted |", "| --- | ---: | ---: |"] + [f"| `{r['path']}` | {r['added']} | {r['deleted']} |" for r in stats["files"][:60]]
        L.append("")
    if checks:
        word = {True: "passed", False: "FAILED", None: "could not run"}
        L += ["## Checks", ""] + [f"- {n}: {word.get(v, str(v))}" for n, v in checks] + [""]
    if verdict:
        L += ["## Verdict", "", str(verdict), ""]
    if notes:
        L += ["## Notes", "", str(notes), ""]
    L += ["_Made by Architect on a local model. Nothing was pushed; review the diff before merging._", ""]
    text = "\n".join(L)
    if write:
        p = os.path.join(root, ".arch", "gitflow", _safe(run_id) + ".PR.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return text


def return_to_base(root, run_id):
    """Switch back to the branch the run started from (git refuses if that would overwrite uncommitted work). -> {"ok", "branch"}."""
    root = os.path.abspath(root)
    st = load_state(root, run_id)
    if not st or not st.get("base_branch") or st["base_branch"] == "HEAD":
        return {"ok": False, "why": "the run did not start from a named branch"}
    rc, _, err = _git(root, ["switch", st["base_branch"]])
    return {"ok": True, "branch": st["base_branch"]} if rc == 0 else {"ok": False, "why": err.strip()[:300]}
