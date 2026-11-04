"""CI/CD verification, baseline generation and a local dry run. Everything here is local: nothing contacts GitHub, GitLab or a registry.

What code decides (no model involved):
  * the pipeline file parses, has triggers/jobs/steps, and `needs` point at real jobs (no cycles);
  * actions are pinned (a branch or no version fails; a third-party action not pinned to a commit SHA is advisory);
  * the classic security mistakes: untrusted `github.event.*` text pasted into `run:` (script injection), `pull_request_target` that checks out
    the PR head, `permissions: write-all`, secrets written into the file;
  * the pipeline matches the repository: `pytest` with no tests, `npm ci` with no lock file, `pip install -r` a missing file, `make` targets,
    scripts and working directories that do not exist, `cache: pip` with no dependency file (a common reason CI fails on the first run);
  * fake verification: `pytest || true`, `continue-on-error` on a test step, `echo "tests passed"`;
  * a local dry run of the safe steps (tests, linters, builds) with the same commands, offline.

YAML: PyYAML is used when it is installed; otherwise a small built-in parser handles the subset CI files use and says so when it meets
something it cannot read (anchors, merge keys) rather than guessing.
"""
import json
import os
import re
import shlex
import subprocess
import sys

import arch_core as core

try:                                                # optional: more complete parsing when present
    import yaml as _pyyaml
except ImportError:
    _pyyaml = None


class YamlError(ValueError):
    pass


# --------------------------------------------------------------------------------------------- tiny YAML subset
def _strip_comment(s):
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q and (q == "'" or s[i - 1] != "\\"):
                q = None
        elif ch in "'\"" and (i == 0 or s[i - 1] in " \t[{,:-"):
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
    return s


def _scalar(s):
    s = s.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        body = s[1:-1]
        return body.replace("''", "'") if s[0] == "'" else body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    if s[0] in "&*" or s.startswith("<<"):
        raise YamlError("anchors and aliases are not supported by the built-in parser (install PyYAML for full YAML)")
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?\d*\.\d+", s):
        return float(s)
    return s


def _split_top(s, sep=","):
    out, depth, q, cur = [], 0, None, []
    for i, ch in enumerate(s):
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "'\"":
            q = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def _find_colon(s):
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == ":" and (i + 1 == len(s) or s[i + 1] in " \t"):
            return i
        elif ch in "[{" and i == 0:
            return -1
    return -1


def _flow(s):
    s = s.strip()
    if s.startswith("["):
        if not s.endswith("]"):
            raise YamlError("unclosed [ in a flow list")
        return [_flow(x) for x in _split_top(s[1:-1])]
    if s.startswith("{"):
        if not s.endswith("}"):
            raise YamlError("unclosed { in a flow map")
        out = {}
        for part in _split_top(s[1:-1]):
            k = _find_colon(part)
            if k < 0:
                raise YamlError("a flow map entry needs 'key: value'")
            out[_scalar(part[:k]) if part[:k].strip()[:1] in "'\"" else part[:k].strip()] = _flow(part[k + 1:])
        return out
    return _scalar(s)


