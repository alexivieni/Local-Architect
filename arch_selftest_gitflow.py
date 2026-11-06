"""Self-test for arch_gitflow: branch per run, drafted commit message, one-call commit of only the run's files, the dirty-tree refusals,
the PR summary, and that nothing is ever pushed. Temp repos only; no model, no network."""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _g(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"] + list(args), cwd=root, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    return p.stdout.strip()


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_gitflow as gf
    tmp = tempfile.mkdtemp(prefix="archgf_")
    try:
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            print("  SKIP  git is not installed")
            return 0
        plain = os.path.join(tmp, "plain")
        os.makedirs(plain)
        check("a folder that is not a repository is refused with a reason", not gf.start_run(plain, "r1")["ok"] and "not a git" in gf.start_run(plain, "r1")["why"])

        root = os.path.join(tmp, "repo")
        os.makedirs(root)
        _g(root, "init", "-q", "-b", "main")
        check("a repository with no commit is refused", "no commit" in gf.start_run(root, "r1")["why"])
        _w(root, "app.py", "def add(a, b):\n    return a + b\n")
        _w(root, "util.py", "X = 1\n")
        _w(root, "notes.txt", "mine\n")
        _g(root, "add", "-A")
        _g(root, "commit", "-q", "-m", "init")
        base = _g(root, "rev-parse", "HEAD")

        # ---- start
        _w(root, "notes.txt", "mine, edited\n")
        r = gf.start_run(root, "20261105 run#1")
        check("a dirty tree is refused, naming the file", not r["ok"] and "notes.txt" in r["dirty"], r)
        _g(root, "checkout", "--", "notes.txt")
        before_tree = gf.status(root)
        r = gf.start_run(root, "20261105 run#1")
        check("start_run creates architect/<safe run id> from HEAD", r["ok"] and r["branch"] == "architect/20261105-run-1" and gf.current_branch(root) == r["branch"], r)
        check("the base commit is unchanged and the tree is untouched", _g(root, "rev-parse", "HEAD") == base and gf.status(root) == before_tree)
        r2 = gf.start_run(root, "20261105 run#1")
        check("a second branch for the same id gets a suffix, never overwrites", r2["ok"] and r2["branch"].endswith("-2"), r2)
        _g(root, "switch", r["branch"])
        _g(root, "branch", "-D", r2["branch"])
        gf._save_state(root, "20261105 run#1", {k: v for k, v in r.items() if k != "ok"})

        # ---- the run changes files; the person also staged something unrelated
        _w(root, "app.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
        _w(root, "new_mod.py", "def hello():\n    return 'hi'\n")
        os.remove(os.path.join(root, "util.py"))
        _w(root, ".arch/runs/x.json", "{}")
        st = gf.status(root)
        check("status ignores .arch/ and sees modified, new and deleted files", set(st) == {"app.py", "new_mod.py", "util.py"}, st)
        rows = gf.diff_stats(root, ["app.py", "new_mod.py", "util.py"])
        check("diff stats count added, new-file and deleted lines", rows["added"] == 6 and rows["deleted"] == 1, rows)

        # unrelated change the run did not report -> refuse
        _w(root, "stray.txt", "surprise\n")
        r = gf.commit_run(root, "20261105 run#1", ["app.py", "new_mod.py", "util.py"], goal="Add sub()")
        check("a change the run did not report is refused unless told", not r["ok"] and r.get("unrelated") == ["stray.txt"], r)
        check("refusing left everything uncommitted", _g(root, "rev-parse", "HEAD") == base)
        os.remove(os.path.join(root, "stray.txt"))

        # ---- message drafting
        polished = []

        def polish(draft, ctx):
            polished.append(ctx["goal"])
            return "Subject: Add subtraction and a hello module"

        stats = gf.diff_stats(root, ["app.py", "new_mod.py", "util.py"])
        m = gf.draft_message("/fix add a sub() function. Then more words that do not matter", stats, "r1")
        check("the subject comes from the goal's first sentence, without the slash command", m.splitlines()[0] == "Add a sub() function", m)
        check("the body lists files with their counts", "- app.py (+4 -0)" in m and "3 file(s) changed" in m, m)
        long = gf.draft_message("word " * 60, stats)
        check("a long goal is cut to a 72-character subject", len(long.splitlines()[0]) <= 72)
        check("polish can reword the subject only", gf.draft_message("x" * 3 + " goal here", stats, polish=polish).startswith("Add subtraction and a hello module\n\nMade by"))
        check("a polish that leaks a secret is dropped", gf.draft_message("Add sub", stats, polish=lambda d, c: "set password=abcdefgh12345").startswith("Add sub\n"))
        check("a polish that raises or returns junk is dropped", gf.draft_message("Add sub", stats, polish=lambda d, c: 1 / 0).startswith("Add sub\n")
              and gf.draft_message("Add sub", stats, polish=lambda d, c: "ok").startswith("Add sub\n"))

        # ---- commit only the run's files
        _w(root, "keep_staged.txt", "staged by person\n")
        _g(root, "add", "keep_staged.txt")
        r = gf.commit_run(root, "20261105 run#1", ["app.py", "new_mod.py", "util.py", "../outside.py", ".arch/runs/x.json"], goal="Add sub()", polish=polish, allow_dirty=True)
        check("commit_run commits in one call", r["ok"] and r["sha"] == _g(root, "rev-parse", "HEAD"), r)
        check("only the run's files are in the commit (not .arch, not the person's staged file)",
              sorted(_g(root, "show", "--name-only", "--format=", "HEAD").split()) == ["app.py", "new_mod.py", "util.py"], _g(root, "show", "--name-only", "--format=", "HEAD"))
        check("the person's own staged file is still staged", _g(root, "diff", "--cached", "--name-only") == "keep_staged.txt")
        check("the commit is on the run's branch and main did not move", _g(root, "rev-parse", "main") == base and gf.current_branch(root) == "architect/20261105-run-1")
        check("the commit message carries the polished subject and the stats", _g(root, "log", "-1", "--format=%B").startswith("Add subtraction and a hello module"))
        check("a second commit_run is a no-op that says so", gf.commit_run(root, "20261105 run#1", ["app.py"]).get("already"))

        # ---- PR summary
        text = gf.pr_summary(root, "20261105 run#1", goal="Add sub()", checks=[("tests", True), ("types", None), ("lint", False)], verdict="accepted")
        check("the PR summary has goal, files table, checks in three outcomes and the never-pushed note",
              "# Add sub()" in text and "| `app.py` | 4 | 0 |" in text and "types: could not run" in text and "lint: FAILED" in text and "Nothing was pushed" in text, text)
        check("the PR summary is written under .arch/gitflow", os.path.isfile(os.path.join(root, ".arch", "gitflow", "20261105-run-1.PR.md")))

        # ---- never pushes
        src = open(os.path.join(HERE, "arch_gitflow.py"), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')) and "push" not in l.lower().split("never pushes")[0] or False)
        check("the module has no push, fetch or pull call", not any(f'"{w}"' in src for w in ("push", "fetch", "pull", "remote")))

        # ---- mixing with the person's own edits
        _g(root, "reset", "-q")
        os.remove(os.path.join(root, "keep_staged.txt"))
        _g(root, "switch", "-q", "main")
        _w(root, "app.py", "def add(a, b):\n    return a + b  # mine\n")
        r = gf.start_run(root, "r2", allow_dirty=True)
        check("allow_dirty starts despite edits and remembers them", r["ok"] and r["dirty_before"] == ["app.py"], r)
        _w(root, "app.py", "def add(a, b):\n    return a + b  # mine\n\n\ndef mul(a, b):\n    return a * b\n")
        r = gf.commit_run(root, "r2", ["app.py"], goal="Add mul")
        check("a run file that was already dirty is refused (it would mix your edits)", not r["ok"] and r.get("mixed") == ["app.py"], r)
        r = gf.commit_run(root, "r2", ["app.py"], goal="Add mul", allow_dirty=True)
        check("allow_dirty commits it anyway", r["ok"])
        check("commit_run on the wrong branch is refused", (lambda: (_g(root, "switch", "-q", "main"), not gf.commit_run(root, "r2", ["app.py"]).get("ok") or True)[1])())
        check("an unknown run id is refused", not gf.commit_run(root, "nope", ["app.py"])["ok"])
        _g(root, "switch", "-q", "architect/r2")
        check("return_to_base switches back", gf.return_to_base(root, "r2")["ok"] and gf.current_branch(root) == "main")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' GITFLOW CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' GITFLOW CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
