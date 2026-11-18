"""Self-test for the ioc flow's code: indicator parsing, exact matching (IP, CIDR, domain boundaries, hashes, URLs), the citation gate, three outcomes, no network.
No model, no network, no Claude."""
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    home = tempfile.mkdtemp(prefix="archioc_")
    old_home, old_env = core.HOME, os.environ.get("ARCHITECT_DATA_DIR")
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    try:
        import arch_flows  # noqa: F401  (arch_gates imports cleanly after it)
        import arch_gates as G
        import arch_ioc as I

        print("== parsing indicators ==")
        kinds = {v: (I.classify(v) or {}).get("kind") for v in ("203.0.113.9", "10.0.0.0/8", "Evil.Example.ORG", "hxxp://evil[.]example[.]org/x", "http://a.test/x",
                                                               "d41d8cd98f00b204e9800998ecf8427e", "da39a3ee5e6b4b0d3255bfef95601890afd80709", "not an indicator", "999.1.1.1", "# note")}
        check("each indicator form is recognised (IP, CIDR, domain, defanged URL, URL, md5, sha1); junk, a bad IP and a comment are not",
              kinds == {"203.0.113.9": "ip", "10.0.0.0/8": "cidr", "Evil.Example.ORG": "domain", "hxxp://evil[.]example[.]org/x": "url", "http://a.test/x": "url",
                        "d41d8cd98f00b204e9800998ecf8427e": "hash", "da39a3ee5e6b4b0d3255bfef95601890afd80709": "hash", "not an indicator": None, "999.1.1.1": None, "# note": None}, kinds)
        check("a defanged URL is read as written normally", I.classify("hxxp://evil[.]example[.]org/x")["norm"] == "http://evil.example.org/x")

        print("\n== loading the list: three outcomes, plain reasons ==")
        root = tempfile.mkdtemp(prefix="p_", dir=home)
        none = I.load_iocs(root)
        open(os.path.join(root, "iocs.json"), "w").write("{broken")
        bad = I.load_iocs(root)
        open(os.path.join(root, "iocs.json"), "w").write(json.dumps({"indicators": ["junk", "# c"]}))
        empty = I.load_iocs(root)
        open(os.path.join(root, "iocs.json"), "w").write(json.dumps(["1.2.3.4", "1.2.3.4", "evil.example.org"]))
        good = I.load_iocs(root)
        check("no list, broken JSON and a list with nothing usable each return a plain reason (never a silent empty pass); a real list is read and de-duplicated",
              none[2] and "iocs.txt" in none[2] and bad[2] and "not valid JSON" in bad[2] and empty[2] and "no indicator" in empty[2] and not good[2] and len(good[0]) == 2,
              (none[2], bad[2], empty[2], good[2], len(good[0])))

        print("\n== matching is exact ==")
        ev = tempfile.mkdtemp(prefix="e_", dir=home)
        sha = hashlib.sha256(b"payload-bytes\n").hexdigest()
        os.makedirs(os.path.join(ev, "evidence"))
        open(os.path.join(ev, "evidence", "a.log"), "w", newline="\n").write(
            "203.0.113.90 near miss\n203.0.113.9 exact\n198.51.100.200 in cidr\n198.51.101.1 outside\nq login.evil.example.org\nq notevil.example.org\n"
            "q evil.example.org.example.com\nhash " + sha.upper() + "\nhash " + sha[:-1] + "0\nGET http://BAD.example.net/x\n")
        open(os.path.join(ev, "evidence", "blob.bin"), "wb").write(b"payload-bytes\n")
        iocs = [I.classify(x) for x in ("203.0.113.9", "198.51.100.0/24", "evil.example.org", sha, "http://bad.example.net/x")]
        hits, notes = I.match(ev, iocs, ["evidence/a.log", "evidence/blob.bin"])
        got = sorted((h["kind"], h["file"], h["line"]) for h in hits)
        check("an IP matches only exactly (not 203.0.113.90), a CIDR matches its members only, a domain matches itself and its subdomains but not notevil.x or evil.x.example.com, "
              "a hash matches in any case but not a near hash, a URL matches in any case, and a file's own hash matches (line 0)",
              got == sorted([("ip", "evidence/a.log", 2), ("cidr", "evidence/a.log", 3), ("domain", "evidence/a.log", 5), ("hash", "evidence/a.log", 8),
                             ("url", "evidence/a.log", 10), ("hash", "evidence/blob.bin", 0)]), got)
        check("hits get ids H1..Hn in order, and the listing names indicator, file and line",
              [h["id"] for h in hits] == [f"H{i}" for i in range(1, len(hits) + 1)] and "[H1]" in I.hits_text(hits) and "evidence/a.log:2" in I.hits_text(hits))
        check("an unreadable or missing evidence file is a note, not a crash and not a match", I.match(ev, iocs, ["evidence/nope.log"])[1] != [] and I.match(ev, iocs, ["evidence/nope.log"])[0] == [])

        print("\n== the flow's gates ==")
        e = SimpleNamespace(root=ev, say=lambda *a, **k: None, report=lambda *a, **k: None)
        open(os.path.join(ev, "iocs.txt"), "w").write("203.0.113.9\nevil.example.org\n")
        socks = []
        real = socket.socket
        socket.socket = lambda *a, **k: socks.append(1) or real(*a, **k)
        try:
            ctx = {"goal": "match the indicators", "params": {}}
            r = G.GATES["ioc_match"](e, ctx, {}, [])
        finally:
            socket.socket = real
        check("ioc_match finds the matches in the evidence and opens no socket", r["ok"] is True and r["metrics"]["hits"] == 2 and not socks, (r, len(socks)))
        no_ev = tempfile.mkdtemp(prefix="n_", dir=home)
        open(os.path.join(no_ev, "iocs.txt"), "w").write("1.2.3.4\n")
        r2 = G.GATES["ioc_match"](SimpleNamespace(root=no_ev, say=lambda *a, **k: None, report=lambda *a, **k: None), {"goal": "", "params": {}}, {}, [])
        check("no evidence is a refusal with a reason, not zero matches", r2["ok"] is False and "evidence" in r2["text"], r2)

        cited = G.GATES["ioc_cited"]
        base = dict(ctx)
        ok_ = cited(e, dict(base, reply="- [H1] seen `203.0.113.9` post login\n- [H2] sub domain `evil.example.org` queried"), {}, [])
        unknown_id = cited(e, dict(base, reply="- [H1] a\n- [H2] b\n- [H9] c"), {}, [])
        missing = cited(e, dict(base, reply="- [H1] only the first"), {}, [])
        invented_ip = cited(e, dict(base, reply="- [H1] a 203.0.113.9\n- [H2] b, also 8.8.8.8 is suspicious"), {}, [])
        invented_dom = cited(e, dict(base, reply="- [H1] a\n- [H2] b, related to totally-new.example.com"), {}, [])
        check("ioc_cited passes notes that cite every match and nothing else; refuses an id that is not a match, a match with no note, an invented IP and an invented domain",
              ok_["ok"] is True and unknown_id["ok"] is False and "H9" in unknown_id["text"] and missing["ok"] is False and "H2" in missing["text"]
              and invented_ip["ok"] is False and "8.8.8.8" in invented_ip["text"] and invented_dom["ok"] is False and "totally-new" in invented_dom["text"],
              (ok_, unknown_id, missing, invented_ip, invented_dom))

        print("\n== registered and routed ==")
        import arch_flows as F
        import arch_dataflow as df
        check("the ioc flow is a registered YAML flow that uses the ioc_analyst role and its two gates exist",
              "ioc" in df.load_all(say=lambda _m: None) and "ioc_analyst" in F.FLOWS["ioc"].doc["uses_roles"] and {"ioc_match", "ioc_cited"} <= set(G.GATES))
        top = lambda g: sorted(((k, len(__import__("re").findall(p, g.lower()))) for k, p in F.KEYWORDS.items()), key=lambda kv: (-kv[1], F.PRIORITY.index(kv[0])))[0][0]
        check("routing: 'match the indicators of compromise in iocs.txt against the logs' goes to ioc; pentest, secure and deps requests do not",
              top("match the indicators of compromise in iocs.txt against the logs") == "ioc" and top("turn these findings into an attack path write-up for the pentest") == "pentest"
              and top("fix the security issues, sql injection and xss") == "secure" and top("run a dependency audit and list unused packages") == "deps")
    finally:
        core.set_home(old_home)
        if old_env is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_env
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' IOC CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' IOC CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
