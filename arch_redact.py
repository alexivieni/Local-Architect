"""Secrets out before anything is kept or learned from.

scrub(text) replaces every credential it recognises with [REDACTED:kind] and leaves everything else byte for byte:
    anthropic_key   sk-ant-...
    openai_key      sk-... / sk-proj-...
    github_token    ghp_ gho_ ghu_ ghs_ ghr_ ... and github_pat_...
    aws_key         AKIA.../ASIA... access key ids;  aws_secret  the 40-character secret next to its name
    slack_token     xoxb-/xoxp-/...;  google_key  AIza...;  jwt  three dot-separated base64 parts starting eyJ
    bearer          the token after "Bearer "
    private_key     a whole -----BEGIN ... PRIVATE KEY----- block (header + base64 lines when the END line is missing)
    url_password    the password in scheme://user:password@host
    env_secret      the value of a .env-style line whose NAME has a KEY/TOKEN/SECRET/PASSWORD part
    secret          a long, high-entropy value right after a name like api_key / token / secret / password

What it deliberately leaves alone: hashes (pure hex) next to a plain "key", UUIDs, data: URIs, variable names on the right of
an assignment in code, file paths, numbers. Running scrub twice gives the same text as running it once.

scrub_file(path) rewrites a file in place (a .bak beside it, only when something changed); scrub_tree(dir, patterns) does a
folder of logs or transcripts. Standard library only; nothing here touches the network.

    python arch_redact.py              dry run over hub_data/learning and hub_data/claude_code/projects: what WOULD change
    python arch_redact.py --apply      do it
    python arch_redact.py --apply DIR  another folder
"""
import argparse
import collections
import fnmatch
import math
import os
import re
import sys

MARK = "[REDACTED:"
DEFAULT_PATTERNS = ("*.log", "*.jsonl", "*.json", "*.md", "*.txt", "*.py", "*.yaml", "*.yml", "*.env", ".env*", "*.cfg", "*.ini",
                    "*.toml", "*.html")
MAX_FILE_BYTES = 200 * 1024 * 1024

# name parts that make a value a credential
_STRONG = {"token", "tokens", "secret", "secrets", "password", "passwords", "passwd", "pwd", "pass", "passphrase",
           "credential", "credentials", "apikey", "privatekey", "authorization", "bearer"}
_WEAK = {"key", "keys", "auth"}
_PLACEHOLDER = re.compile(r"(?i)^(?:x{4,}|\*{3,}|\.{3,}|<.*>|\$.*|%.*%|\{\{.*|your[_\-]?.*|changeme|change[_\-]me|example.*|"
                          r"placeholder.*|dummy.*|none|null|true|false|redacted.*)$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_PATHLIKE = re.compile(r"(^[./~]|/.*\.[A-Za-z0-9]{1,5}$|\\)")
_WORDY = re.compile(r"^[a-z][a-z0-9]*(?:[_.\-][a-z0-9]+){2,}$")


def entropy(s):
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in collections.Counter(s).values())


def _classes(s):
    return sum((any(c.islower() for c in s), any(c.isupper() for c in s), any(c.isdigit() for c in s),
                any(not c.isalnum() and c not in "_ " for c in s)))


