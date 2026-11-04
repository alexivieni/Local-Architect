"""Self-test for the offline installer: what goes in, what is deliberately left out, and that a damaged copy is caught before it runs.

Uses a stand-in model server and a 1 KB stand-in model file, so it needs no GPU, no download and almost no disk.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== the offline installer ==")
    import arch_dist as D
    import arch_server

    tmp = tempfile.mkdtemp(prefix="archdist_")
    quiet = lambda *_a, **_k: None
    real_here, real_resolve = D.HERE, arch_server.resolve_model
    try:
        # ---- a small stand-in app tree, with a stand-in server and the things that must NOT travel
        src = os.path.join(tmp, "src")
        for rel, text in {"architect.py": "print('app')\n", "arch_core.py": "X = 1\n", "flows/build.yaml": "id: build\n",
                          "skills/x.md": "# x\n", "bin/llama/llama-server.exe": "stand-in server", "bin/llama/VERSION.txt": "b9999 test build",
                          "hub_data/model_stats.json": "{\"measured\": \"on the build machine\"}", "architect_config.json": "{\"tiers\": {}}",
                          "__pycache__/x.pyc": "junk", ".arch/runs/r1/trace.jsonl": "{}"}.items():
            p = os.path.join(src, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        fake_gguf = os.path.join(tmp, "worker.gguf")
        with open(fake_gguf, "wb") as f:
            f.write(b"GGUF" + b"\0" * 1020)
        D.HERE = src
        arch_server.resolve_model = lambda spec, cfg=None: ({"name": spec, "gguf": fake_gguf, "size": 1024, "source": "file"}
                                                            if "coder:7b" in (spec or "") else None)

        out = os.path.join(tmp, "out", "architect-gpu6")
        res = D.build("gpu6", out, with_server=True, with_models=True, make_zip=True, say=quiet)
        files = {os.path.relpath(os.path.join(b, f), out).replace("\\", "/") for b, _d, fs in os.walk(out) for f in fs}
        check("the app goes in", "architect/architect.py" in files and "architect/flows/build.yaml" in files and "architect/skills/x.md" in files, sorted(files)[:8])
        check("the model server goes in when asked for", "architect/bin/llama/llama-server.exe" in files and "b9999" in res["server"], res["server"])
        check("a model that can be found here goes in as a file", any(f.startswith("models/") and f.endswith(".gguf") for f in files)
              and len(res["models_included"]) == 1, res["models_included"])
        check("models that cannot be found are listed with the command that installs them",
              len(res["models_to_fetch"]) == 3 and "ollama pull" in open(os.path.join(out, "MODELS.txt"), encoding="utf-8").read(), res["models_to_fetch"])
        check("nothing measured or chosen on the building machine goes in",
              not any("hub_data" in f or f.endswith("architect_config.json") for f in files), [f for f in files if "hub_data" in f or "config" in f])
        check("nor any compiled leftovers or run history", not any("__pycache__" in f or "/.arch/" in f for f in files))
        check("it carries an install script for Windows and one for everything else",
              {"install.bat", "install.sh", "INSTALL.md", "SHA256SUMS", "installer.json"} <= files)
        bat = open(os.path.join(out, "install.bat"), encoding="utf-8").read()
        check("the install script checks the files BEFORE it runs anything, and stops if they do not match",
              bat.index("dist --verify") < bat.index("port --apply") < bat.index("selftest") and "exit /b 1" in bat)
        check("and it picks the team for the machine it lands on, rather than copying one", "port --apply" in bat)
        meta = json.load(open(os.path.join(out, "installer.json"), encoding="utf-8"))
        check("the installer says what it left out, and why", "re-measured" in meta["left_out"], meta["left_out"])

        # ---- checksums
        v = D.verify(out)
        check("a fresh installer matches every checksum", v["ok"] and v["checked"] == res["files"], v)
        with open(os.path.join(out, "architect", "arch_core.py"), "a", encoding="utf-8") as f:
            f.write("# changed after it was built\n")
        v2 = D.verify(out)
        check("a file changed after building is caught", not v2["ok"] and v2["changed"] == ["architect/arch_core.py"], v2)
        os.remove(os.path.join(out, "INSTALL.md"))
        check("a missing file is caught", "INSTALL.md" in D.verify(out)["missing"])
        check("a folder with no checksum list is refused, not trusted", D.verify(tmp)["ok"] is False)
        check("the zip holds the same folder", res.get("zip") and zipfile.ZipFile(res["zip"]).namelist()[0].startswith("architect-gpu6/"))
        check("an unknown profile is refused with the list of real ones", _raises(lambda: D.build("gpu99", os.path.join(tmp, "x"), say=quiet)))

        # ---- the real command line, on a real installer of the real app
        D.HERE, arch_server.resolve_model = real_here, real_resolve
        real_out = os.path.join(tmp, "real", "architect-cpu8")
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "dist", "--profile", "cpu8", "--out", real_out],
                           capture_output=True, text=True, timeout=300)
        check("the real app builds an installer from the command line", r.returncode == 0 and os.path.isfile(os.path.join(real_out, "SHA256SUMS")),
              (r.stdout + r.stderr)[-300:])
        r2 = subprocess.run([sys.executable, os.path.join(real_out, "architect", "architect.py"), "dist", "--verify", real_out],
                            capture_output=True, text=True, timeout=300)
        check("and the installed copy can verify itself, which is the first thing the install script does",
              r2.returncode == 0 and "match their checksums" in r2.stdout, (r2.stdout + r2.stderr)[-300:])
        r3 = subprocess.run([sys.executable, os.path.join(real_out, "architect", "architect.py"), "flows", "--validate"],
                            capture_output=True, text=True, timeout=300)
        check("the installed copy runs: every flow file in it is valid", r3.returncode == 0 and "valid" in r3.stdout, (r3.stdout + r3.stderr)[-300:])
    finally:
        D.HERE, arch_server.resolve_model = real_here, real_resolve
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' INSTALLER CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' INSTALLER CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(run())