class _Mini:
    def __init__(self, text):
        if re.search(r"^\t|^ *\t", text, re.M):
            raise YamlError("tabs cannot be used for indentation in YAML")
        self.raw = text.replace("\r\n", "\n").split("\n")
        self.i = 0

    def peek(self):
        while self.i < len(self.raw):
            s = _strip_comment(self.raw[self.i]).rstrip()
            if s.strip() == "" or s.strip() in ("---", "..."):
                self.i += 1
                continue
            return len(s) - len(s.lstrip(" ")), s.strip()
        return None

    def node(self, parent):
        pk = self.peek()
        if pk is None or pk[0] <= parent:
            return None
        if pk[1].startswith("- ") or pk[1] == "-":
            return self.seq(pk[0])
        return self.map(pk[0])

    def seq(self, ind):
        out = []
        while True:
            pk = self.peek()
            if pk is None or pk[0] != ind or not (pk[1].startswith("- ") or pk[1] == "-"):
                break
            rest = pk[1][1:].lstrip(" ")
            if rest == "":
                self.i += 1
                out.append(self.node(ind))
                continue
            gap = len(self.raw[self.i]) - len(self.raw[self.i].lstrip(" ")) + 1
            gap += len(pk[1][1:]) - len(pk[1][1:].lstrip(" "))
            self.raw[self.i] = " " * gap + rest
            if rest.startswith("- ") or _find_colon(rest) > 0:
                out.append(self.node(ind))
            elif rest[0] in "|>":
                out.append(self.block(rest, ind))
            else:
                self.i += 1
                out.append(_flow(rest) if rest[0] in "[{" else _scalar(rest))
        return out

    def block(self, head, key_indent):
        """| or > scalar: every following line indented deeper than the key (blank lines kept)."""
        self.i += 1
        lines, indent = [], None
        while self.i < len(self.raw):
            ln = self.raw[self.i]
            if ln.strip() == "":
                lines.append("")
                self.i += 1
                continue
            cur = len(ln) - len(ln.lstrip(" "))
            if cur <= key_indent:
                break
            indent = cur if indent is None else min(indent, cur)
            lines.append(ln)
            self.i += 1
        while lines and lines[-1] == "":
            lines.pop()
        body = [l[indent:] if l else "" for l in lines] if indent else []
        text = "\n".join(body)
        if head.startswith(">"):
            text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
        return text + ("" if head.endswith("-") else "\n")

    def map(self, ind):
        out = {}
        while True:
            pk = self.peek()
            if pk is None or pk[0] != ind or pk[1].startswith("- "):
                break
            line = pk[1]
            c = _find_colon(line)
            if c < 0:
                raise YamlError(f"line {self.i + 1}: expected 'key: value', found '{line[:40]}'")
            kraw, rest = line[:c].strip(), line[c + 1:].strip()
            if kraw.startswith("<<") or kraw.startswith("&") or kraw.startswith("*"):
                raise YamlError("anchors and merge keys are not supported by the built-in parser (install PyYAML for full YAML)")
            key = _scalar(kraw) if kraw[:1] in "'\"" else kraw
            if key in out:
                raise YamlError(f"line {self.i + 1}: duplicate key '{key}'")
            if rest == "":
                self.i += 1
                nxt = self.peek()
                if nxt and nxt[0] == ind and nxt[1].startswith("- "):
                    out[key] = self.seq(ind)
                else:
                    out[key] = self.node(ind)
            elif rest[0] in "|>":
                out[key] = self.block(rest, ind)
            elif rest[0] in "[{":
                buf = rest
                self.i += 1
                while buf.count("[") + buf.count("{") > buf.count("]") + buf.count("}") and self.i < len(self.raw):
                    buf += " " + _strip_comment(self.raw[self.i]).strip()
                    self.i += 1
                out[key] = _flow(buf)
            else:
                self.i += 1
                out[key] = _scalar(rest)
                nxt = self.peek()
                if nxt and nxt[0] > ind:
                    raise YamlError(f"line {self.i + 1}: unexpected indented text after '{key}: {rest[:30]}' (multi-line plain scalars are not supported)")
        return out


def load_yaml(text):
    """-> python object. Raises YamlError. GitHub's `on` key (which YAML 1.1 reads as True) is returned as "on"."""
    if _pyyaml is not None:
        try:
            obj = _pyyaml.safe_load(text)
        except _pyyaml.YAMLError as e:
            raise YamlError(str(e).replace("\n", " ")[:300])
    else:
        p = _Mini(text)
        obj = p.node(-1)
        if p.peek() is not None:
            raise YamlError(f"line {p.i + 1}: could not read this line")
    if isinstance(obj, dict) and True in obj and "on" not in obj:
        obj["on"] = obj.pop(True)
    return obj


# --------------------------------------------------------------------------------------------- finding helpers
def F(file, rule, severity, msg, gate=False, line=0):
    return {"file": file, "line": line, "rule": rule, "severity": severity, "msg": msg, "gate": bool(gate)}


def _line_of(text, needle):
    if not needle:
        return 0
    needle = needle.strip().split("\n")[0][:60]
    for n, ln in enumerate(text.split("\n"), 1):
        if needle in ln:
            return n
    return 0


def find_pipelines(root):
    out = []
    wf = os.path.join(root, ".github", "workflows")
    if os.path.isdir(wf):
        out += [("github", f".github/workflows/{n}") for n in sorted(os.listdir(wf)) if n.endswith((".yml", ".yaml"))]
    if os.path.isfile(os.path.join(root, ".gitlab-ci.yml")):
        out.append(("gitlab", ".gitlab-ci.yml"))
    return out


# --------------------------------------------------------------------------------------------- rules
KNOWN_EVENTS = {"push", "pull_request", "pull_request_target", "workflow_dispatch", "workflow_call", "workflow_run", "schedule", "release", "create", "delete",
                "deployment", "deployment_status", "fork", "issues", "issue_comment", "label", "milestone", "page_build", "project", "public", "registry_package",
                "repository_dispatch", "status", "watch", "check_run", "check_suite", "discussion", "discussion_comment", "merge_group", "pull_request_review",
                "pull_request_review_comment", "branch_protection_rule", "gollum", "member", "project_card", "project_column", "package"}
