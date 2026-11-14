"""The manifest check for a fresh git checkout (GitHub Actions).

    python tools/ci_manifest_check.py

`architect.py self-verify` compares the folder with MANIFEST.sha256, which is written from the owner's working folder and so also
lists files git does not track (their own notes and papers). A clean clone cannot have those, so here a file counts against the
manifest only when it is there and differs, or when git tracks it and it is missing. Everything else is reported, not failed.
Exit 1 on a real mismatch, 0 otherwise. Reads only.
"""
import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def main():
    import arch_selfimprove as si
    r = si.verify(APP)
    if not r.get("has_manifest"):
        print("no MANIFEST.sha256: nothing to compare (could not check)")
        return 0
    out = subprocess.run(["git", "ls-files", "-z"], cwd=APP, capture_output=True, text=True)
    if out.returncode != 0:
        print("git ls-files failed: " + out.stderr.strip())
        return 1
    tracked = set(p for p in out.stdout.split("\0") if p)
    lost = [m for m in r["missing"] if m in tracked]
    absent = [m for m in r["missing"] if m not in tracked]
    for title, rows in (("changed since the manifest", r["modified"]), ("tracked but missing", lost)):
        if rows:
            print(f"{title} ({len(rows)}):\n  " + "\n  ".join(rows[:40]))
    if absent:
        print(f"listed in the manifest but not in git (the owner's own files, not expected in a clone): {len(absent)}")
    if r["unlisted"]:
        print(f"in the checkout but not in the manifest (write it with: architect.py self-verify --write): {len(r['unlisted'])}\n  "
              + "\n  ".join(r["unlisted"][:20]))
    bad = r["modified"] or lost
    print("manifest: " + ("MISMATCH" if bad else "ok") + f" ({len(tracked)} tracked files)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
