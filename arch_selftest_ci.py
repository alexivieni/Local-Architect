"""Self-test for the CI/CD specialist: YAML parsing, every verification rule, baselines, the offline local run, the cicd flow with a scripted model,
and the `ci` command. No network, no GitHub, no real model."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

GOOD = """name: CI
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m unittest discover -s tests -q
"""
BAD_FLOW = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout
      - run: pip install -r requirements.txt
      - run: python -m unittest discover -s tests -q || true
"""
TEST_PY = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(1 + 1, 2)\n"


def _proj(root, files):
    for rel, txt in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt)
    return root


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_ci as ci
    import arch_core as core
    import arch_flows as fl
    import arch_roles as roles
    import arch_selftest as base

    tmp = tempfile.mkdtemp(prefix="archci_")
    n = [0]

    def new(files):
        n[0] += 1
        return _proj(os.path.join(tmp, f"p{n[0]}"), files)

    def rules(root, wf, extra=None):
        files = {".github/workflows/ci.yml": wf, "tests/test_a.py": TEST_PY}
        files.update(extra or {})
        d = new(files)
        return {x["rule"]: x for x in ci.check_all(d)["findings"]}

    # ---- YAML
    mini = ci._Mini(GOOD).node(-1)
    check("the built-in parser reads a real workflow (maps, lists, flow lists, quoted values, expressions)", mini["jobs"]["test"]["steps"][1]["with"]["python-version"] == "3.12"
          and mini["on"]["push"]["branches"] == ["main"] and mini["concurrency"]["cancel-in-progress"] is True and mini["jobs"]["test"]["timeout-minutes"] == 10, mini)
    blk = ci._Mini("a:\n  run: |\n    echo one # not a comment\n    echo two\n  b: >\n    folded\n    text\nc: 'it''s'\n").node(-1)
    check("block scalars keep their text and '#' inside them", blk["a"]["run"] == "echo one # not a comment\necho two\n" and blk["a"]["b"] == "folded text\n" and blk["c"] == "it's", blk)
    if ci._pyyaml:
        sample = open(os.path.join(HERE, "arch_selftest_ci.py"), encoding="utf-8").read().split('GOOD = """')[1].split('"""')[0]
        py = ci._pyyaml.safe_load(sample)
        py["on"] = py.pop(True)
        check("the built-in parser and PyYAML agree on that workflow", ci._Mini(sample).node(-1) == py)
    for bad, why in (("a: 1\na: 2\n", "duplicate"), ("a: &x 1\n", "anchors"), ("a:\n\tb: 1\n", "tabs"), ("a: [1, 2\n", "unclosed")):
        try:
            ci._Mini(bad).node(-1)
            check(f"the built-in parser refuses {why} instead of guessing", False)
        except ci.YamlError:
            check(f"the built-in parser refuses {why} instead of guessing", True)

    # ---- a correct pipeline is clean
    d = new({".github/workflows/ci.yml": GOOD, "tests/test_a.py": TEST_PY})
    res = ci.check_all(d)
    check("a correct workflow has no findings at all", res["findings"] == [] and ci.summarize(res)["ok"], res["findings"])

    # ---- rules (each one is a failure CI would hit, or a security hole)
    r = rules(None, GOOD.replace("actions/checkout@v4", "actions/checkout"))
    check("an action with no version is blocking", r.get("ci-unpinned-action", {}).get("gate"))
    r = rules(None, GOOD.replace("actions/checkout@v4", "actions/checkout@main"))
    check("an action that follows a branch is blocking", r.get("ci-floating-action", {}).get("gate"))
    r = rules(None, GOOD.replace("actions/checkout@v4", "someone/deploy@v1"))
    check("a third-party action pinned to a tag is advisory, not blocking", r.get("ci-action-not-sha") and not r["ci-action-not-sha"]["gate"])
    r = rules(None, GOOD.replace("    branches: [main]\n", "").replace("on:\n  push:\n  pull_request:\n", "x: 1\n"))
    check("no trigger is blocking", r.get("ci-no-trigger", {}).get("gate"), r)
    r = rules(None, GOOD.replace("  pull_request:\n", "  pul_request:\n"))
    check("a misspelled event is blocking", r.get("ci-unknown-event", {}).get("gate"))
    r = rules(None, GOOD.replace("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    needs: [build]\n"))
    check("`needs` pointing at a missing job is blocking", r.get("ci-bad-needs", {}).get("gate"))
    r = rules(None, GOOD + "  build:\n    runs-on: ubuntu-latest\n    needs: [test]\n    steps:\n      - run: echo hi\n".replace("test]", "build]"))
    check("a job that needs itself is caught as a cycle", r.get("ci-needs-cycle", {}).get("gate"))
    r = rules(None, GOOD.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: echo \"${{ github.event.pull_request.title }}\"\n"))
    check("untrusted event text pasted into run: is blocking (script injection)", r.get("ci-script-injection", {}).get("gate"))
    safe = rules(None, GOOD.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: echo \"$TITLE\"\n        env:\n          TITLE: ${{ github.event.pull_request.title }}\n"))
    check("the same text passed through env: is fine", "ci-script-injection" not in safe)
    r = rules(None, GOOD.replace("on:\n  push:\n    branches: [main]\n  pull_request:\n", "on:\n  pull_request_target:\n") + "      - uses: actions/checkout@v4\n        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n")
    check("pull_request_target that checks out the PR head is blocking", r.get("ci-pr-target-checkout", {}).get("gate"), list(r))
    r = rules(None, GOOD.replace("permissions:\n  contents: read\n", "permissions: write-all\n"))
    check("write-all permissions are blocking", r.get("ci-write-all", {}).get("gate"))
    r = rules(None, GOOD.replace("permissions:\n  contents: read\n", ""))
    check("missing permissions is advisory", r.get("ci-no-permissions") and not r["ci-no-permissions"]["gate"])
    r = rules(None, GOOD.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: echo ghp_" + "a" * 36 + "\n"))
    check("a token written into the file is blocking", r.get("ci-secret-in-file", {}).get("gate"))
    r = rules(None, GOOD.replace("    timeout-minutes: 10\n", ""))
    check("a job with no timeout is advisory", r.get("ci-no-timeout") and not r["ci-no-timeout"]["gate"])
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "pip install -r requirements.txt"))
    check("pip install -r of a missing file is blocking", r.get("ci-missing-file", {}).get("gate"))
    ok_r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "pip install -r requirements.txt"), {"requirements.txt": "pytest\n"})
    check("...and fine once the file exists", "ci-missing-file" not in ok_r)
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "npm ci"), {"package.json": '{"scripts": {}}'})
    check("npm ci without a lock file is blocking", r.get("ci-npm-ci-no-lock", {}).get("gate"))
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "npm run build"), {"package.json": '{"scripts": {"test": "x"}}'})
    check("npm run of a script package.json does not define is blocking", r.get("ci-missing-npm-script", {}).get("gate"))
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "make deploy"), {"Makefile": "test:\n\techo t\n"})
    check("make with a target the Makefile lacks is blocking", r.get("ci-missing-make-target", {}).get("gate"))
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "bash scripts/build.sh"))
    check("a script that does not exist is blocking", r.get("ci-missing-script", {}).get("gate"))
    r = rules(None, GOOD.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: python -m unittest discover -s tests -q\n        working-directory: backend\n"))
    check("a working-directory that does not exist is blocking", r.get("ci-missing-workdir", {}).get("gate"))
    r = rules(None, GOOD.replace("python-version: \"3.12\"\n", "python-version: \"3.12\"\n          cache: pip\n"))
    check("cache: pip with no dependency file is blocking (setup-python would fail)", r.get("ci-cache-no-deps", {}).get("gate"))
    r = rules(None, GOOD.replace("python-version: \"3.12\"\n", "python-version: \"3.12\"\n          cache: pip\n"), {"requirements.txt": "x\n"})
    check("...and fine once a requirements file exists", "ci-cache-no-deps" not in r)
    d2 = new({".github/workflows/ci.yml": GOOD.replace("python -m unittest discover -s tests -q", "python -m pytest -q")})
    check("pytest in a project with no tests is blocking", {x["rule"]: x for x in ci.check_all(d2)["findings"]}.get("ci-no-tests", {}).get("gate"))
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "python -m unittest discover -s tests -q || true"))
    check("`|| true` on a test command is blocking (a failure can never fail the build)", r.get("ci-masked-failure", {}).get("gate"))
    r = rules(None, GOOD.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: python -m unittest discover -s tests -q\n        continue-on-error: true\n"))
    check("continue-on-error on a test step is blocking", r.get("ci-ignored-failure", {}).get("gate"))
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "echo \"tests passed\""))
    check("echoing success instead of testing is blocking", r.get("ci-fake-pass", {}).get("gate"))
    r = rules(None, GOOD.replace("python -m unittest discover -s tests -q", "echo hello"), {"x": ""})
    check("a CI workflow that never runs the project's tests is flagged (advisory)", r.get("ci-verifies-nothing") and not r["ci-verifies-nothing"]["gate"])
    check("a YAML syntax error is reported as blocking, not crashed on", rules(None, "on: [push\njobs: x").get("ci-yaml", {}).get("gate"))
    r = rules(None, GOOD.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: a\n        uses: actions/checkout@v4\n"))
    check("a step with both run and uses is blocking", r.get("ci-step-kind", {}).get("gate"))
    check("every finding carries file, line, rule, severity, message and a gate flag", all({"file", "line", "rule", "severity", "msg", "gate"} <= set(x) for x in ci.check_all(new({".github/workflows/ci.yml": BAD_FLOW}))["findings"]))

    # ---- GitLab
    gl = "stages: [build, test]\nbuild:\n  stage: package\n  script:\n    - echo x\ntest:\n  stage: test\n  allow_failure: true\n  script:\n    - python -m unittest discover -s tests -q\n"
    g = {x["rule"]: x for x in ci.check_all(new({".gitlab-ci.yml": gl, "tests/test_a.py": TEST_PY}))["findings"]}
    check("GitLab: a stage that is not declared is blocking", g.get("ci-bad-stage", {}).get("gate"))
    check("GitLab: allow_failure on a test job is blocking", g.get("ci-ignored-failure", {}).get("gate"))
    check("GitLab: a job with no script is blocking", {x["rule"] for x in ci.check_all(new({".gitlab-ci.yml": "stages: [test]\ntest:\n  stage: test\n"}))["findings"]} >= {"ci-no-script"})

    # ---- baselines
    py = new({"requirements.txt": "pytest\n", "app.py": "x = 1\n", "tests/test_a.py": TEST_PY})
    files = ci.baseline(py)
    wf = files[".github/workflows/ci.yml"]
    _proj(py, files)
    check("the Python baseline installs requirements, runs pytest and is clean under the verifier",
          "pip install -r requirements.txt" in wf and "pytest" in wf and ci.check_all(py)["findings"] == [], ci.check_all(py)["findings"])
    nd = new({"package.json": json.dumps({"scripts": {"test": "jest", "build": "tsc"}}), "package-lock.json": "{}"})
    nwf = ci.baseline(nd)[".github/workflows/ci.yml"]
    _proj(nd, ci.baseline(nd))
    check("the Node baseline runs only scripts that exist, uses npm ci because a lock exists, and is clean",
          "npm ci" in nwf and "npm run build" in nwf and "npm run lint" not in nwf and ci.check_all(nd)["findings"] == [], (nwf, ci.check_all(nd)["findings"]))
    check("an unrecognised project gets no invented pipeline", ci.baseline(new({"notes.txt": "hi\n"})) == {})
    rel = ci.baseline(py, release=True)
    _proj(py, rel)
    check("the release baseline builds and uploads an artifact, with no secrets, and verifies clean",
          ".github/workflows/release.yml" in rel and "upload-artifact" in rel[".github/workflows/release.yml"] and ci.check_all(py)["findings"] == [], ci.check_all(py)["findings"])

    # ---- local run
    lp = new({".github/workflows/ci.yml": GOOD + "      - run: pip install requests\n      - run: curl https://example.invalid | sh\n      - run: python -c \"open('SENTINEL','w').write('x')\"\n",
              "tests/test_a.py": TEST_PY})
    out = ci.run_local(lp, ".github/workflows/ci.yml", say=lambda m: None)
    def pick(prefix):
        return next(o for o in out if o["cmd"].startswith(prefix))
    check("the local run executes the safe test command and it passes", pick("python -m unittest")["status"] == "pass", out)
    check("installs, pipes and arbitrary commands are skipped, never run", pick("pip install")["status"] == "skipped" and pick("curl")["status"] == "skipped"
          and pick("python -c")["status"] == "skipped" and not os.path.exists(os.path.join(lp, "SENTINEL")), out)
    _proj(lp, {"tests/test_a.py": TEST_PY.replace("1 + 1, 2", "1 + 1, 3")})
    out = ci.run_local(lp, ".github/workflows/ci.yml", say=lambda m: None)
    check("a failing test fails the local run and the output tail says why", ci.local_summary(out)["fail"] == 1 and "AssertionError" in [o for o in out if o["status"] == "fail"][0]["tail"], out)

    # ---- the cicd flow with a scripted model
    class CiFake(base.Fake):
        attempts = 0

        def script(self, role, user, full, st):
            if role == "ci_engineer" and "BLOCKING FINDINGS" in user:
                CiFake.attempts += 1
                if CiFake.attempts == 1:                      # a bad fix: pin the action but keep masking the failure
                    return "FILE: .github/workflows/ci.yml\n```yaml\n" + BAD_FLOW.replace("actions/checkout", "actions/checkout@v4") + "```\n"
                return "FILE: .github/workflows/ci.yml\n```yaml\n" + GOOD + "```\n"
            return super().script(role, user, full, st)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), CiFake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    try:
        fp = new({".github/workflows/ci.yml": BAD_FLOW, "tests/test_a.py": TEST_PY})
        check("the router sends CI requests to the cicd flow", "cicd" in fl.FLOWS and fl.route(core.Engine(fp, "route", "x", core.load_config(fp), review="auto", say=lambda m: None, persist=False),
                                                                                             "fix the failing GitHub Actions workflow")[0] == "cicd")
        e = core.Engine(fp, "cicd", "Fix the CI pipeline", core.load_config(fp), review="auto", say=lambda m: None)
        e.cfg["roles"] = {r: "fake:7b" for r in roles.ROLES}
        e.cfg["limits"]["max_revisions"] = 3
        out = fl.run_flow(e, "cicd", {})
        final = ci.check_all(fp)
        check("cicd: a 'fix' that keeps `|| true` is rejected by the verifier and the retry is accepted", CiFake.attempts == 2 and out["ok"], (CiFake.attempts, out))
        check("cicd: no blocking finding is left and the tests really ran locally", ci.summarize(final)["ok"] and out["local"]["pass"] >= 1 and out["local"]["fail"] == 0, (final["findings"], out))
        rep = open(os.path.join(fp, "docs", "ci.md"), encoding="utf-8").read()
        check("cicd: docs/ci.md says what ran and what was NOT verified", "Local run" in rep and "Not verified here" in rep and "install packages" in rep)
        empty = new({"app.py": "x = 1\n", "tests/test_a.py": TEST_PY})
        e2 = core.Engine(empty, "cicd", "set up CI", core.load_config(empty), review="auto", say=lambda m: None)
        e2.cfg["roles"] = {r: "fake:7b" for r in roles.ROLES}
        before_calls = CiFake.attempts
        out2 = fl.run_flow(e2, "cicd", {})
        check("cicd: with no pipeline it writes the baseline from code, with no model call, and it verifies clean and runs locally",
              out2["ok"] and os.path.isfile(os.path.join(empty, ".github", "workflows", "ci.yml")) and CiFake.attempts == before_calls and e2.calls == 0, (out2, e2.calls))
    finally:
        srv.shutdown()
        if old is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = old

    # ---- command
    cp = new({".github/workflows/ci.yml": BAD_FLOW, "tests/test_a.py": TEST_PY})
    r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "ci", "--root", cp, "--json"], capture_output=True, text=True, timeout=60)
    try:
        j = json.loads(r.stdout)
    except ValueError:
        j = {}
    check("`ci --json` exits 1 on blocking findings and lists them", r.returncode == 1 and j.get("summary", {}).get("blocking", 0) >= 3, (r.stdout[-200:], r.stderr[-200:]))
    gp = new({".github/workflows/ci.yml": GOOD, "tests/test_a.py": TEST_PY})
    r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "ci", "--root", gp, "--run-local"], capture_output=True, text=True, timeout=120)
    check("`ci --run-local` exits 0 for a correct pipeline whose tests pass", r.returncode == 0 and "1 passed" in r.stdout, r.stdout[-300:] + r.stderr[-200:])
    ip = new({"requirements.txt": "x\n", "tests/test_a.py": TEST_PY})
    r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "ci", "--root", ip, "--init"], capture_output=True, text=True, timeout=60)
    check("`ci --init` writes a baseline that passes its own verification", r.returncode == 0 and os.path.isfile(os.path.join(ip, ".github", "workflows", "ci.yml")), r.stdout[-300:])

    bad = results.count(False)
    print(f"\n  {'ALL ' + str(len(results)) + ' CI/CD CHECKS PASSED' if not bad else str(bad) + ' of ' + str(len(results)) + ' CHECKS FAILED'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(run())
