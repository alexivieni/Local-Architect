"""Self-test for arch_replybudget: learned reply caps never cut a reply they should not.

    python arch_selftest_replybudget.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    home = tempfile.mkdtemp(prefix="replybudget_")
    saved = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST", "ARCHITECT_EVAL")}
    os.environ["ARCHITECT_DATA_DIR"] = home
    os.environ.pop("ARCHITECT_SELFTEST", None)
    os.environ.pop("ARCHITECT_EVAL", None)
    try:
        import arch_replybudget as R
        R._MEMO.update(data=None, path=None)
        check("tries, candidates and samples of one step share a key; structured and text do not",
              R.key("generator", "sprint2-try3", False) == R.key("generator", "sprint1-try1", False) == "generator|sprint|text"
              and R.key("planner", "plan", True).endswith("|json"))
        k = R.key("generator", "fix-try1", False)
        for n in range(29):
            R.record(k, 300 + n, False)
        check("no learned cap before 30 replies", R.cap(k, 3072) is None)
        R.record(k, 400, False)
        c = R.cap(k, 3072)
        check("after 30: 2 x p95 + 128, but never below the 1024 floor for code", c == 1024, c)
        for n in range(30):
            R.record(k, 900, False)
        check("a longer step earns a higher cap (2 x 900 + 128 = 1928)", R.cap(k, 3072) == 1928, R.cap(k, 3072))
        check("never above the fixed limit", R.cap(k, 1500) is None)
        R.record(k, 1928, True)
        check("one recent reply that hit its limit switches the learned cap off", R.cap(k, 3072) is None)
        R._MEMO.update(data=None, path=None)
        check("it survives a restart (kept on disk)", R._load().get(k) and len(R._load()[k]["n"]) == 61)
        os.environ["ARCHITECT_EVAL"] = "1"
        check("off in benchmarks", R.off({}))
        os.environ.pop("ARCHITECT_EVAL")
        check("off with behavior.reply_budget = off; on by default", R.off({"behavior": {"reply_budget": "off"}}) and not R.off({}))
        src = open(os.path.join(os.path.dirname(R.__file__), "arch_core.py"), encoding="utf-8").read()
        check("the engine applies it only with no explicit limit, and records every local reply after the call",
              "if not npred:" in src and "_rb.cap(_rb_key, options[\"num_predict\"])" in src and "_rb.record(_rb_key" in src)
    finally:
        for k2, v in saved.items():
            if v is None:
                os.environ.pop(k2, None)
            else:
                os.environ[k2] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' REPLY-BUDGET CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' REPLY-BUDGET CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