INJECTION = re.compile(r"\$\{\{[^}]*\b(github\.event\.(issue\.(title|body)|pull_request\.(title|body|head\.(ref|label)|head\.repo\.default_branch)|comment\.body|"
                       r"review\.body|review_comment\.body|pages\.[^}]*page_name|head_commit\.(message|author\.(email|name))|commits[^}]*\.(message|author)|"
                       r"workflow_run\.(head_branch|head_commit\.message))|github\.head_ref)[^}]*\}\}")
SECRET_RX = [(re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "a GitHub token"), (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
             (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"), (re.compile(r"\bxox[abp]-[A-Za-z0-9-]{20,}"), "a Slack token")]
TEST_WORDS = re.compile(r"\b(pytest|unittest|nosetests|tox|npm (?:run )?test|yarn test|pnpm test|go test|cargo test|mvn(?:w)? (?:-\S+ )*(?:test|verify)|gradle(?:w)? test|dotnet test|make (?:test|check)|rspec|phpunit|bundle exec rake test)\b")
MASKS = re.compile(r"\|\|\s*(true|:|exit 0)\s*$|;\s*exit 0\s*$")
FAKE_ECHO = re.compile(r"^\s*echo\s+['\"]?(all )?(tests?|checks?|build) (passed|ok|succeeded|successful)", re.I)


def _has_tests(root):
    if os.path.isdir(os.path.join(root, "tests")) or os.path.isdir(os.path.join(root, "test")):
        return True
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in (".git", "node_modules", "venv", ".venv", "__pycache__", ".arch")]
        if any(f.startswith("test_") and f.endswith(".py") or f.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts", "_test.go")) for f in files):
            return True
        if d.count(os.sep) - root.count(os.sep) > 4:
            dirs[:] = []
    return False


def _pkg_scripts(root):
    p = os.path.join(root, "package.json")
    try:
        with open(p, encoding="utf-8") as f:
            return (json.load(f) or {}).get("scripts") or {}
    except (OSError, ValueError):
        return None


def _make_targets(root):
    for n in ("Makefile", "makefile", "GNUmakefile"):
        p = os.path.join(root, n)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", f.read(), re.M))
            except OSError:
                return set()
    return None


def command_lines(script):
    """A `run:`/`script:` value -> its commands, one per line, joined continuation lines, comments dropped."""
    out, buf = [], ""
    for ln in str(script or "").split("\n"):
        s = ln.rstrip()
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        s = (buf + s).strip()
        buf = ""
        if s and not s.startswith("#"):
            out.append(s)
    return out


def check_commands(root, file, text, cmds, workdir=None, ctx=""):
    """Does each command refer to things that exist in this repository? Every finding here is a failure the first CI run would hit."""
    out = []
    base = os.path.join(root, workdir) if workdir else root
    if workdir and not os.path.isdir(base):
        out.append(F(file, "ci-missing-workdir", "high", f"{ctx}working-directory '{workdir}' does not exist", True, _line_of(text, workdir)))
        return out
    scripts, targets = _pkg_scripts(base), _make_targets(base)
    for c in cmds:
        for part in re.split(r"\s*&&\s*", c):
            p = part.strip()
            if not p:
                continue
            ln = _line_of(text, p)
            m = re.match(r"(?:python3?|py)\s+-m\s+pytest\b|pytest\b", p)
            if m and not _has_tests(base):
                out.append(F(file, "ci-no-tests", "medium", f"{ctx}runs pytest but the project has no tests (pytest exits with an error when it collects nothing)", True, ln))
            m = re.match(r"(?:pip3?|python3? -m pip) install\b.*?(?:-r|--requirement)\s+(\S+)", p)
            if m and "$" not in m.group(1) and not os.path.isfile(os.path.join(base, m.group(1))):
                out.append(F(file, "ci-missing-file", "high", f"{ctx}installs from '{m.group(1)}' which does not exist", True, ln))
            if re.match(r"npm ci\b", p) and not os.path.isfile(os.path.join(base, "package-lock.json")):
                out.append(F(file, "ci-npm-ci-no-lock", "high", f"{ctx}`npm ci` needs package-lock.json, which is missing (commit it or use npm install)", True, ln))
            name = None
            if re.match(r"npm\s+(?:test|t)\b", p):
                name = "test"
            else:
                m = re.match(r"npm\s+run(?:-script)?\s+([\w:-]+)", p)
                if m:
                    name = m.group(1)
            if name and scripts is not None and name not in scripts and "--if-present" not in p:
                out.append(F(file, "ci-missing-npm-script", "high", f"{ctx}`{p}` but package.json has no '{name}' script", True, ln))
            elif name and scripts is None:
                out.append(F(file, "ci-no-package-json", "high", f"{ctx}`{p}` but there is no package.json", True, ln))
            m = re.match(r"make\s+(?:-\S+\s+)*([A-Za-z0-9_.-]+)", p)
            if m:
                if targets is None:
                    out.append(F(file, "ci-no-makefile", "high", f"{ctx}`{p}` but there is no Makefile", True, ln))
                elif m.group(1) not in targets:
                    out.append(F(file, "ci-missing-make-target", "high", f"{ctx}Makefile has no '{m.group(1)}' target", True, ln))
            m = re.match(r"(?:bash|sh|python3?|node)\s+(\.?/?[\w./-]+\.(?:sh|py|js|mjs))\b|(\./[\w./-]+)\s*(?:$|\s)", p)
            if m:
                target = m.group(1) or m.group(2)
                if "$" not in target and not os.path.isfile(os.path.join(base, target)):
                    out.append(F(file, "ci-missing-script", "high", f"{ctx}runs '{target}' which does not exist", True, ln))
            m = re.match(r"cd\s+([\w./-]+)\s*$", p)
            if m and not os.path.isdir(os.path.join(base, m.group(1))):
                out.append(F(file, "ci-missing-workdir", "high", f"{ctx}`cd {m.group(1)}` but that folder does not exist", True, ln))
        if TEST_WORDS.search(c) and MASKS.search(c):
            out.append(F(file, "ci-masked-failure", "high", f"{ctx}`{c[:70]}` swallows the exit code, so a failing test can never fail the build", True, _line_of(text, c)))
        if FAKE_ECHO.match(c):
            out.append(F(file, "ci-fake-pass", "high", f"{ctx}`{c[:70]}` prints success without running anything", True, _line_of(text, c)))
    return out


def _is_pinned_sha(ref):
    return bool(re.fullmatch(r"[0-9a-f]{40}", ref or ""))


def check_github(root, rel, text, doc=None):
    out = []
    if doc is None:
        try:
            doc = load_yaml(text)
        except YamlError as e:
            return [F(rel, "ci-yaml", "high", f"does not parse as YAML: {e}", True)]
    if not isinstance(doc, dict):
        return [F(rel, "ci-structure", "high", "the workflow is not a YAML mapping", True)]
    trig = doc.get("on")
    if trig in (None, {}, []):
        out.append(F(rel, "ci-no-trigger", "high", "no `on:` trigger, so this workflow never runs", True, _line_of(text, "on:")))
    else:
        evs = [trig] if isinstance(trig, str) else list(trig.keys()) if isinstance(trig, dict) else [x for x in trig if isinstance(x, str)]
        for ev in evs:
            if ev not in KNOWN_EVENTS:
                out.append(F(rel, "ci-unknown-event", "high", f"'{ev}' is not a GitHub event", True, _line_of(text, str(ev))))
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        out.append(F(rel, "ci-no-jobs", "high", "no jobs defined", True, _line_of(text, "jobs:")))
        return out
    perms = doc.get("permissions")
    if perms == "write-all" or (isinstance(perms, dict) and perms.get("contents") == "write" and len([v for v in perms.values() if v == "write"]) > 3):
        out.append(F(rel, "ci-write-all", "high", "permissions grant broad write access; give each job only what it needs", True, _line_of(text, "permissions")))
    any_perm = perms is not None or any(isinstance(j, dict) and "permissions" in j for j in jobs.values())
    if not any_perm:
        out.append(F(rel, "ci-no-permissions", "medium", "no `permissions:`: the token gets the repository default, which may be read-write (set `contents: read`)", False, 1))
    names = set(jobs)
    if "pull_request_target" in (trig if isinstance(trig, (dict, list)) else [trig]) and re.search(r"github\.event\.pull_request\.head\.(sha|ref)|github\.head_ref", text):
        out.append(F(rel, "ci-pr-target-checkout", "high", "pull_request_target runs with secrets; checking out the PR's own code there lets a fork run code with them", True,
                     _line_of(text, "pull_request_target")))
    for pat, what in SECRET_RX:
        m = pat.search(text)
        if m:
            out.append(F(rel, "ci-secret-in-file", "high", f"{what} is written into the file; move it to the repository secrets", True, _line_of(text, m.group(0)[:20])))
    deps = {}
    for jn, job in jobs.items():
        if not isinstance(job, dict):
            out.append(F(rel, "ci-job-shape", "high", f"job '{jn}' is not a mapping", True, _line_of(text, f"{jn}:")))
            continue
        ctx = f"job '{jn}': "
        need = job.get("needs") or []
        need = [need] if isinstance(need, str) else list(need)
        deps[jn] = need
        for n in need:
            if n not in names:
                out.append(F(rel, "ci-bad-needs", "high", f"{ctx}needs '{n}', which is not a job", True, _line_of(text, f"needs")))
        if "uses" in job and "steps" not in job:
            continue                                                     # a reusable-workflow call
        if "runs-on" not in job:
            out.append(F(rel, "ci-no-runs-on", "high", f"{ctx}no `runs-on`", True, _line_of(text, f"{jn}:")))
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            out.append(F(rel, "ci-no-steps", "high", f"{ctx}no steps", True, _line_of(text, f"{jn}:")))
            continue
        if "timeout-minutes" not in job:
            out.append(F(rel, "ci-no-timeout", "low", f"{ctx}no `timeout-minutes` (a hung step holds a runner for up to 6 hours)", False, _line_of(text, f"{jn}:")))
        wd = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
        for si, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                out.append(F(rel, "ci-step-shape", "high", f"{ctx}step {si} is not a mapping", True))
                continue
            sctx = f"{ctx}step {si}: "
            if ("run" in st) == ("uses" in st):
                out.append(F(rel, "ci-step-kind", "high", f"{sctx}needs exactly one of `run` or `uses`", True, _line_of(text, st.get("name") or "")))
                continue
            if "uses" in st:
                out += _check_uses(root, rel, text, st, sctx)
                continue
            run = str(st["run"])
            m = INJECTION.search(run)
            if m:
                out.append(F(rel, "ci-script-injection", "high", f"{sctx}`{m.group(0)[:60]}` is attacker-controlled text pasted into a shell command (CWE-94); pass it through `env:` and quote it", True, _line_of(text, m.group(0)[:30])))
            cmds = command_lines(run)
            out += check_commands(root, rel, text, cmds, st.get("working-directory") or wd, sctx)
            if st.get("continue-on-error") is True and any(TEST_WORDS.search(c) for c in cmds):
                out.append(F(rel, "ci-ignored-failure", "high", f"{sctx}`continue-on-error: true` on a test step: failures will not fail the job", True, _line_of(text, "continue-on-error")))
    for start in deps:
        seen, stack = set(), list(deps[start])
        while stack:
            n = stack.pop()
            if n == start:
                out.append(F(rel, "ci-needs-cycle", "high", f"job '{start}' depends on itself through `needs`", True, _line_of(text, "needs")))
                break
            if n in seen or n not in deps:
                continue
            seen.add(n)
            stack += deps[n]
    allrun = "\n".join(str(s.get("run")) for j in jobs.values() if isinstance(j, dict) for s in (j.get("steps") or []) if isinstance(s, dict) and "run" in s)
    if _has_tests(root) and not TEST_WORDS.search(allrun) and os.path.basename(rel) in ("ci.yml", "ci.yaml", "test.yml", "tests.yml", "build.yml"):
        out.append(F(rel, "ci-verifies-nothing", "medium", "the project has tests but no step in this workflow runs them", False))
    if "concurrency" not in doc and trig and "pull_request" in (trig if isinstance(trig, (dict, list)) else [trig]):
        out.append(F(rel, "ci-no-concurrency", "low", "no `concurrency:`: every push to a PR starts a new run without cancelling the old one", False))
    return out


def _check_uses(root, rel, text, st, sctx):
    out = []
    u = str(st["uses"]).strip()
    ln = _line_of(text, u)
    if u.startswith("./"):
        if not os.path.exists(os.path.join(root, u[2:])):
            out.append(F(rel, "ci-missing-local-action", "high", f"{sctx}local action '{u}' does not exist", True, ln))
        return out
    if u.startswith("docker://"):
        return out
    if "@" not in u:
        out.append(F(rel, "ci-unpinned-action", "high", f"{sctx}'{u}' has no version (`@v4` or a commit SHA)", True, ln))
        return out
    name, ref = u.rsplit("@", 1)
    if ref in ("main", "master", "HEAD", "latest", "dev", "develop"):
        out.append(F(rel, "ci-floating-action", "high", f"{sctx}'{u}' follows a moving branch: the pipeline can change without a commit", True, ln))
    elif not name.startswith(("actions/", "github/")) and not _is_pinned_sha(ref):
        out.append(F(rel, "ci-action-not-sha", "low", f"{sctx}third-party action '{name}' is pinned to a tag ('{ref}'), not a commit SHA: a moved tag changes what runs", False, ln))
    w = st.get("with") or {}
    if name == "actions/setup-python" and str(w.get("cache", "")).lower() in ("pip", "pipenv", "poetry"):
        dep = w.get("cache-dependency-path")
        if not dep and not any(os.path.isfile(os.path.join(root, f)) for f in ("requirements.txt", "pyproject.toml", "Pipfile.lock", "poetry.lock", "setup.py")):
            out.append(F(rel, "ci-cache-no-deps", "high", f"{sctx}`cache: {w.get('cache')}` but no dependency file exists: setup-python fails with 'No file matched'", True, ln))
    if name == "actions/setup-node" and str(w.get("cache", "")).lower() in ("npm", "yarn", "pnpm"):
        locks = {"npm": "package-lock.json", "yarn": "yarn.lock", "pnpm": "pnpm-lock.yaml"}
        if not w.get("cache-dependency-path") and not os.path.isfile(os.path.join(root, locks[str(w["cache"]).lower()])):
            out.append(F(rel, "ci-cache-no-deps", "high", f"{sctx}`cache: {w['cache']}` but {locks[str(w['cache']).lower()]} is missing", True, ln))
    return out


GL_RESERVED = {"stages", "variables", "default", "include", "workflow", "image", "services", "cache", "before_script", "after_script"}


def check_gitlab(root, rel, text, doc=None):
    out = []
    if doc is None:
        try:
            doc = load_yaml(text)
        except YamlError as e:
            return [F(rel, "ci-yaml", "high", f"does not parse as YAML: {e}", True)]
    if not isinstance(doc, dict):
        return [F(rel, "ci-structure", "high", "not a YAML mapping", True)]
    stages = doc.get("stages") or ["build", "test", "deploy"]
    jobs = {k: v for k, v in doc.items() if k not in GL_RESERVED and not str(k).startswith(".") and isinstance(v, dict)}
    if not jobs:
        return [F(rel, "ci-no-jobs", "high", "no jobs defined", True)]
    for jn, j in jobs.items():
        ctx = f"job '{jn}': "
        if not (j.get("script") or j.get("trigger") or j.get("extends")):
            out.append(F(rel, "ci-no-script", "high", f"{ctx}no `script`", True, _line_of(text, f"{jn}:")))
        st = j.get("stage", "test")
        if st not in stages:
            out.append(F(rel, "ci-bad-stage", "high", f"{ctx}stage '{st}' is not in `stages`", True, _line_of(text, f"{jn}:")))
        for n in (j.get("needs") or []):
            nm = n.get("job") if isinstance(n, dict) else n
            if nm not in jobs:
                out.append(F(rel, "ci-bad-needs", "high", f"{ctx}needs '{nm}', which is not a job", True, _line_of(text, "needs")))
        img = j.get("image") or doc.get("image")
        img = img.get("name") if isinstance(img, dict) else img
        if isinstance(img, str) and (img.endswith(":latest") or ":" not in img.split("/")[-1]):
            out.append(F(rel, "ci-image-unpinned", "medium", f"{ctx}image '{img}' has no fixed tag", False, _line_of(text, img)))
        scripts = []
        for k in ("before_script", "script", "after_script"):
            v = j.get(k) or []
            scripts += [str(x) for x in ([v] if isinstance(v, str) else v)]
        cmds = [c for s in scripts for c in command_lines(s)]
        out += check_commands(root, rel, text, cmds, None, ctx)
        if j.get("allow_failure") is True and any(TEST_WORDS.search(c) for c in cmds):
            out.append(F(rel, "ci-ignored-failure", "high", f"{ctx}`allow_failure: true` on a test job", True, _line_of(text, "allow_failure")))
    return out


def check_all(root, only=None):
    """-> {"pipelines": [...], "findings": [...], "parser": "pyyaml"|"built-in"}. Never calls a model."""
    root = os.path.abspath(root)
    pipes = [p for p in find_pipelines(root) if not only or p[1] in only]
    findings = []
    for kind, rel in pipes:
        try:
            with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            findings.append(F(rel, "ci-unreadable", "high", str(e), True))
            continue
        findings += (check_github if kind == "github" else check_gitlab)(root, rel, text)
    if os.path.isfile(os.path.join(root, "Jenkinsfile")):
        findings.append(F("Jenkinsfile", "ci-not-checked", "low", "a Jenkinsfile is present but Groovy pipelines are not parsed here", False))
    sev = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda x: (not x["gate"], sev.get(x["severity"], 3), x["file"], x["line"]))
    return {"pipelines": [r for _, r in pipes], "findings": findings, "parser": "pyyaml" if _pyyaml else "built-in"}


def summarize(res):
    g = [x for x in res["findings"] if x["gate"]]
    return {"pipelines": len(res["pipelines"]), "blocking": len(g), "advisory": len(res["findings"]) - len(g), "ok": bool(res["pipelines"]) and not g}


# --------------------------------------------------------------------------------------------- baseline
def detect_project(root):
    """-> facts used to write a baseline pipeline. Only what is actually in the repository."""
    has = lambda *n: any(os.path.isfile(os.path.join(root, x)) for x in n)
    py = has("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile") or any(f.endswith(".py") for f in os.listdir(root))
    node = has("package.json")
    scripts = _pkg_scripts(root) or {}
    return {"python": py, "node": node, "npm_scripts": scripts, "requirements": has("requirements.txt"), "pyproject": has("pyproject.toml"),
            "setup_py": has("setup.py"), "tests": _has_tests(root), "lock": has("package-lock.json"),
            "dev_requirements": has("requirements-dev.txt")}


def baseline(root, release=False):
    """-> {path: text}. A minimal, correct GitHub Actions workflow for what the repository contains; {} when the project type is not recognised
    (nothing is invented). Every action is a first-party one, pinned to a major version; permissions are read-only; jobs have timeouts."""
    d = detect_project(root)
    head = ("name: CI\n\non:\n  push:\n    branches: [main, master]\n  pull_request:\n\npermissions:\n  contents: read\n\n"
            "concurrency:\n  group: ci-${{ github.workflow }}-${{ github.ref }}\n  cancel-in-progress: true\n\njobs:\n")
    files = {}
    if d["python"] and not d["node"]:
        cache = d["requirements"] or d["pyproject"]
        body = ("  test:\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    strategy:\n      matrix:\n        python-version: [\"3.10\", \"3.12\"]\n    steps:\n"
                "      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: ${{ matrix.python-version }}\n")
        if cache:
            body += "          cache: pip\n"
        if d["requirements"]:
            body += "      - run: python -m pip install -r requirements.txt\n"
        elif d["pyproject"] or d["setup_py"]:
            body += "      - run: python -m pip install -e .\n"
        body += ("      - run: python -m pytest -q\n" if d["tests"] else "      - run: python -m compileall -q .\n")
        files[".github/workflows/ci.yml"] = head + body
    elif d["node"]:
        sc = d["npm_scripts"]
        body = ("  test:\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-node@v4\n"
                "        with:\n          node-version: 20\n" + ("          cache: npm\n" if d["lock"] else "") +
                ("      - run: npm ci\n" if d["lock"] else "      - run: npm install\n"))
        for name in ("lint", "build", "test"):
            if name in sc:
                body += f"      - run: npm run {name}\n" if name != "test" else "      - run: npm test\n"
        files[".github/workflows/ci.yml"] = head + body
    if files and release:
        art = "dist" if d["python"] and not d["node"] else "."
        steps = ("      - uses: actions/checkout@v4\n" + ("      - uses: actions/setup-python@v5\n        with:\n          python-version: \"3.12\"\n      - run: python -m pip install build\n      - run: python -m build\n"
                                                      if d["python"] and not d["node"] else "      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n      - run: npm pack\n") +
                 f"      - uses: actions/upload-artifact@v4\n        with:\n          name: release\n          path: {'dist/*' if art == 'dist' else '*.tgz'}\n")
        files[".github/workflows/release.yml"] = ("name: Release\n\non:\n  push:\n    tags: [\"v*\"]\n\npermissions:\n  contents: read\n\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                                                  "    timeout-minutes: 15\n    steps:\n" + steps)
    return files


# --------------------------------------------------------------------------------------------- local dry run
SAFE = [r"(?:python3?|py)\s+-m\s+(?:pytest|unittest|compileall|ruff|flake8|mypy|pylint|black\s+--check|isort\s+--check\S*)\b", r"pytest\b", r"ruff\s+(?:check|format\s+--check)\b",
        r"flake8\b", r"mypy\b", r"npm\s+(?:test|t)\b", r"npm\s+run\s+(?:test|lint|build|typecheck|check)\b", r"yarn\s+(?:test|lint|build)\b", r"pnpm\s+(?:test|lint|build)\b",
        r"go\s+(?:test|vet|build)\b", r"cargo\s+(?:test|check|clippy|build)\b", r"make\s+(?:test|lint|check|build)\b"]
INSTALL = re.compile(r"^(?:pip3?|python3? -m pip|npm (?:ci|install|i)|yarn(?: install)?|pnpm install|apt(?:-get)? |brew |go mod download|cargo fetch|choco )\b")
META = re.compile(r"[|;<>`]|\$\(|\|\||\$\{\{")


def _runnable(cmd):
    c = cmd.strip()
    if META.search(c):
        return False, "uses shell features or a workflow expression: not run locally"
    if INSTALL.match(c):
        return False, "installs packages (needs the network): not run locally"
    if any(re.match(p, c) for p in SAFE):
        return True, ""
    return False, "not on the safe list (tests, linters, builds): not run locally"


def pipeline_commands(root, rel):
    """-> [(job, step_label, workdir, command)] for the pipeline's run/script lines, in order."""
    with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
        doc = load_yaml(f.read())
    out = []
    if rel.endswith(".gitlab-ci.yml"):
        for jn, j in doc.items():
            if jn in GL_RESERVED or str(jn).startswith(".") or not isinstance(j, dict):
                continue
            for k in ("before_script", "script"):
                v = j.get(k) or []
                for s in ([v] if isinstance(v, str) else v):
                    out += [(jn, k, None, c) for c in command_lines(s)]
        return out
    for jn, j in (doc.get("jobs") or {}).items():
        if not isinstance(j, dict):
            continue
        wd = ((j.get("defaults") or {}).get("run") or {}).get("working-directory")
        for i, st in enumerate(j.get("steps") or [], 1):
            if isinstance(st, dict) and "run" in st:
                out += [(jn, st.get("name") or f"step {i}", st.get("working-directory") or wd, c) for c in command_lines(st["run"])]
    return out


def run_local(root, rel, say=print, timeout=300):
    """Run the pipeline's SAFE commands here, offline, in order. -> [{job, step, cmd, status: pass|fail|skipped, why, tail}].
    Commands that install packages, use shell features or are not tests/linters/builds are listed as skipped, never run."""
    root = os.path.abspath(root)
    results = []
    for job, step, wd, cmd in pipeline_commands(root, rel):
        for part in re.split(r"\s*&&\s*", cmd):
            part = part.strip()
            ok, why = _runnable(part)
            rec = {"job": job, "step": step, "cmd": part, "status": "skipped", "why": why, "tail": ""}
            if ok:
                cwd = os.path.abspath(os.path.join(root, wd)) if wd else root
                if not (cwd == root or cwd.startswith(root + os.sep)) or not os.path.isdir(cwd):
                    rec["why"] = "working directory is outside the project or missing"
                else:
                    argv = shlex.split(part, posix=(os.name != "nt"))
                    if argv[0] in ("python", "python3", "py"):
                        argv[0] = sys.executable
                    try:
                        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=core.guard_env(offline=True))
                        rec.update(status="pass" if p.returncode == 0 else "fail", why="", tail=(p.stdout + p.stderr)[-600:])
                    except FileNotFoundError:
                        rec["why"] = f"'{argv[0]}' is not installed on this machine"
                    except subprocess.TimeoutExpired:
                        rec.update(status="fail", why=f"still running after {timeout}s", tail="")
            results.append(rec)
            say(f"  {rec['status'].upper():7} {job}: {part[:70]}" + (f"   ({rec['why']})" if rec["why"] else ""))
            if rec["status"] == "fail":
                break
    return results


def local_summary(results):
    n = {k: sum(1 for r in results if r["status"] == k) for k in ("pass", "fail", "skipped")}
    return dict(n, ok=n["fail"] == 0 and n["pass"] > 0)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="architect.py ci", description="Verify the CI/CD pipeline files in a project (local, no model).")
    ap.add_argument("--root", default=".")
    ap.add_argument("--run-local", action="store_true", help="also run the pipeline's safe commands here, offline")
    ap.add_argument("--init", action="store_true", help="write a baseline workflow if the project has none")
    ap.add_argument("--release", action="store_true", help="with --init: also write a tag-triggered build-and-upload workflow")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    if a.init:
        if find_pipelines(root):
            print("This project already has a pipeline; nothing was written.")
        else:
            files = baseline(root, a.release)
            if not files:
                print("No Python or Node project was recognised, so nothing was written (a guess would be wrong).")
                return 1
            for rel, txt in files.items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
                    f.write(txt)
                print(f"wrote {rel}")
    res = check_all(root)
    loc = []
    if a.run_local:
        for rel in res["pipelines"]:
            loc += run_local(root, rel, say=(lambda m: None) if a.json else print)
    out = {"summary": summarize(res), "findings": res["findings"], "parser": res["parser"], "local": loc, "local_summary": local_summary(loc) if loc else None}
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        if not res["pipelines"]:
            print("No pipeline files found (.github/workflows/*.yml, .gitlab-ci.yml). `ci --init` writes a baseline.")
        for x in res["findings"]:
            print(f"  {'BLOCK' if x['gate'] else 'note '} {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}")
        s = out["summary"]
        print(f"\n{s['pipelines']} pipeline file(s); {s['blocking']} blocking, {s['advisory']} advisory finding(s)  (YAML parser: {res['parser']})")
        if loc:
            ls = out["local_summary"]
            print(f"local run: {ls['pass']} passed, {ls['fail']} failed, {ls['skipped']} skipped (installs and non-test commands are never run)")
    bad = out["summary"]["blocking"] > 0 or (out["local_summary"] or {}).get("fail", 0) > 0
    return 1 if bad or (not res["pipelines"] and not a.init) else 0


if __name__ == "__main__":
    sys.exit(main())
