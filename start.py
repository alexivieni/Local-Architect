"""Menu launcher for Architect.   python start.py [key]"""
import os, re, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
MENU = [("H", "Start Architect: Ollama if needed, the model servers, and the hub (recommended)", ["architect.py", "start"]),
        ("1", "Self-test (fake model, no Ollama needed)", ["architect.py", "selftest"]),
        ("2", "List the flows", ["architect.py", "flows"]),
        ("3", "Show settings in effect", ["architect.py", "show-config"]),
        ("4", "Count training examples", ["arch_training.py", "--stats"]),
        ("5", "Test the harness with YOUR models (quick live test)", ["architect.py", "livetest", "--quick"]),
        ("6", "Put an Architect shortcut on the Desktop", ["architect.py", "shortcut"]),
        ("7", "Status (Ollama, models, power, other machines)", ["architect.py", "status"]),
        ("U", "After a hardware upgrade: what Architect sees, its plan, and measured speeds", ["after_upgrade.py"])]


def python_for_tests():
    """This Python when it can run a project's tests (pytest), else the newest installed Python that can, else this one with a note.
    Seen live: the py launcher picked a newly installed Python 3.15 without pytest, so every check that runs tests quietly did less."""
    import importlib.util
    if importlib.util.find_spec("pytest"):
        return sys.executable
    cands = []
    if os.name == "nt":
        try:
            out = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        cands = [m.group(1) for m in (re.search(r"([A-Za-z]:\\.*?python\.exe)\s*$", ln.strip(), re.I) for ln in out.splitlines()) if m]
    else:
        import shutil
        cands = [p for p in (shutil.which(f"python3.{v}") for v in range(20, 9, -1)) if p]
    for exe in cands:                                          # newest first, as the launcher lists them
        if os.path.normcase(os.path.abspath(exe)) == os.path.normcase(os.path.abspath(sys.executable)):
            continue
        try:
            if subprocess.run([exe, "-c", "import pytest"], capture_output=True, timeout=30).returncode == 0:
                print(f"Using {exe}: it can run your tests (pytest); the default Python here cannot.")
                return exe
        except (OSError, subprocess.SubprocessError):
            continue
    print("Note: no installed Python has pytest, so checks that run tests will do less. Install it once with:  python -m pip install pytest")
    return sys.executable


def main():
    key = (sys.argv[1] if len(sys.argv) > 1 else "").upper()
    if not key:
        print("\nArchitect\n")
        for k, l, _ in MENU:
            print(f"  [{k}] {l}")
        print("  [Q] Quit")
        try:
            key = input("\n> ").strip().upper()
        except EOFError:
            return 0
    for k, _, argv in MENU:
        if k == key:
            return subprocess.call([python_for_tests(), *argv], cwd=HERE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
