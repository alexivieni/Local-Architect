"""Git checkpoints per attempt. Every attempt's changed files are committed to a throwaway ref refs/architect/<run>/<try>.

Safety: a separate index file (GIT_INDEX_FILE) and plumbing only (update-index, write-tree, commit-tree, update-ref), so the person's
branch, HEAD, index and working tree are never touched. If the project root is a git repo the ref lives in it (objects only); otherwise
a hidden shadow repo is created under .arch/ckpt/git. Never pushes, never runs hooks, never alters existing commits.
undo = write a checkpoint's files back to the working tree; compare = diff two attempts.
Setting: behavior.checkpoints ("on" | "off", default "off").
"""
import os
import re
import subprocess
import tempfile

_ID = {"GIT_AUTHOR_NAME": "architect", "GIT_AUTHOR_EMAIL": "architect@localhost",
       "GIT_COMMITTER_NAME": "architect", "GIT_COMMITTER_EMAIL": "architect@localhost"}


def enabled(cfg):
    v = (cfg.get("behavior") or {}).get("checkpoints", "off")
    return v is True or str(v).lower() in ("on", "true", "1", "yes")


def _run(args, env_extra, cwd, inp=None, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_ID)
    env.update(env_extra)
    p = subprocess.run(["git"] + args, cwd=cwd, env=env, input=inp, capture_output=True, timeout=60)
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace").strip()[:300] or f"git {args[0]} failed")
    return p


def _safe(name):
    return re.sub(r"[^\w.-]+", "_", str(name or "run")).strip("._") or "run"


def _repo(root):
    """-> {"git_dir", "shadow", "index"} for this project, creating the shadow repo when the root is not itself a git repo."""
    root = os.path.realpath(root)
    ck = os.path.join(root, ".arch", "ckpt")
    try:
        p = _run(["rev-parse", "--show-toplevel", "--absolute-git-dir"], {}, root, check=False)
        top, gd = ((p.stdout or b"").decode("utf-8", "replace").splitlines() + ["", ""])[:2]
        if p.returncode == 0 and top and os.path.realpath(top) == root and gd:
            return {"git_dir": gd, "shadow": False, "index": os.path.join(ck, "index")}
    except (OSError, subprocess.SubprocessError):
        raise
    gd = os.path.join(ck, "git")
    if not os.path.isdir(gd):
        os.makedirs(ck, exist_ok=True)
        _run(["init", "--quiet", "--bare", gd], {}, root)
    return {"git_dir": gd, "shadow": True, "index": os.path.join(ck, "index")}


def _env(repo, root):
    return {"GIT_DIR": repo["git_dir"], "GIT_WORK_TREE": os.path.realpath(root), "GIT_INDEX_FILE": repo["index"]}


def _ref(run_id, n):
    return f"refs/architect/{_safe(run_id)}/{n}"


def _norm(rel):
    return str(rel).replace("\\", "/").lstrip("/")


def snapshot(root, run_id, n, files, message=""):
    """Commit `files` (relative paths, as they are on disk now) on top of the previous checkpoint of this run (or HEAD).
    -> {"ok", "ref", "commit", "files"} or {"ok": False, "why"}."""
    try:
        root = os.path.realpath(root)
        repo = _repo(root)
        env = _env(repo, root)
        os.makedirs(os.path.dirname(repo["index"]), exist_ok=True)
        if os.path.exists(repo["index"]):
            os.remove(repo["index"])
        parent = None
        for cand in [_ref(run_id, int(n) - 1) if int(n) > 1 else None] + [x["ref"] for x in reversed(list_checkpoints(root, run_id))]:
            if cand and _run(["rev-parse", "--verify", "-q", cand], env, root, check=False).returncode == 0:
                parent = cand
                break
        if parent is None and not repo["shadow"]:
            if _run(["rev-parse", "--verify", "-q", "HEAD"], env, root, check=False).returncode == 0:
                parent = "HEAD"
        if parent:
            _run(["read-tree", parent], env, root)
        done = []
        for rel in sorted({_norm(f) for f in files or ()}):
            if not rel or rel.startswith(".arch/") or ".." in rel.split("/"):
                continue
            full = os.path.join(root, rel)
            if os.path.isfile(full):
                _run(["update-index", "--add", "--", rel], env, root)
            else:
                _run(["update-index", "--force-remove", "--", rel], env, root)
            done.append(rel)
        tree = _run(["write-tree"], env, root).stdout.decode().strip()
        args = ["commit-tree", tree, "-m", message or f"architect checkpoint {run_id} try {n}"]
        if parent:
            args += ["-p", _run(["rev-parse", parent], env, root).stdout.decode().strip()]
        commit = _run(args, env, root).stdout.decode().strip()
        ref = _ref(run_id, n)
        _run(["update-ref", ref, commit], env, root)
        return {"ok": True, "ref": ref, "commit": commit, "files": done, "shadow": repo["shadow"]}
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as ex:
        return {"ok": False, "why": f"{type(ex).__name__}: {ex}"[:300]}
    finally:
        try:
            if os.path.exists(repo["index"]):
                os.remove(repo["index"])
        except (OSError, UnboundLocalError, NameError):
            pass


