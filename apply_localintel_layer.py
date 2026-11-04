"""Hook the LocalIntel layer into the Architect harness. Re-run it after dropping in a newer harness.

    python apply_localintel_layer.py             add the hook (safe to run twice) and register the portfolio entry
    python apply_localintel_layer.py --check     only report the state
    python apply_localintel_layer.py --remove    take the hook out again (the harness is then byte-for-byte upstream)

The only harness file touched is arch_flows.py, and only by appending one tagged block at its very end, so there is
no line anchor to go stale. localintel_arch.py (the layer) and this script are not part of the harness and survive an update.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "arch_flows.py")
BEGIN = "# [localintel-bridge] begin"
END = "# [localintel-bridge] end"
BLOCK = f"""

{BEGIN}: app-specific layer (removed by apply_localintel_layer.py --remove)
try:
    import localintel_arch  # noqa: F401
except ImportError:
    pass
{END}
"""


def read():
    with open(TARGET, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(t):
    with open(TARGET, "w", encoding="utf-8", newline="") as f:
        f.write(t)


def state(t):
    return BEGIN in t and END in t


def strip(t):
    a = t.index(BEGIN)
    b = t.index(END) + len(END)
    head = t[:a].rstrip("\n")
    tail = t[b:].lstrip("\n")
    return head + "\n" + (("\n" + tail) if tail else "")


def portfolio(project):
    path = os.path.join(HERE, "portfolio.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    apps = [a for a in (data.get("apps") or []) if isinstance(a, dict) and a.get("name") != "LocalIntel"]
    apps.insert(0, {"name": "LocalIntel", "path": project, "stack": "python/starlette + single-file html UI (sqlite)",
                    "test_command": "{python} -m pytest -q -p no:cacheprovider",
                    "interfaces": ["REST /api/* (governed: localintel/areas.py)", "UI localintel/static/index.html"],
                    "depends_on": [], "notes": "Air-gapped security teaching and operations platform. Rules: AGENT_NOTES.md. Approval gates are never weakened."})
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"apps": apps}, f, indent=2)
    return path


def main(argv):
    if not os.path.isfile(TARGET):
        print("arch_flows.py not found next to this script: is this the harness folder?")
        return 2
    t = read()
    if "--check" in argv:
        print("hook:", "applied" if state(t) else "not applied")
        return 0
    if "--remove" in argv:
        if state(t):
            write(strip(t))
            print("hook removed from arch_flows.py")
        else:
            print("hook was not applied")
        return 0
    if state(t):
        print("hook already applied")
    else:
        write(t.rstrip("\n") + "\n" + BLOCK)
        print("hook added to the end of arch_flows.py")
    project = os.path.abspath(os.path.join(HERE, ".."))
    if os.path.isfile(os.path.join(project, "run.py")):
        print("portfolio entry written:", portfolio(project))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
