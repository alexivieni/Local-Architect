"""Self-tests for arch_redact.py (secrets out of everything that is kept or learned from) and the scrub-on-copy in
hub_data/learning/keeper.py. Every fake secret is assembled at run time, so this file itself holds none.
    python arch_selftest_redact.py
"""
import base64
import hashlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import contextlib

import arch_redact as R

HERE = os.path.dirname(os.path.abspath(__file__))


def F(n, seed="x"):
    """n random-looking alphanumeric characters (deterministic), starting with a digit."""
    raw = base64.b64encode(hashlib.sha512(seed.encode()).digest() * 4).decode()
    raw = raw.replace("+", "q").replace("/", "Z").replace("=", "")
    return ("7" + raw)[:n]


def HEX(n, seed="h"):
    return (hashlib.sha512(seed.encode()).hexdigest() * 2)[:n]


SHA256_EMPTY = hashlib.sha256(b"").hexdigest()
SHA1_EMPTY = hashlib.sha1(b"").hexdigest()
UUID = "123e4567-e89b-12d3-a456-426614174000"
PK_BEGIN, PK_END = "-----BEGIN " + "RSA PRIVATE KEY-----", "-----END " + "RSA PRIVATE KEY-----"

# (kind, text, the secret part that must be gone, text that must survive)
POSITIVE = [
    ("anthropic_key", 'client = Anthropic(api_key="' + "sk-ant-" + "api03-" + F(60, "a") + '")', F(60, "a"), "client = Anthropic(api_key="),
    ("openai_key", "OPENAI key: " + "sk-" + "proj-" + F(48, "o"), F(48, "o"), "OPENAI key: "),
    ("openai_key", "old style " + "sk-" + F(48, "o2"), F(48, "o2"), "old style "),
    ("github_token", "remote token " + "gh" + "p_" + F(36, "g"), F(36, "g"), "remote token "),
    ("github_token", "pat=" + "github" + "_pat_" + F(22, "p1") + "_" + F(59, "p2"), F(59, "p2"), "pat="),
    ("aws_key", "id AK" + "IAIOSFODNN7EXAMPLE here", "IOSFODNN7EXAMPLE", " here"),
    ("aws_secret", "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "bPxRfiCYEXAMPLEKEY", "aws_secret_access_key = "),
    ("bearer", 'curl -H "Authorization: Bearer ' + F(40, "b") + '" https://api.local', F(40, "b"), "Authorization: Bearer "),
    ("jwt", "cookie " + "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0." + F(43, "j"), F(43, "j"), "cookie "),
    ("private_key", "before\n" + PK_BEGIN + "\n" + F(64, "k1") + "\n" + F(64, "k2") + "\n" + PK_END + "\nafter", F(64, "k2"), "after"),
    ("private_key", "cut off log: " + PK_BEGIN + "\\n" + F(64, "k3"), F(64, "k3"), "cut off log: "),
    ("url_password", "DATABASE at postgres://admin:" + "s3cr3tP4ss" + "@db.local:5432/app", "s3cr3tP4ss", "postgres://admin:"),
    ("env_secret", "DB_PASSWORD=" + "hunter2", "hunter2", "DB_PASSWORD="),
    ("env_secret", "STRIPE_SECRET_KEY='" + F(30, "s") + "'", F(30, "s"), "STRIPE_SECRET_KEY='"),
    ("env_secret", "export SERVICE_TOKEN=" + "abc123XYZ789", "abc123XYZ789", "export SERVICE_TOKEN="),
    ("env_secret", '{"text": "line one\\nMAIL_PASSWORD=' + "Tr0ub4dor3" + '\\nline three"}', "Tr0ub4dor3", "line three"),
    ("secret", '{"api_key": "' + F(32, "c") + '"}', F(32, "c"), '{"api_key": "'),
    ("secret", '{"content": "cfg = {\\"client_secret\\": \\"' + F(32, "e") + '\\"}"}', F(32, "e"), "client_secret"),
    ("secret", "x-api-key: " + F(28, "x"), F(28, "x"), "x-api-key: "),
    ("secret", "refreshToken => '" + F(26, "r") + "'", F(26, "r"), "refreshToken"),
    ("secret", "token: " + HEX(40, "t"), HEX(40, "t"), "token: "),
]

NORMAL_CODE = "\n".join([
    "import os, getpass, hashlib",
    'API_KEY = os.environ["ANTHROPIC_API_KEY"]',
    "SECRET_KEY = settings_value",
    "token = tokens[0]",
    "max_tokens = 4096",
    "MAX_TOKENS=4096",
    "password = getpass.getpass()",
    "def get_secret(name):",
    "    return vault.read(name)",
    "key = hashlib.sha1(data).hexdigest()",
    "cache_key = make_cache_key_for_session_v2",
    'headers = {"Authorization": f"Bearer {token}"}',
    'url = f"https://{user}:{password}@{host}/db"',
    "    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
    "PASSWORD=",
    "API_KEY=your_api_key_here",
    "the bearer of this letter is welcome",
    "sk-learn is a nickname, and so is sk-some-css-class-name-here",
    'sortKey: "createdAt"',
    'key_file = "config/keys/server2024.pem"',
    'tool_use_id: "toolu_01' + "ABCDEFGHJKMNPQRSTVWXYZab" + '"',
    "https://example.com/path?page=2",
])
HASH_TESTS = "\n".join([
    'assert hashlib.sha256(b"").hexdigest() == "' + SHA256_EMPTY + '"',
    'self.assertEqual(digest, "' + SHA1_EMPTY + '")',
    'cache_key = "' + SHA256_EMPTY + '"',
    '{"sha256": "' + SHA256_EMPTY + '", "etag": "' + HEX(32) + '"}',
    "commit " + SHA1_EMPTY,
])
UUIDS = "\n".join([
    'request_id = "' + UUID + '"',
    "idempotency_key: " + UUID,
    '{"sessionId": "' + UUID + '", "parentUuid": "' + UUID + '"}',
])
_PIXELS = base64.b64encode(b"".join(hashlib.sha256(bytes([i])).digest() for i in range(40))).decode()  # ~1.7 KB
IMAGES = "\n".join([
    '<img src="data:image/png;base64,' + _PIXELS + '">',
    '{"thumbnail": "data:image/jpeg;base64,' + _PIXELS + '"}',
    "background: url(data:image/gif;base64," + _PIXELS[:400] + ")",
])


def run():
    results = []
    not_sure = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))

    # every kind is caught, and only the secret part goes
    seen_kinds = set()
    for kind, text, secret, keep in POSITIVE:
        clean, found = R.scrub_report(text)
        seen_kinds.add(kind)
        check(f"{kind}: caught in {text[:38]!r}", found.get(kind, 0) >= 1 and secret not in clean
              and "[REDACTED:" + kind + "]" in clean, f"{clean!r} {dict(found)}")
        check(f"{kind}: the rest survives", keep in clean, clean)
    want = {"anthropic_key", "openai_key", "github_token", "aws_key", "aws_secret", "bearer", "jwt", "private_key",
            "url_password", "env_secret", "secret"}
    check("all kinds are covered by a case", want <= seen_kinds, want - seen_kinds)
    check("the user name in a URL is kept", "admin:[REDACTED:url_password]@db.local" in R.scrub(POSITIVE[11][1]))
    check("quotes around an env value are kept",
          R.scrub("STRIPE_SECRET_KEY='" + F(30, "s") + "'") == "STRIPE_SECRET_KEY='[REDACTED:env_secret]'")
    pk = R.scrub(POSITIVE[9][1])
    check("a private key block goes as one piece", pk == "before\n[REDACTED:private_key]\nafter", pk)

    # no false positives
    for label, text in (("normal code", NORMAL_CODE), ("hashes in tests", HASH_TESTS), ("UUIDs", UUIDS),
                        ("base64 images", IMAGES)):
        clean, found = R.scrub_report(text)
        diff = [(a, b) for a, b in zip(text.splitlines(), clean.splitlines()) if a != b]
        check(f"{label}: untouched", clean == text and not found, diff[:3])
    check("empty input", R.scrub("") == "" and R.scrub_report("")[1] == {})
    cut = R.scrub("log: " + PK_BEGIN + "\\n" + F(64, "k4") + "\\nThen the run went on normally.")
    check("a cut-off key block does not swallow the rest of the log",
          F(64, "k4") not in cut and "Then the run went on normally." in cut, cut)

    # idempotent
    everything = "\n".join(t for _, t, _, _ in POSITIVE) + "\n" + NORMAL_CODE + "\n" + HASH_TESTS + "\n" + UUIDS
    once = R.scrub(everything)
    check("idempotent: scrub(scrub(x)) == scrub(x)", R.scrub(once) == once)
    check("idempotent: nothing found the second time", not R.scrub_report(once)[1], dict(R.scrub_report(once)[1]))
    for kind, text, _, _ in POSITIVE:
        c = R.scrub(text)
        if R.scrub(c) != c:
            check(f"idempotent for {kind}", False, c)
    check("counts add up", sum(R.scrub_report(everything)[1].values()) == len(POSITIVE),
          dict(R.scrub_report(everything)[1]))

    tmp = tempfile.mkdtemp(prefix="arch_redact_")
    try:
        # scrub_file
        secret_text = "line 1\nDB_PASSWORD=hunter2\r\nline 3 " + "sk-ant-" + F(40, "f") + "\n"
        p = os.path.join(tmp, "a.log")
        with open(p, "wb") as f:
            f.write(secret_text.encode())
        dry = R.scrub_file(p, dry_run=True)
        check("scrub_file dry run reports", dry.get("env_secret") == 1 and dry.get("anthropic_key") == 1, dict(dry))
        check("scrub_file dry run changes nothing", open(p, "rb").read() == secret_text.encode()
              and not os.path.exists(p + ".bak"))
        got = R.scrub_file(p)
        body = open(p, "rb").read().decode()
        check("scrub_file rewrites in place", got and F(40, "f") not in body and "hunter2" not in body, body)
        check("scrub_file keeps line endings", "\r\n" in body and body.startswith("line 1\n"))
        check("scrub_file keeps a .bak of the original", open(p + ".bak", "rb").read() == secret_text.encode())
        check("scrub_file again: no change", not R.scrub_file(p))
        q = os.path.join(tmp, "clean.log")
        with open(q, "w", encoding="utf-8") as f:
            f.write(NORMAL_CODE)
        check("a clean file: no change, no .bak", not R.scrub_file(q) and not os.path.exists(q + ".bak"))
        check("a missing file is not an error", not R.scrub_file(os.path.join(tmp, "nope.log")))

        # scrub_tree
        t = os.path.join(tmp, "tree")
        os.makedirs(os.path.join(t, "proj", "sub"))
        files = {
            os.path.join(t, "agent.log"): "token: " + F(30, "t1"),
            os.path.join(t, "proj", "sub", "s.jsonl"): '{"m": "Bearer ' + F(30, "t2") + '"}\n',
            os.path.join(t, "proj", "keep.bin"): "token: " + F(30, "t3"),
            os.path.join(t, "proj", "old.log.bak"): "token: " + F(30, "t4"),
            os.path.join(t, "proj", "clean.md"): "# notes\n" + HASH_TESTS,
        }
        for fp, txt in files.items():
            with open(fp, "w", encoding="utf-8") as f:
                f.write(txt)
        binp = os.path.join(t, "blob.log")
        with open(binp, "wb") as f:
            f.write(b"\x00\x01token: " + F(30, "t5").encode())
        dry = R.scrub_tree(t, dry_run=True)
        check("scrub_tree dry run lists what would change", set(dry) == {os.path.join(t, "agent.log"),
              os.path.join(t, "proj", "sub", "s.jsonl")}, sorted(dry))
        check("scrub_tree dry run changes nothing", all(open(fp, encoding="utf-8").read() == txt for fp, txt in files.items()))
        res = R.scrub_tree(t, ("*.log", "*.jsonl"))
        check("scrub_tree scrubs matching files", len(res) == 2 and F(30, "t2") not in open(os.path.join(t, "proj", "sub", "s.jsonl")).read())
        check("scrub_tree skips other patterns, .bak files and binaries",
              F(30, "t3") in open(os.path.join(t, "proj", "keep.bin")).read()
              and F(30, "t4") in open(os.path.join(t, "proj", "old.log.bak")).read()
              and F(30, "t5").encode() in open(binp, "rb").read())
        check("scrub_tree on a missing folder", R.scrub_tree(os.path.join(tmp, "missing")) == {})

        # the one-time scrub: dry run by default
        m = os.path.join(tmp, "once")
        os.makedirs(m)
        mp = os.path.join(m, "x.log")
        with open(mp, "w", encoding="utf-8") as f:
            f.write("DB_PASSWORD=hunter2\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            R.main([m])
        check("__main__ default is a dry run", "would scrub" in out.getvalue() and "hunter2" in open(mp).read()
              and "hunter2" not in out.getvalue(), out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            R.main(["--apply", "--no-backup", m])
        check("__main__ --apply rewrites", "hunter2" not in open(mp).read() and not os.path.exists(mp + ".bak"))
        dr = R.default_roots()
        check("default roots are learning + claude_code/projects",
              dr[0].endswith("learning") and dr[1].endswith(os.path.join("claude_code", "projects")), dr)

        # keeper.py copies through the scrubber
        kp = os.path.join(HERE, "hub_data", "learning", "keeper.py")
        if not os.path.isfile(kp):
            # keeper.py lives in the data folder (hub_data/learning), not in the code: without it these checks cannot run, which is
            # not the same as failing (three outcomes).
            not_sure.append("keeper.py copies through the scrubber")
            print(f"  NOT_SURE keeper.py copies through the scrubber  -- could not run: {kp} is not there")
        else:
            spec = importlib.util.spec_from_file_location("arch_keeper_under_test", kp)
            keeper = importlib.util.module_from_spec(spec)
            old_argv = sys.argv
            sys.argv = [kp]
            try:
                spec.loader.exec_module(keeper)
            finally:
                sys.argv = old_argv
            src = os.path.join(tmp, "ag_demo.log")
            dst = os.path.join(tmp, "learning", "agent_logs", "ag_demo.log")
            with open(src, "w", encoding="utf-8") as f:
                f.write("start\nANTHROPIC_API_KEY=" + "sk-ant-" + F(40, "kk") + "\n=== STATUS done | cost $0.12 | turns 3\n")
            seen = {}
            wrote = keeper.copy_new(src, dst, seen)
            kept = open(dst, encoding="utf-8").read()
            check("keeper: the copy is scrubbed", wrote and F(40, "kk") not in kept and "[REDACTED:" in kept, kept)
            check("keeper: the status line survives for the ledger", "=== STATUS done | cost $0.12 | turns 3" in kept)
            check("keeper: an unchanged source is not copied again", keeper.copy_new(src, dst, seen) is False)
            with open(src, "a", encoding="utf-8") as f:
                f.write("more " + "gh" + "p_" + F(36, "kg") + "\n")
            check("keeper: a grown source is copied again, scrubbed",
                  keeper.copy_new(src, dst, seen) and F(36, "kg") not in open(dst, encoding="utf-8").read())
            saved = keeper.arch_redact
            keeper.arch_redact = None
            try:
                dst2 = os.path.join(tmp, "learning", "agent_logs", "other.log")
                check("keeper: no scrubber, no copy", keeper.copy_new(src, dst2, {}) is False and not os.path.exists(dst2))
            finally:
                keeper.arch_redact = saved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    if not_sure:
        print(f"\n  {len(results) - n} of {len(results)} REDACT CHECKS FAILED, {len(not_sure)} COULD NOT RUN (not a pass)")
        return 1
    print(f"\n  {'ALL ' + str(n) + ' REDACT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' REDACT CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
