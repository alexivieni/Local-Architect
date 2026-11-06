"""Self-test for onboarding (arch_onboard) and memory consolidation (arch_memory.consolidate). Throwaway project and data folders;
no model, no network."""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROJECT = {
    "shop/__init__.py": "",
    "shop/cart.py": "from typing import List\n\n\ndef total(prices: List[float]) -> float:\n    \"\"\"Sum of prices.\"\"\"\n    return sum(prices)\n\n\nclass Cart:\n    def __init__(self) -> None:\n        self.items = []\n\n    def add(self, p: float) -> None:\n        self.items.append(p)\n",
    "shop/__main__.py": "import argparse\nfrom shop.cart import total\n\n\ndef main():\n    ap = argparse.ArgumentParser()\n    sub = ap.add_subparsers()\n    sub.add_parser('total')\n    sub.add_parser('clear')\n    ap.parse_args()\n\n\nif __name__ == '__main__':\n    main()\n",
    "tests/test_cart.py": "from shop.cart import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    "legacy.py": "def old():\r\n    return 1\r\n",
    "README.md": "# shop\n",
}


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    tmp = tempfile.mkdtemp(prefix="archonb_")
    saved = {k: os.environ.get(k) for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")}
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
    try:
        import arch_memory as mem
        import arch_onboard as ob

        root = os.path.join(tmp, "proj")
        for rel, text in PROJECT.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(text.encode("utf-8"))

        # ---- onboarding
        r = ob.onboard(root)
        check("onboard succeeds and finds Python", r["ok"] and r["language"] == "Python", r.get("why"))
        check("test command found, with the interpreter path replaced by `python`", r["test_command"] == "python -m pytest -q", r["test_command"])
        check("run command comes from the package's __main__", r["run_command"] == "python -m shop", r["run_command"])
        check("package layout and test dir detected", r["layout"]["packages"] == ["shop"] and r["layout"]["test_dirs"] == ["tests"], r["layout"])
        check("entry points include the package __main__", "shop/__main__.py" in r["entry_points"], r["entry_points"])
        check("CLI commands come from the contract derivation", r["cli_commands"] == ["clear", "total"], r["cli_commands"])
        check("public API lists the cart module's names", any(m == "shop.cart" and "Cart" in n and "total" in n for m, n in r["public_api"]), r["public_api"])
        check("conventions are measured (type hints, 4 spaces)", any("Type hints" in c for c in r["conventions"]) and any("4 spaces" in c for c in r["conventions"]), r["conventions"])
        check("gotchas name the CRLF file and the missing git repo", any("legacy.py" in g for g in r["gotchas"]) and any("not a git repository" in g for g in r["gotchas"]), r["gotchas"])
        d = ob.draft_path(root)
        check("the draft is written to .arch/memory/proposed_ARCHITECT.md", os.path.isfile(d) and r["changed"])
        text = open(d, encoding="utf-8").read()
        check("the draft has the commands, conventions and gotchas sections",
              all(h in text for h in ("## Commands", "## Conventions", "## Gotchas", "## Learned")) and "python -m shop" in text, text[:300])
        check("ARCHITECT.md itself was not created", not os.path.exists(os.path.join(root, "ARCHITECT.md")))
        check("the FTS index was built inside the project", r["index"] and r["index"]["ok"] and os.path.isfile(os.path.join(root, ".arch", "index.db")), r["index"])
        check("the profile for the prompts was written", os.path.isfile(os.path.join(root, ".arch", "profile.md")))

        r2 = ob.onboard(root)
        check("idempotent: a second run changes nothing", not r2["changed"] and open(d, encoding="utf-8").read() == text)
        check("idempotent: the second index run reuses every chunk", r2["index"]["ok"] and r2["index"]["new"] == 0, r2["index"])

        with open(os.path.join(root, "shop", "extra.py"), "w", encoding="utf-8") as f:
            f.write("def extra():\n    return 1\n")
        r3 = ob.onboard(root)
        check("a changed project updates the draft", r3["changed"] and "shop.extra" in open(d, encoding="utf-8").read())

        a = ob.accept_draft(root)
        check("accept_draft copies the draft to ARCHITECT.md", a["ok"] and open(os.path.join(root, "ARCHITECT.md"), encoding="utf-8").read() == open(d, encoding="utf-8").read())
        with open(os.path.join(root, "ARCHITECT.md"), "w", encoding="utf-8") as f:
            f.write("# mine\n")
        check("accept_draft never overwrites the person's file", not ob.accept_draft(root)["ok"] and open(os.path.join(root, "ARCHITECT.md"), encoding="utf-8").read() == "# mine\n")
        r4 = ob.onboard(root)
        check("onboard with an ARCHITECT.md leaves it alone and notes it", r4["existing_architect_md"] and open(os.path.join(root, "ARCHITECT.md"), encoding="utf-8").read() == "# mine\n")
        check("onboard on a missing folder says so", not ob.onboard(os.path.join(tmp, "nope"))["ok"])
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        re_ = ob.onboard(empty, build_index=False)
        check("an empty folder still gets a draft that says no test command", re_["ok"] and "none found" in re_["draft"] and re_["test_command"] is None)

        # ---- consolidation
        def seed(name, rows):
            p = os.path.join(root, ".arch", "memory", name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write("# Lessons\n\n" + "".join(f"- [2026-11-01 r{i}] {t} (used {u}, confirmed {c})\n" for i, (t, u, c) in enumerate(rows)))
            return p

        os.remove(os.path.join(root, "ARCHITECT.md"))
        p = seed("lessons.md", [
            ("run the tests from the project root with python -m pytest", 3, 2),
            ("Run the tests from the project root, with python -m pytest.", 2, 1),
            ("never edit generated files under build/", 1, 0),
            ("old guess that never helped anyone here", 6, 0),
            ("fresh unused note about the cart module", 0, 0),
        ])
        dry = mem.consolidate(root, dry_run=True)
        check("dry_run reports counts and writes nothing", dry["merged"] == 1 and dry["dropped"] == 1 and "run the tests" in open(p, encoding="utf-8").read()
              and open(p, encoding="utf-8").read().count("- [") == 5 and not mem.proposed(root), dry)
        rep = mem.consolidate(root)
        rows = mem.lines(root, "lessons.md")
        check("duplicates merge into the better-earned line, counts summed", rep["merged"] == 1 and any(l["used"] == 5 and l["confirmed"] == 3 for l in rows), rows)
        check("a never-confirmed line used 5+ times is dropped; an unused one stays",
              rep["dropped"] == 1 and not any("never helped" in l["text"] for l in rows) and any("fresh unused" in l["text"] for l in rows), rows)
        check("the report counts files before and after", rep["files"]["lessons.md"] == {"before": 5, "after": 3}, rep["files"])
        check("a well-confirmed lesson is proposed, not applied", rep["proposed"] == 1 and len(mem.proposed(root)) == 1 and not os.path.exists(os.path.join(root, "ARCHITECT.md")), rep)
        again = mem.consolidate(root)
        check("idempotent: a second pass merges, drops and proposes nothing", (again["merged"], again["dropped"], again["proposed"]) == (0, 0, 0)
              and len(mem.proposed(root)) == 1 and len(mem.lines(root, "lessons.md")) == 3, again)
        with open(os.path.join(root, "ARCHITECT.md"), "w", encoding="utf-8") as f:
            f.write("# mine\n- never edit generated files under build/\n")
        seed("gotchas.md", [("never edit generated files under build/", 4, 3)])
        check("a lesson ARCHITECT.md already says is not proposed", mem.consolidate(root)["proposed"] == 0)
        check("ARCHITECT.md is untouched by consolidation", open(os.path.join(root, "ARCHITECT.md"), encoding="utf-8").read() == "# mine\n- never edit generated files under build/\n")
        check("the lock file is gone after a run", not os.path.exists(os.path.join(root, ".arch", "memory", ".consolidate.lock")))
        lock = os.path.join(root, ".arch", "memory", ".consolidate.lock")
        open(lock, "w").close()
        busy = mem.consolidate(root)
        check("a fresh lock means another run is going: skipped, nothing changed", not busy["ok"] and "another" in busy["skipped"])
        os.utime(lock, (1, 1))
        check("a stale lock is taken over", mem.consolidate(root)["ok"])
        check("no project memory -> a clean no-op", mem.consolidate(tmp)["skipped"] == "no project memory yet")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' ONBOARD/CONSOLIDATE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' ONBOARD/CONSOLIDATE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