def _parts(name):
    """'x-api-key' -> {x, api, key, apikey}; 'clientSecret' -> {client, secret, clientsecret}."""
    words = []
    for chunk in re.split(r"[_\-.\s]+", name):
        words += [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", chunk)]
    out = set(words)
    out.update(a + b for a, b in zip(words, words[1:]))
    return out


def _strength(name):
    p = _parts(name)
    if p & _STRONG:
        return 2
    if p & _WEAK:
        return 1
    return 0


def _looks_secret(value, strength, quoted, sep):
    """The value of `name<sep>value` in the 'secret' rule: long, random-looking, not a hash/UUID/path/variable."""
    if MARK in value or _PLACEHOLDER.match(value) or _UUID.match(value):
        return False
    if len(value) < 20 or not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        return False
    if _HEX.match(value) and strength < 2:
        return False  # a digest next to a plain "key" (cache_key, sha) is not a credential
    if _PATHLIKE.search(value) or _WORDY.match(value):
        return False
    if sep == "=" and not quoted and _IDENT.match(value):
        return False  # code: the right-hand side is a variable
    return entropy(value) >= (3.0 if _HEX.match(value) else 3.5)


# ---- the rules, most specific first -----------------------------------------------------------------------------------
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----(?:(?!-----(?:BEGIN|END) ).)*?"
                          r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----", re.S)
# a block cut off before its END line (a truncated log): the header plus the base64 lines that follow it, nothing more
_PRIVATE_KEY_CUT = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----(?:(?:\\[rn]|\s)*[A-Za-z0-9+/=]{16,})*")
_SIMPLE = [
    ("anthropic_key", re.compile(r"(?<![A-Za-z0-9_\-])sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"(?<![A-Za-z0-9_\-])sk-(?!ant-)(?:[a-z]{2,8}-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("aws_key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("google_key", re.compile(r"(?<![A-Za-z0-9_\-])AIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
]
_AWS_SECRET = re.compile(r"(?i)(aws_?secret_?access_?key[\"']?[ \t]*[:=][ \t]*[\"']?)([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")
_BEARER = re.compile(r"(?i)(\bbearer[ \t]+)([A-Za-z0-9\-._~+/]{16,}=*)")
_URL_PW = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@\"'<>]+:)([^\s@/\"'<>]+)(@)")
# .env line: at a line start, or after a literal "\n" inside a JSON-escaped log line
_ENV = re.compile(r"(?m)((?:^|(?<=\\n))[ \t]*(?:export[ \t]+)?)([A-Z][A-Z0-9_]*)([ \t]*=[ \t]*)"
                  r"(\"[^\"\n\\]*\"|'[^'\n\\]*'|[^\s\"'\\]+)")
# (a quote may arrive JSON-escaped, \" , inside a transcript line)
_CONTEXT = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_\-.]*)((?:\\?[\"'])?[ \t]*)(:|=>|=)([ \t]*)((?:\\?[\"'])?)"
                      r"([A-Za-z0-9+/_\-.=~]{20,})")


def _env_value_secret(raw):
    quoted = raw[:1] in "\"'" and raw[-1:] == raw[:1] and len(raw) >= 2
    v = raw[1:-1] if quoted else raw
    if not v or MARK in v or _PLACEHOLDER.match(v) or len(v) < 6:
        return False
    if not quoted:
        if any(c in v for c in "([{$`") or re.match(r"^-?\d+(\.\d+)?$", v):
            return False
        if _IDENT.match(v) and not re.search(r"\d", v):
            return False  # PASSWORD = some_variable
    return _classes(v) >= 2


def scrub_report(text):
    """(clean_text, Counter of kind -> count)."""
    if not text:
        return text, collections.Counter()
    found = collections.Counter()

    def whole(kind):
        def sub(m):
            found[kind] += 1
            return MARK + kind + "]"
        return sub

    text = _PRIVATE_KEY.sub(whole("private_key"), text)
    text = _PRIVATE_KEY_CUT.sub(whole("private_key"), text)
    for kind, rx in _SIMPLE:
        if kind in ("anthropic_key", "openai_key"):
            def sub(m, kind=kind):
                if not re.search(r"\d", m.group(0)[3:]):
                    return m.group(0)  # sk-some-css-class-name: words, not a key
                found[kind] += 1
                return MARK + kind + "]"
            text = rx.sub(sub, text)
        else:
            text = rx.sub(whole(kind), text)

    def aws_secret(m):
        found["aws_secret"] += 1
        return m.group(1) + MARK + "aws_secret]"
    text = _AWS_SECRET.sub(aws_secret, text)

    def bearer(m):
        if MARK in m.group(2) or _classes(m.group(2)) < 2:
            return m.group(0)
        found["bearer"] += 1
        return m.group(1) + MARK + "bearer]"
    text = _BEARER.sub(bearer, text)

    def url_pw(m):
        pw = m.group(2)
        if pw.startswith("[REDACTED") or pw[:1] in "{$%<*" or _PLACEHOLDER.match(pw):
            return m.group(0)  # already done, or a template like {password} / ${PW} / ****
        found["url_password"] += 1
        return m.group(1) + MARK + "url_password]" + m.group(3)
    text = _URL_PW.sub(url_pw, text)

    def env(m):
        name, raw = m.group(2), m.group(4)
        p = _parts(name)
        if not (p & (_STRONG | {"key", "keys"})) or not _env_value_secret(raw):
            return m.group(0)
        found["env_secret"] += 1
        q = raw[0] if raw[:1] in "\"'" and raw[-1:] == raw[:1] and len(raw) >= 2 else ""
        return m.group(1) + name + m.group(3) + q + MARK + "env_secret]" + q
    text = _ENV.sub(env, text)

    def context(m):
        name, sep, quote, value = m.group(1), m.group(3), m.group(5), m.group(6)
        strength = _strength(name)
        if not strength or not _looks_secret(value, strength, bool(quote), sep):
            return m.group(0)
        found["secret"] += 1
        return m.group(1) + m.group(2) + sep + m.group(4) + quote + MARK + "secret]"
    text = _CONTEXT.sub(context, text)
    return text, found


def scrub(text):
    """The text with every recognised secret replaced by [REDACTED:kind]."""
    return scrub_report(text)[0]


def _read(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="surrogateescape")


def _write(path, text):
    tmp = path + ".redact.tmp"
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8", errors="surrogateescape"))
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def scrub_file(path, dry_run=False, backup=True):
    """Scrub one file in place. Returns a Counter of what was (or, with dry_run, would be) redacted; empty = untouched.
    The original goes to path + '.bak' only when something changed (note: that .bak still holds the secrets)."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return collections.Counter()
        text = _read(path)
    except OSError:
        return collections.Counter()
    if text is None:
        return collections.Counter()
    clean, found = scrub_report(text)
    if clean == text or dry_run:
        return found if clean != text else collections.Counter()
    if backup:
        _write(path + ".bak", text)
    _write(path, clean)
    return found


def scrub_tree(root, patterns=DEFAULT_PATTERNS, dry_run=False, backup=True):
    """Scrub every file under root whose name matches one of patterns. Returns {path: Counter} for the files that changed."""
    if isinstance(patterns, str):
        patterns = (patterns,)
    out = {}
    if not os.path.isdir(root):
        return out
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "node_modules")]
        for fn in sorted(files):
            if fn.endswith((".bak", ".redact.tmp")) or not any(fnmatch.fnmatch(fn, p) for p in patterns):
                continue
            p = os.path.join(d, fn)
            found = scrub_file(p, dry_run=dry_run, backup=backup)
            if found:
                out[p] = found
    return out


def default_roots():
    home = os.path.dirname(os.path.abspath(__file__))
    data = os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(home, "hub_data")
    return [os.path.join(data, "learning"), os.path.join(data, "claude_code", "projects")]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remove secrets from kept logs and transcripts (dry run unless --apply).")
    ap.add_argument("roots", nargs="*", help="folders to scrub (default: hub_data/learning and hub_data/claude_code/projects)")
    ap.add_argument("--apply", action="store_true", help="rewrite the files (a .bak is kept beside each changed one)")
    ap.add_argument("--no-backup", action="store_true", help="with --apply: do not keep a .bak (it would still hold the secret)")
    a = ap.parse_args(argv)
    roots = a.roots or default_roots()
    total, files = collections.Counter(), 0
    for root in roots:
        if not os.path.isdir(root):
            print(f"  skip (not a folder): {root}")
            continue
        res = scrub_tree(root, dry_run=not a.apply, backup=not a.no_backup)
        for p, found in sorted(res.items()):
            files += 1
            total.update(found)
            kinds = ", ".join(f"{k} x{n}" for k, n in sorted(found.items()))
            print(f"  {'scrubbed' if a.apply else 'would scrub'}: {os.path.relpath(p, root)}  ({kinds})")
    summary = ", ".join(f"{k} x{n}" for k, n in sorted(total.items())) or "nothing"
    verb = "scrubbed" if a.apply else "would scrub"
    print(f"\n  {verb} {files} file(s): {summary}")
    if not a.apply and files:
        print("  dry run: nothing was changed. Run again with --apply to rewrite them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
