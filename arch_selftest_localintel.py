"""Self-test: what the LocalIntel layer DOES once it is switched on (arch_selftest_overlay proves it is off until then).

On a LocalIntel project (run.py + localintel/areas.py + localintel/__init__.py) the layer must:
  * add the project's AGENT_NOTES.md and the house rules to the system prompt the model receives
  * show a big file (index.html > 40 KB) as numbered slices that match the request, never whole
  * refuse to write a protected file (tests/test_invariants.py), and ask a person before a governance file even in --review auto
  * add its own code checks (no stubs added, no approval gate weakened) to the generic ones
On any other project every one of those is exactly what the plain harness does. Scripted model, throwaway home, no network.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    import arch_core as core
    import arch_checks as ck
    import arch_selftest as base

    seen = []

    class Model(base.Fake):
        def script(self, role, user, full, st):
            seen.append(full)
            return "ok"

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="archli_")
    keys = ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST", "ARCH_LOCALINTEL", "LOCALINTEL_ARCH_SELFTEST")
    old_env = {k: os.environ.get(k) for k in keys}
    old_home = core.HOME
    os.environ.update(ARCHITECT_DATA_DIR=os.path.join(tmp, "data"), ARCHITECT_NO_CACHE="1", OLLAMA_HOST=f"127.0.0.1:{fake.server_address[1]}",
                      LOCALINTEL_ARCH_SELFTEST="1")
    os.environ.pop("ARCH_LOCALINTEL", None)
    core.set_home(os.path.join(tmp, "home"))
    with open(core.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"default_model": "fake:7b", "server": {"kind": "ollama"}}, f)

    li_root, plain = os.path.join(tmp, "li"), os.path.join(tmp, "plain")

    def write(root, rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    big = "<html><body>\n" + "".join(f"<section id='s{i}'>\n<p>filler {i} " + "x" * 300 + "</p>\n</section>\n" for i in range(120)) + \
          "<script>\nfunction renderInvoices(rows) {\n  return rows.map(r => r.total);\n}\n</script>\n</body></html>\n"
    for root in (li_root, plain):
        write(root, "app.py", "def total(rows):\n    return sum(rows)\n")
        write(root, "index.html", big)
        write(root, "tests/test_invariants.py", "def test_ok():\n    assert True\n")
        write(root, "AGENT_NOTES.md", "NOTE-XYZ: never weaken the approval gate.\n")
    for rel in ("run.py", "localintel/__init__.py", "localintel/areas.py"):
        write(li_root, rel, "# LocalIntel\n")

    try:
        before_plain = [(r["name"] if isinstance(r, dict) else getattr(r, "name", str(r))) for r in ck.check_files(plain, ["app.py"], core.load_config(plain), None)]
        orig = (core.Engine.call, core.Engine.context, core.Engine._decide, core.safe_path, ck.check_files)
        import localintel_arch as li
        cfg = core.load_config(li_root)
        cfg["localintel"] = {"enabled": True}
        quiet = lambda m: None   # noqa: E731
        e = core.Engine(li_root, "build", "fix renderInvoices", cfg=cfg, persist=False, say=quiet, review="auto")
        p = core.Engine(plain, "build", "fix renderInvoices", cfg=core.deep_merge(core.load_config(plain), {"localintel": {"enabled": True}}),
                        persist=False, say=quiet, review="auto")
        check("with localintel.enabled the layer is on", li._INSTALLED and li.is_li(li_root) and not li.is_li(plain))
        check("it is an extension the engine calls: no engine or checks function was replaced",
              li.EXT in core.EXTENSIONS and (core.Engine.call, core.Engine.context, core.Engine._decide, core.safe_path, ck.check_files) == orig)

        seen.clear()
        e.call("generator", "say ok")
        check("LocalIntel project: the system prompt carries AGENT_NOTES.md and the house rules",
              seen and "LOCALINTEL PROJECT NOTES" in seen[-1] and "NOTE-XYZ" in seen[-1], (seen[-1][:300] if seen else None))
        seen.clear()
        p.call("generator", "say ok")
        check("other project: the system prompt has none of it", seen and "LOCALINTEL PROJECT NOTES" not in seen[-1])

        cx = e.context("renderInvoices", 20000)
        check("LocalIntel project: a big file is shown as a slice that matches the request",
              "slice of a big file" in cx and "function renderInvoices" in cx, cx[-400:])
        check("other project: no slices", "slice of a big file" not in p.context("renderInvoices", 20000))

        try:
            core.safe_path(li_root, "tests/test_invariants.py", cfg)
            refused = False
        except ValueError as ex:
            refused = "safety rules" in str(ex)
        check("LocalIntel project: a protected file is refused", refused)
        check("other project: the same path is allowed", core.safe_path(plain, "tests/test_invariants.py", p.cfg).endswith("test_invariants.py"))

        asked = []
        e.ask = lambda q: (asked.append(q), "n")[1]
        check("LocalIntel project: a governance file needs a person's yes even in review auto",
              e._decide("AGENT_NOTES.md") == "no" and asked and "GOVERNANCE FILE" in asked[-1], asked)
        asked.clear()
        check("LocalIntel project: any other file follows the review mode (auto = yes, nobody asked)", e._decide("app.py") == "yes" and not asked)
        p.ask = lambda q: (asked.append(q), "n")[1]
        check("other project: the governance file is just another file (auto = yes)", p._decide("AGENT_NOTES.md") == "yes" and not asked)

        names = lambda rs: [(r["name"] if isinstance(r, dict) else getattr(r, "name", str(r))) for r in rs]   # noqa: E731
        rd = os.path.join(tmp, "run")                       # a run that changed app.py: its backup holds the file as it was
        write(rd, "backup/app.py", "def total(rows):\n    return sum(rows)\n")
        write(li_root, "app.py", "def total(rows):\n    return sum(rows)\n\n\ndef later(rows):\n    pass\n")
        res = ck.check_files(li_root, ["app.py"], cfg, rd)
        stub = [r for r in res if "no stubs or placeholders added" in (r["name"] if isinstance(r, dict) else getattr(r, "name", ""))]
        check("LocalIntel project: its own code checks are added, and an empty function the run added fails them",
              stub and not (stub[0]["ok"] if isinstance(stub[0], dict) else stub[0].ok), names(res))
        check("other project: exactly the plain harness's checks", names(ck.check_files(plain, ["app.py"], p.cfg, None)) == before_plain)
    finally:
        try:
            import localintel_arch as li
            if hasattr(li, "uninstall"):
                li.uninstall()
        except Exception as ex:
            print("  (could not switch the layer off again:", ex, ")")
        fake.shutdown()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' LOCALINTEL CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' LOCALINTEL CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
