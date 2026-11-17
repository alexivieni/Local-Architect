"""Self-test: a file's line-ending style cannot change by accident (tools/eol_audit.py).

`.gitattributes` is `* -text`, so git never converts; a tool that rewrites a file in text mode on Windows flips CRLF and LF, and the whole
file shows as changed. The audit reads the bytes of the working tree against a base commit. It is proven here on a throwaway git repository:
a flip either way is caught, a new line written in the other style is caught, a new CRLF file is caught, and a clean edit, a new LF file, a
CRLF .bat file, somebody else's documents and a deleted file are not. No git, or a base that is not there, is "could not check" (exit 2),
never a pass.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import eol_audit as ea

    print("\n== line endings are not changed by accident ==")
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        print("  git is not installed: this suite could not run")
        return 2

    check("the style of bytes is read exactly (crlf, lf, mixed, none)",
          [ea.style(x) for x in (b"a\r\nb\r\n", b"a\nb\n", b"a\r\nb\n", b"abc")] == ["crlf", "lf", "mixed", "none"])

    d = tempfile.mkdtemp(prefix="eol_")

    def git(*a):
        r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *a], cwd=d, capture_output=True)
        return r

    def put(rel, data):
        p = os.path.join(d, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    try:
        git("init", "-q")
        put("lf_edit.py", b"a = 1\nb = 2\n")
        put("crlf_to_lf.py", b"a = 1\r\nb = 2\r\n")
        put("lf_to_crlf.py", b"a = 1\nb = 2\n")
        put("crlf_mixed.py", b"a = 1\r\nb = 2\r\n")
        put("was_mixed.py", b"a = 1\r\nb = 2\n")
        put("gone.py", b"a = 1\r\n")
        put("data/blob.bin", b"\r\n\r\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        put("lf_edit.py", b"a = 1\nb = 3\nc = 4\n")
        put("crlf_to_lf.py", b"a = 1\nb = 2\n")
        put("lf_to_crlf.py", b"a = 1\r\nb = 2\r\n")
        put("crlf_mixed.py", b"a = 1\r\nb = 2\r\nc = 3\n")
        put("was_mixed.py", b"a = 1\nb = 2\n")
        os.remove(os.path.join(d, "gone.py"))
        put("new_lf.py", b"x = 1\n")
        put("new_crlf.py", b"x = 1\r\n")
        put("tools/run.bat", b"@echo off\r\n")
        put("docs/research/paper.md", b"a\r\nb\r\n")
        put("data/blob.bin", b"\n\n")
        probs, status = ea.audit(d)
        bad = {p: why for p, why in probs}
        check("the audit runs on a fresh repository", status == "ok", status)
        check("a clean LF edit is fine", "lf_edit.py" not in bad, bad)
        check("CRLF turned into LF is caught", "crlf_to_lf.py" in bad and "crlf to lf" in bad["crlf_to_lf.py"], bad)
        check("LF turned into CRLF is caught", "lf_to_crlf.py" in bad, bad)
        check("a new LF line written into a CRLF file (mixed) is caught", "crlf_mixed.py" in bad, bad)
        check("a file that was mixed may become pure", "was_mixed.py" not in bad, bad)
        check("a new LF file is fine, a new CRLF source file is caught, a new CRLF .bat file is fine",
              "new_lf.py" not in bad and "new_crlf.py" in bad and "tools/run.bat" not in bad, bad)
        check("somebody else's documents under docs/research are left alone", "docs/research/paper.md" not in bad, bad)
        check("a deleted file and a non-text file are left alone", "gone.py" not in bad and "data/blob.bin" not in bad, bad)
        check("exactly the four real problems are reported", sorted(bad) == ["crlf_mixed.py", "crlf_to_lf.py", "lf_to_crlf.py", "new_crlf.py"], sorted(bad))

        git("add", "-A")
        git("commit", "-q", "-m", "second")
        probs2, status2 = ea.audit(d, base="HEAD~1")
        check("--base compares what a commit changed (what CI runs): the same four, from the commit",
              status2 == "ok" and sorted(p for p, _ in probs2) == ["crlf_mixed.py", "crlf_to_lf.py", "lf_to_crlf.py", "new_crlf.py"], (status2, probs2))
        probs3, status3 = ea.audit(d, base="HEAD")
        check("a clean tree has no problems", status3 == "ok" and not probs3, probs3)
        _p, status4 = ea.audit(d, base="no-such-ref")
        check("a base that is not in the checkout is 'could not check', not a pass", status4 != "ok" and "not in this checkout" in status4, status4)
        plain = tempfile.mkdtemp(prefix="eol_plain_")
        try:
            _p, status5 = ea.audit(plain)
            check("a folder that is not a git checkout is 'could not check', not a pass", status5 != "ok", status5)
        finally:
            shutil.rmtree(plain, ignore_errors=True)
    finally:
        def _ro(fn, path, _exc):
            os.chmod(path, 0o700)
            fn(path)
        shutil.rmtree(d, onerror=_ro)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' LINE-ENDING CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' LINE-ENDING CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