def list_checkpoints(root, run_id):
    """-> [{"try", "ref", "commit"}] in try order."""
    try:
        root = os.path.realpath(root)
        repo = _repo(root)
        env = _env(repo, root)
        p = _run(["for-each-ref", "--format=%(refname) %(objectname)", f"refs/architect/{_safe(run_id)}/"], env, root, check=False)
        out = []
        for ln in p.stdout.decode().splitlines():
            ref, _, sha = ln.partition(" ")
            if ref.rsplit("/", 1)[-1].isdigit():
                out.append({"try": int(ref.rsplit("/", 1)[-1]), "ref": ref, "commit": sha})
        return sorted(out, key=lambda x: x["try"])
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []


def restore(root, run_id, n, files=None):
    """Undo: write the files of checkpoint `n` back into the working tree (only files in that checkpoint; `files` narrows it).
    Nothing else is touched and no git state changes. -> {"ok", "restored": [...]}."""
    try:
        root = os.path.realpath(root)
        repo = _repo(root)
        env = _env(repo, root)
        ref = _ref(run_id, n)
        if _run(["rev-parse", "--verify", "-q", ref], env, root, check=False).returncode != 0:
            return {"ok": False, "why": f"no checkpoint {n} for run {run_id}"}
        parent = _run(["rev-parse", "--verify", "-q", ref + "^"], env, root, check=False)
        names = _run(["ls-tree", "-r", "--name-only", ref], env, root).stdout.decode("utf-8", "replace").splitlines()
        # for a git repo the checkpoint holds the whole HEAD tree plus changes: restore only what this run's checkpoints touched
        touched = _touched(root, run_id, env)
        want = {_norm(f) for f in files} if files else None
        out = []
        for rel in names:
            if (want is not None and rel not in want) or (want is None and rel not in touched):
                continue
            data = _run(["cat-file", "blob", f"{ref}:{rel}"], env, root).stdout
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
            out.append(rel)
        return {"ok": True, "restored": out}
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as ex:
        return {"ok": False, "why": f"{type(ex).__name__}: {ex}"[:300]}


def _touched(root, run_id, env):
    """Files any checkpoint of this run changed relative to its parent (the first one: relative to the base)."""
    seen = set()
    for c in list_checkpoints(root, run_id):
        p = _run(["diff-tree", "-r", "--no-commit-id", "--name-only", "--root", c["ref"]] if _run(
            ["rev-parse", "--verify", "-q", c["ref"] + "^"], env, root, check=False).returncode != 0 else
            ["diff-tree", "-r", "--no-commit-id", "--name-only", c["ref"] + "^", c["ref"]], env, root, check=False)
        seen |= {x for x in p.stdout.decode("utf-8", "replace").splitlines() if x}
    return seen


def compare(root, run_id, a, b):
    """Diff two attempts of a run (unified text). Read-only."""
    try:
        root = os.path.realpath(root)
        repo = _repo(root)
        env = _env(repo, root)
        p = _run(["diff", "--no-color", "--no-ext-diff", _ref(run_id, a), _ref(run_id, b)], env, root)
        return {"ok": True, "diff": p.stdout.decode("utf-8", "replace")}
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as ex:
        return {"ok": False, "why": f"{type(ex).__name__}: {ex}"[:300]}


def attempt(e, n, files=None):
    """Flow hook: checkpoint this attempt's changed files when behavior.checkpoints is on. Never raises, never blocks a run."""
    try:
        if not enabled(e.cfg):
            return None
        r = snapshot(e.root, getattr(e, "run_id", "run"), n, files if files is not None else getattr(e, "changed_files", []))
        import time
        e._trace({"type": "checkpoint", "t": time.time(), "try": n, **{k: v for k, v in r.items() if k in ("ok", "ref", "commit", "files", "why")}})
        return r
    except Exception:
        return None
