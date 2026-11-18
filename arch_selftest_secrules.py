"""Self-test: the five newer scanner rules in arch_security.sec_scan (tar-slip, tempfile-mktemp, xml-untrusted, path-traversal, ssrf-candidate),
each shown on code that has the problem and on code that does not, and the cloud price list's Mythos rows.

A rule that only ever fires proves nothing, so every rule here has a case it must stay quiet on: zipfile's extractall (it strips '..' itself),
tarfile with a filter, defusedxml, a literal URL, send_from_directory. The scanner is plain AST; no model, no network.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_cloud
    import arch_security as S

    print("\n== newer scanner rules ==")
    d = tempfile.mkdtemp(prefix="secrules_")

    def rules(src, low=True):
        p = os.path.join(d, "t.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        return sorted({x["rule"] for x in S.sec_scan(d, ["t.py"], include_low=low)})

    try:
        check("tar-slip: tarfile.extractall with no filter is a high finding with CWE-22",
              rules("import tarfile\nt = tarfile.open('a.tar')\nt.extractall('out')\n") == ["tar-slip"]
              and S.sec_scan(d, ["t.py"])[0]["cwe"] == "CWE-22" and S.sec_scan(d, ["t.py"])[0]["severity"] == "high")
        check("tar-slip: the same call with filter='data' or members= is quiet",
              rules("import tarfile\nt = tarfile.open('a.tar')\nt.extractall('out', filter='data')\n") == []
              and rules("import tarfile\nt = tarfile.open('a.tar')\nt.extractall('out', members=[])\n") == [])
        check("tar-slip: zipfile.extractall is NOT flagged (zipfile removes '..' and absolute parts itself)",
              rules("import zipfile\nz = zipfile.ZipFile('a.zip')\nz.extractall('out')\n") == []
              and rules("import zipfile, io\nwith zipfile.ZipFile(io.BytesIO(b'')) as z:\n    z.extractall('out')\n") == [])
        check("tar-slip: tarfile.open(...).extractall() chained is found",
              rules("import tarfile\ntarfile.open('a.tar').extractall('out')\n") == ["tar-slip"])

        check("tempfile-mktemp: mktemp() is flagged, mkstemp() is not",
              rules("import tempfile\np = tempfile.mktemp()\n") == ["tempfile-mktemp"] and rules("import tempfile\nfd, p = tempfile.mkstemp()\n") == [])

        check("xml-untrusted: ElementTree and minidom parsing are flagged (medium, CWE-611)",
              rules("import xml.etree.ElementTree as ET\nt = ET.fromstring(data)\n") == ["xml-untrusted"]
              and rules("from xml.dom import minidom\nm = minidom.parseString(data)\n") == ["xml-untrusted"])
        check("xml-untrusted: defusedxml is quiet, and so is code that merely builds XML",
              rules("import defusedxml.ElementTree as ET\nt = ET.fromstring(data)\n") == []
              and rules("import xml.etree.ElementTree as ET\nr = ET.Element('a')\n") == [])

        check("path-traversal: send_file on a run-time path is flagged, a literal path or send_from_directory is not",
              rules("from flask import send_file\ndef f(name):\n    return send_file(name)\n") == ["path-traversal"]
              and rules("from flask import send_file\ndef f():\n    return send_file('static/a.txt')\n") == []
              and rules("from flask import send_from_directory\ndef f(name):\n    return send_from_directory('static', name)\n") == [])

        check("ssrf-candidate: a fetch of a non-literal URL is a LOW finding",
              rules("import requests\ndef f(u):\n    return requests.get(u)\n") == ["ssrf-candidate"]
              and S.sec_scan(d, ["t.py"])[0]["severity"] == "low")
        check("ssrf-candidate: a literal URL is quiet, and the fix flows (include_low=False) never see it",
              rules("import requests\nrequests.get('https://example.com')\n") == []
              and rules("import requests\ndef f(u):\n    return requests.get(u)\n", low=False) == [])

        check("the older rules still fire next to the new ones (weak hash, shell=True)",
              rules("import hashlib, subprocess\nhashlib.md5(b'x')\nsubprocess.run('ls', shell=True)\n") == ["shell-true", "weak-hash"])
    finally:
        shutil.rmtree(d, ignore_errors=True)

    import arch_advisor
    d2 = tempfile.mkdtemp(prefix="secrules_adv_")
    try:
        with open(os.path.join(d2, "a.py"), "w", encoding="utf-8") as f:
            f.write("import requests\n\n\ndef fetch(u):\n    return requests.get(u)\n")
        check("the advisor does not turn a LOW ssrf hint into a proposal for a project that fetches a variable URL",
              arch_advisor.from_code_scan(d2, ["a.py"]) == [], arch_advisor.from_code_scan(d2, ["a.py"]))
        with open(os.path.join(d2, "a.py"), "w", encoding="utf-8") as f:
            f.write("import hashlib\n\n\ndef h(b):\n    return hashlib.md5(b).hexdigest()\n")
        check("the advisor still proposes for a real finding", len(arch_advisor.from_code_scan(d2, ["a.py"])) == 1)
    finally:
        shutil.rmtree(d2, ignore_errors=True)

    print("\n== the network plan check ==")
    import json
    v6 = S.check_cidr_plan(json.dumps({"subnets": [{"name": "a", "cidr": "fd00::/64", "hosts": 10, "gateway": "fd00::1"}, {"name": "c", "cidr": "10.0.0.0/24"}]}))
    check("an IPv6 unique-local subnet next to an IPv4 one no longer crashes the check, and is accepted as private", v6 == [], v6)
    ov = S.check_cidr_plan(json.dumps({"subnets": [{"name": "a", "cidr": "fd00::/64"}, {"name": "b", "cidr": "fd00::/48"}]}))
    check("overlapping IPv6 subnets are still caught", [x["rule"] for x in ov] == ["overlap"], ov)
    pub6 = S.check_cidr_plan(json.dumps({"subnets": [{"name": "p", "cidr": "2001:db8::/32"}]}))
    check("a public IPv6 range is still reported as not private", [x["rule"] for x in pub6] == ["not-private"], pub6)
    check("hosts: true is not read as one host", S.check_cidr_plan(json.dumps({"subnets": [{"name": "x", "cidr": "10.0.0.0/30", "hosts": True}]})) == [])

    print("\n== the cloud price list ==")
    P = arch_cloud.PRICES
    check("Mythos 5.2 and 5.1 are in the price list at the same price as Fable 5.2 and 5.1",
          P.get("claude-mythos-5-2") == P.get("claude-fable-5-2") == (8.0, 40.0) and P.get("claude-mythos-5-1") == P.get("claude-fable-5-1") == (10.0, 50.0), (P.get("claude-mythos-5-2"), P.get("claude-mythos-5-1")))
    check("a Mythos model name resolves to its own price, not the default fallback",
          P.get(arch_cloud.api_model("claude:claude-mythos-5-2")) == (8.0, 40.0))

    _script_rules(check, S)
    _fix_loop(check)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SCANNER-RULE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SCANNER-RULE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _script_rules(check, S):
    print("\n== JavaScript, TypeScript and shell rules ==")
    d = tempfile.mkdtemp(prefix="secrules_js_")

    def rules(name, src, low=True):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(src)
        return sorted({x["rule"] for x in S.sec_scan(d, [name], include_low=low)})
    try:
        cases = [
            ("js-eval", "a.js", "eval(code);\n", "eval('1 + 1');\n"),
            ("js-eval", "a2.ts", "const f = new Function(body);\n", "const f = 1;\n"),
            ("js-innerhtml", "b.js", "el.innerHTML = userInput;\n", "el.innerHTML = 'static';\nel.textContent = userInput;\n"),
            ("js-innerhtml", "b2.jsx", "<div dangerouslySetInnerHTML={{__html: x}} />\n", "<div>{x}</div>\n"),
            ("js-exec", "c.js", "exec(`ls ${dir}`);\n", "execFile('ls', [dir]);\n"),
            ("js-tls", "d.js", "https.request({ rejectUnauthorized: false });\n", "https.request({ rejectUnauthorized: true });\n"),
            ("js-secret", "e.ts", "const apiKey = 'abcdef0123456789abcdef';\n", "const apiKey = process.env.API_KEY;\n"),
            ("js-random", "f.js", "const sessionToken = Math.random().toString(36);\n", "const sessionToken = crypto.randomUUID();\nconst jitter = Math.random();\n"),
            ("js-sql", "g.js", "db.query('select * from t where id=' + id);\n", "db.query('select * from t where id=?', [id]);\n"),
            ("sh-pipe-to-shell", "h.sh", "curl -s https://x.example/i.sh | sh\n", "curl -fsSL -o i.sh https://x.example/i.sh\n"),
            ("sh-tls", "i.sh", "curl -k https://x.example\n", "curl https://x.example\n"),
            ("sh-chmod-777", "j.sh", "chmod -R 777 /srv\n", "chmod 640 /srv/a\n"),
            ("sh-eval", "k.sh", "eval \"$cmd\"\n", "echo \"$cmd\"\n"),
        ]
        for rule, name, bad, good in cases:
            check(f"{rule}: found in {name}, and quiet on the safe form", rules(name, bad) == [rule] and rules(name, good) == [], (rules(name, bad), rules(name, good)))
        check("comments are skipped: a line comment, a block comment and a shell comment never fire",
              rules("m.js", "// eval(code)\n/* el.innerHTML = x;\n   exec(cmd) */\nconst a = 1;\n") == [] and rules("n.sh", "# curl x | sh\n") == [])
        check("a finding carries its CWE id, line and severity", S.sec_scan(d, ["a.js"])[0]["cwe"] == "CWE-95" if rules("a.js", "x();\neval(code);\n") else False)
        check("the line number is exact", [x["line"] for x in S.sec_scan(d, ["a.js"])] == [2])
        check("files that are not Python, script or shell are still skipped", rules("o.txt", "eval(code)\n") == [])
    finally:
        shutil.rmtree(d, ignore_errors=True)


CASES = {
    "tar-slip": {"goal": "fix the security findings in app.py", "role": "security_engineer",
                 "bad": "import tarfile\n\n\ndef unpack(path, dest):\n    with tarfile.open(path) as t:\n        t.extractall(dest)\n",
                 "good": "import tarfile\n\n\ndef unpack(path, dest):\n    with tarfile.open(path) as t:\n        t.extractall(dest, filter='data')\n", "name": "app.py", "lang": "python"},
    "innerhtml": {"goal": "fix the security findings in page.js", "role": "security_engineer",
                  "bad": "function show(el, text) {\n  el.innerHTML = text;\n}\n",
                  "good": "function show(el, text) {\n  el.textContent = text;\n}\n", "name": "page.js", "lang": "javascript"},
}


def _fix_loop(check):
    """The secure flow, run for real against a scripted model: it fixes the planted defect (the same scanner then finds fewer: pass) or hands the file
    back unchanged (the flow must NOT pass). This is the property that matters; no live model is involved."""
    import threading
    from http.server import ThreadingHTTPServer
    import arch_core as core
    import arch_dataflow as df
    import arch_roles
    import arch_selftest
    print("\n== the secure flow repairs the new findings (scripted model) ==")
    home = tempfile.mkdtemp(prefix="secrules_flow_")
    old_home = core.HOME
    core.set_home(home)
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    mode = {"fix": True, "case": None}

    class Scripted(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            c = CASES[mode["case"]]
            if role == c["role"]:
                text = c["good"] if mode["fix"] else c["bad"]
                return f"FILE: {c['name']}\n```{c['lang']}\n{text}```\n"
            return "ok"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    try:
        flows = df.load_all(say=lambda _m: None)
        for key, c in CASES.items():
            for fixed in (True, False):
                mode.update(fix=fixed, case=key)
                proj = os.path.join(home, f"{key}-{'fixed' if fixed else 'unfixed'}")
                os.makedirs(os.path.join(proj, "tests"))
                for rel, text in ((c["name"], c["bad"]), ("tests/test_ok.py", "def test_ok():\n    assert True\n"), ("conftest.py", "")):
                    with open(os.path.join(proj, rel), "w", encoding="utf-8") as f:
                        f.write(text)
                cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                               "behavior": {"battery_saver": "off", "recheck_before_retry": False}, "limits": {"max_revisions": 1}})
                e = core.Engine(proj, "secure", c["goal"], cfg, review="auto", say=lambda *_a, **_k: None)
                out = flows["secure"].run(e, {})
                with open(os.path.join(proj, c["name"]), encoding="utf-8") as f:
                    now = f.read()
                if fixed:
                    check(f"{key}: when the model really fixes it, the same scanner finds fewer and the flow passes, and the file is the fixed one",
                          out["verdict"] == "pass" and now == c["good"], (out["verdict"], out.get("why")))
                else:
                    check(f"{key}: when the model hands back the same code, the flow does NOT pass", out["verdict"] != "pass" and now == c["bad"], out["verdict"])
    finally:
        srv.shutdown()
        core.set_home(old_home)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
