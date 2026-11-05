"""Architect core: configuration, model calls, safe file edits, test running, tracing, undo.
Standard library only. Talks to Ollama over plain HTTP on localhost.
"""
import ast
import collections
import difflib
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

ASKS_RX = re.compile(r"\?\s*$|\b(would you like|do you want me|shall i|should i proceed|please confirm|let me know (?:if|whether|how))\b", re.I)

import arch_roles
import arch_schemas
import arch_tune

HERE = os.path.dirname(os.path.abspath(__file__))
# HOME is where this install keeps ITS OWN data (global config, measured speeds, scoreboard results, tune profile,
# self-improvement backups). It defaults to the install folder, so a standalone copy behaves exactly as before; a host
# app that vendors the Architect sets ARCHITECT_HOME (or calls set_home) to keep that data in its own data folder.
HOME = os.path.abspath(os.environ.get("ARCHITECT_HOME") or HERE)
CONFIG_FILE = os.path.join(HOME, "architect_config.json")


def data_dir():
    """Folder for measured speeds, scoreboard results, the tune profile. ARCHITECT_DATA_DIR wins (tests use it)."""
    return os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(HOME, "hub_data")


def set_home(path):
    """Relocate this install's own data (config + data dir) at runtime, e.g. from an embedding app. Returns the new HOME."""
    global HOME, CONFIG_FILE
    HOME = os.path.abspath(path)
    os.makedirs(HOME, exist_ok=True)
    os.environ["ARCHITECT_HOME"] = HOME
    CONFIG_FILE = os.path.join(HOME, "architect_config.json")
    mh = sys.modules.get("model_hub")
    if mh is not None:                                   # the hub keeps its own copies of these paths
        mh.CONFIG_FILE = CONFIG_FILE
        mh.DATA_DIR = data_dir()
        mh.RESULTS_FILE = os.path.join(mh.DATA_DIR, "results.json")
        mh.NOTES_FILE = os.path.join(HOME, "NOTES.md")
    return HOME

DEFAULTS = {
    "default_model": "",
    "ollama_host": "",             # blank = the OLLAMA_HOST environment variable, else 127.0.0.1:11434. A remote GPU box works too.
    "roles": {r: "" for r in arch_roles.ROLES},
    "hosts": {},                   # name -> "http://addr:11434": other machines of yours running Ollama (see arch_hosts.py)
    "role_hosts": {},              # role -> host name: that role's model calls go to that machine
    "role_overrides": {},          # role -> {think, sampling, num_predict}
    "model_overrides": {},         # model -> {num_ctx, sampling, think, system_prompt: "inline"|"system"}
    "limits": {"cloud_usd_per_run": 0.5, "cloud_usd_per_day": 5.0, "num_ctx": 8192, "max_calls": 60, "max_revisions": 3, "request_timeout": 900, "escalate_max_s": 240,
               "max_files_per_step": 6, "context_chars": 20000, "keep_alive": "10m", "max_file_bytes": 400000,
               "test_timeout": 300, "max_sprints": 5,
               "timeout_mode": "auto",      # auto = first-token/idle/total limits derived from each model's measured speed (request_timeout is the ceiling); fixed = request_timeout for everything
               "ctx_autogrow": True,        # if a prompt hit the context window (silently truncated by Ollama), retry once with a doubled window
               "num_ctx_max": 32768},       # ceiling for that growth
    "storage": {"archive": "",                         # a second drive for copies of run records (arch_archive.py); "" = off
                "slot_cache_gb": 4.0,                  # saved prompt memory (server/slots, ~0.2-0.4 GB a project): least recently used beyond this is dropped at each save (arch_cachehygiene.py)
                "slot_max_age_days": 30},              # a saved prompt memory unused this long is offered for removal by `architect.py caches` (asked, never automatic)
    "behavior": {"model_defaults": "off", "telemetry": "on", "telemetry_mb": 20, "rank_cache": "on", "review_mode": "ask", "offline_tests": True, "best_of_n": 1, "evaluator": "final",   # final | each | off
                 "playwright": "auto", "seed": None,
                 "mutation": "on", "mutation_max": 8,          # test flow: how many code mutations to try
                 "min_gain_pct": 10, "bench_runs": 5,          # optimize flow: required speed-up, benchmark repetitions
                 "hunt_max_files": 4, "hunt_findings": 5,      # hunt flow
                 "network": "offline", "net_allow": [], "net_block_tools": True,   # policy for code run by the Architect: offline | practice | open (default: offline -- nothing the harness runs leaves this machine unless you say so; the binary block below is independent of this)
                 "thinking": "roles", "sampling": "roles", "chat_model": "worker", "stall_samples": 3, "ground_edits": "on", "tests_first": "off", "test_first": "off", "tests_on_ecores": "auto", "chores_on_ecores": "auto", "close_on_exit": "auto", "reply_budget": "auto", "retry_stop": "auto", "broker": "suggest", "sandbox_mem_mb": 4096,     # roles = as each role defines; plan_debug_only / model_card: measured A/B options
                 "edit_style": "patch",                        # patch = EDIT blocks (search/replace); whole = write complete files under ~200 lines (small models); auto = whole under ~80 lines
                 "max_shrink_pct": 60,                         # refuse a reply that would cut an existing file (25+ lines) by more than this percent; 0 = off
                 "swap_aware": "on", "fast_tests": "on",      # fast_tests: in fix loops run the tests related to the changed files first, then the whole suite
                           # on = on a small GPU, order report-only steps (the per-sprint judge) so a different judge model loads once, not twice per sprint
                 "escalate": "stalled", "battery_saver": "auto", "routing": "keywords", "small_helpers": "on", "exclusive_gpu": "auto", "learn_from_runs": "on", "self_protect": "none",                          # off | stalled: a stalled fix loop hands the step to a LARGER model you already have installed (never a remote one)
                 "fake_code": "fail",                          # off | warn | fail: stubs, assertion-free tests, placeholder data in files a step changed (arch_fakecode.py)
                 "git": "off",                                 # off | commit (commit = checkpoint each successful run in git)
                 "fold_failed_sprints": True,                  # unattended builds: a sprint that will not pass folds into the next one instead of ending the build before the rest of the request is attempted
                 "merge_small_plans": True,                    # a one-module request runs as one sprint: no hand-off where a small model can drop finished work
                 "keep_best_try": True,                        # a try that is clearly worse (broke the code, lost a function, fewer tests pass) is undone before the next one
                 "record_live": "on",                          # on = while the hub runs, a 5-second sample of RAM, graphics memory, GPU temperature/power and power source goes to hub_data/live.log
                 "heavy_jobs": "one",                          # one = test suites, benchmarks and tuning run one at a time, below normal priority (arch_heavy.py); any = no lock
                 "postmortem": "on",                           # on = after a failed run the model explains the failure in plain words (postmortem.md), and a retry reads it; off = never
                 "recheck_before_retry": True,                 # a failed check is run once more, unchanged, before another model call: a flaky check makes the step "not sure" instead of a coin toss
                 "config_strict": "medium",
                 "stable_context": True},                      # True = within a run the project context of one step keeps its first order, cards and memory lines (prompt-cache reuse); False = re-ranked per call                   # config files: findings at or above this severity fail the check (high|medium|low)
    "tests": {"command": ""},
    "tiers": {"router": "", "worker": "", "heavy": "", "draft": "", "cloud": ""},   # model TIERS: any installed model (Ollama tag or .gguf path) per slot; roles name a tier (arch_roles.TIER_OF); empty = the hardware profile's default
    "server": {"kind": "auto", "binary": "", "port": 8081, "kv": "q8_0", "flash_attn": True, "draft": "auto", "parallel": 1, "ubatch": 256, "ctx": 0, "slots": True, "ngram": "auto", "cache_ram": 0, "make_room": "auto", "heavy_beside": "auto", "think_budget": 800, "fast_lane": "off", "fast_think_budget": 256, "idle_free_min": 15, "ollama_gpu": "auto", "ollama_keep_alive": "60s", "slot_persist": "auto"},   # the resident llama-server (arch_server.py); auto = use it when the binary is installed, else Ollama. ctx 0 = the hardware profile's measured window.
    "flows": {"engine": "legacy", "budget_scale": 1.0},                 # legacy = the Python flow classes, YAML only fills ids no class provides; data = flows/*.yaml win; off = YAML ignored
    "cache": {"responses": True, "max_rows": 4000, "max_days": 30},   # arch_cache.py: remember temperature-0 replies so a retry or a repeated task costs nothing. Never used by `eval`, never at temperature > 0.
    "supervisor": {"daily_runs": 40, "daily_minutes": 240, "task_minutes": 15, "max_attempts": 3},   # arch_supervisor.py: the queue the machine works through on its own, and the ceilings that stop it running away
    "hooks": {"enabled": True, "trust_project": False},   # arch_hooks.py: your own commands at fixed points in a run. A project's hooks arrive with its code, so they need approving once (`hooks --trust`) before they run.
    "skills": {"enabled": True, "max": 2, "budget": 4000, "min_score": 1.5},
    "memory": {"prompt_chars": 1800, "top_lines": 12, "max_lines": 400, "prune_after": 5},   # arch_memory.py: what the app has learned about a project, and how much of it rides along in every prompt
    "eval": {"pass_rate_drop": 5.0, "tokens_p50_rise_pct": 25.0, "secs_p95_rise_pct": 40.0},   # arch_eval.py: what counts as a regression when one benchmark run is compared with another   # arch_skills.py: short Markdown notes on how this project does things, loaded only for the tasks they apply to
    "online": {"enabled": False, "allow_pip": False, "cloud_tier": "off", "cloud_model": "claude:claude-opus-5-5",
               "cloud_cache": True, "cloud_cache_ttl": "5m", "cloud_fast": False,      # arch_cloud.py: local cache of Claude replies (30 days); Anthropic prompt-cache lifetime 5m | 1h
               "ultra_worker": "claude:claude-sonnet-5-5", "ultra_skeptic": "claude:claude-haiku-5-5"},   # ultracode helpers; the lead stays on the chosen model   # the ONE switch for anything that leaves this machine on the harness's own behalf (catalog verify, Hugging Face facts, pip repairs). Off = the app behaves identically with no network at all.
    "retrieval": {"mode": "hybrid", "chunk_lines": 60, "max_chunks": 20000,   # arch_index.py: SQLite FTS5 over functions + TF-IDF files, fused
                  "embed_model": "", "embed_host": ""},                        # arch_embed.py: optional dense ranking, "" = keyword-only (default)
    "plugins": {"enabled": True, "trust_project": False, "disabled": []},      # arch_plugins.py
    "squad": {"replay": True, "step_cache_days": 30},                            # arch_squad.py: crew --resume replay, agent step cache
    "files": {"extensions": [".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".sql", ".md", ".txt",
                             ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".go", ".rs", ".java", ".cs", ".rb", ".php"],
              "ignored_dirs": [], "protected_dirs": []},
}
BUILTIN_IGNORED = {".git", ".arch", ".arch_self", "._guard", "node_modules", "venv", ".venv", "__pycache__", ".pytest_cache", "dist", "build", ".mypy_cache"}
PROTECTED_FILES = {"architect_config.json"}


def deep_merge(base, over):
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
            return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(root=None):
    """defaults < architect_config.json (global) < <root>/.arch/config.json (per project)."""
    cfg = deep_merge(DEFAULTS, load_json(CONFIG_FILE))
    if root:
        cfg = deep_merge(cfg, load_json(os.path.join(root, ".arch", "config.json")))
    return cfg


# ----------------------------------------------------------------------------------------------
# Ollama
# ----------------------------------------------------------------------------------------------
def ollama_host():
    h = (os.environ.get("OLLAMA_HOST") or load_json(CONFIG_FILE).get("ollama_host") or "127.0.0.1:11434").strip()
    if not h.startswith("http"):
        h = "http://" + h
    return h.replace("0.0.0.0", "127.0.0.1").rstrip("/")


_OLLAMA_LOCK = threading.Lock()
_OLLAMA_USERS = {}                 # model -> runs in this process that loaded it through Ollama and have not finished


class LLMError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class LoopDetected(Exception):
    pass


def list_models():
    """Installed models: Ollama's tags plus whatever the resident llama-server is serving right now (a tier alias answers to its name)."""
    out = []
    try:
        with urllib.request.urlopen(ollama_host() + "/api/tags", timeout=10) as r:
            out = [m["name"] for m in json.loads(r.read().decode()).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        out = []
    try:
        import arch_server
        out += [m for m in arch_server.models() if m not in out]
    except Exception:
        pass
    return out


def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def ollama_keep_alive(cfg=None):
    """How long Ollama keeps a model in memory after a call (server.ollama_keep_alive, default 60s): one value for every chat call,
    the ~20 GB off-card MoE included, so nothing sits in RAM for half an hour after a run."""
    try:
        v = ((cfg or load_config()).get("server") or {}).get("ollama_keep_alive")
    except Exception:
        v = None
    return v if v not in (None, "") else "60s"


def stream_chat(model, messages, options=None, think=None, keep_alive=None, timeout=900, fmt=None, on_token=None, host=None):
    """One model call -> {"text","thinking","tok_s","gen_tokens","prompt_tokens","cached_tokens","done_reason","prefill_tok_s","load_s","transport"}.
    The server that answers (Ollama, or the resident llama-server for a model it serves) is chosen by arch_transport.route; `timeout` is one
    number or {"first", "idle", "total"} seconds (arch_tune.timeouts_for)."""
    import arch_transport
    return arch_transport.chat(model, messages, options, think, keep_alive, timeout, fmt, on_token, host)


# ----------------------------------------------------------------------------------------------
# Safe files
# ----------------------------------------------------------------------------------------------
def safe_path(root, rel, cfg):
    """-> absolute path inside root, or raises ValueError with a plain-language reason."""
    rel = (rel or "").strip().strip("`'\"").replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or "\0" in rel:
        raise ValueError(f"'{rel}': give a path relative to the project folder")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"'{rel}': '..' is not allowed")
    protected = BUILTIN_IGNORED | set(cfg["files"].get("protected_dirs") or [])
    if any(p in protected for p in parts[:-1]) or parts[-1] in (".git", ".arch"):
        raise ValueError(f"'{rel}': that folder is protected")
    if parts[-1] in PROTECTED_FILES or parts[-1].startswith("architect_config"):
        raise ValueError(f"'{rel}': the Architect's own settings are protected")
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext not in set(cfg["files"]["extensions"]) and parts[-1] not in ("Dockerfile", "Makefile", ".gitignore", ".dockerignore", ".editorconfig", ".env.example"):
        raise ValueError(f"'{rel}': file type {ext or '(none)'} is not enabled (Settings > Files)")
    full = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath([os.path.abspath(root), full]) != os.path.abspath(root):
        raise ValueError(f"'{rel}': outside the project")
    if os.path.islink(full):
        raise ValueError(f"'{rel}': symbolic links are not edited")
    return full


# A single project file is read many times per flow run (once by rank_files' TF-IDF pass over EVERY file,
# again by context() for files shown in full, again by outline() for files that are not, again by each code
# check) -- with a different byte `limit` at nearly every call site. This cache holds the full, unlimited raw
# bytes of each path, keyed by (mtime, size) so it is invalidated the instant the file changes on disk (by the
# Architect's own edits, or by anything else), and read_text() slices the limit from that cached copy instead
# of hitting the disk again -- same bytes, same truncation point, same CRLF detection as before; only where
# they come from changes. Bounded so a long-lived hub process holding many large projects open over time
# cannot grow without limit.
_READ_CACHE = {}
_READ_CACHE_MAX = 4000


def read_text(path, limit=None):
    st = os.stat(path)  # raises OSError exactly where open() would have, same as before
    key = os.path.abspath(path)
    hit = _READ_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        raw_full = hit[2]
    else:
        with open(path, "rb") as f:
            raw_full = f.read()
        if len(_READ_CACHE) >= _READ_CACHE_MAX:
            _READ_CACHE.pop(next(iter(_READ_CACHE)))  # evict oldest (insertion-ordered dict)
        _READ_CACHE[key] = (st.st_mtime_ns, st.st_size, raw_full)
    raw = raw_full[:limit] if limit else raw_full
    crlf = b"\r\n" in raw
    return raw.decode("utf-8", "replace").replace("\r\n", "\n"), crlf


def write_text(path, text, crlf=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    _READ_CACHE.pop(os.path.abspath(path), None)  # never rely on mtime resolution alone to see our own write


def list_files(root, cfg, exts=None):
    ignored = BUILTIN_IGNORED | set(cfg["files"].get("ignored_dirs") or [])
    allowed = set(exts or cfg["files"]["extensions"])
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in ignored and not x.startswith("._"))
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in allowed or f in ("Dockerfile", "Makefile"):
                p = os.path.join(d, f)
                try:
                    if os.path.getsize(p) <= cfg["limits"]["max_file_bytes"]:
                        out.append(os.path.relpath(p, root).replace(os.sep, "/"))
                except OSError:
                    pass
    return out


_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _terms(text):
    out = []
    for w in _TOK.findall(text):
        out.append(w.lower())
        out.extend(p.lower() for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", w) if len(p) > 2 and p.lower() != w.lower())
    return out


def _tel_mirror(rec, run_id):
    """Mirror one trace record into the unified telemetry stream (arch_telemetry). Never raises."""
    try:
        import arch_telemetry
        r = dict(rec)
        kind = r.pop("type", "event")
        r.pop("t", None)
        for k in ("kind", "sid", "parent", "run"):
            if k in r:
                r["rec_" + k] = r.pop(k)
        if kind == "call":
            r["cache"] = {"state": "hit" if r.get("cached") else "miss"}
        arch_telemetry.emit(kind, run=run_id, **r)
    except Exception:
        pass


def _tel_swallow(where, ex):
    try:
        import arch_telemetry
        arch_telemetry.swallow(where, ex)
    except Exception:
        pass


_TERMS = {}          # abspath -> (mtime_ns, size, Counter): rank_files term memo
_TERMS_CAP = 6000
_TERMS_STATS = {"hit": 0, "miss": 0}


def _file_terms(root, f, cfg=None):
    ap = os.path.join(root, f)
    st = os.stat(ap)
    key = (st.st_mtime_ns, st.st_size)
    got = _TERMS.get(ap)
    if (((cfg or DEFAULTS).get("behavior") or {}).get("rank_cache", "on") != "off" and got and got[:2] == key):
        _TERMS_STATS["hit"] += 1
        return got[2]
    _TERMS_STATS["miss"] += 1
    t, _ = read_text(ap, 200000)
    c = collections.Counter(_terms(f.replace("/", " ") * 3 + " " + t))
    if len(_TERMS) >= _TERMS_CAP:
        for k in list(_TERMS)[: _TERMS_CAP // 4]:
            _TERMS.pop(k, None)
    _TERMS[ap] = (key[0], key[1], c)
    return c


def rank_files(root, cfg, query, files=None):
    """TF-IDF ranking of files against a query (stdlib). -> [(file, score)] best first."""
    files = files or list_files(root, cfg)
    docs, df = {}, collections.Counter()
    for f in files:
        try:
            c = _file_terms(root, f, cfg)
        except OSError:
            continue
        docs[f] = c
        df.update(c.keys())
    n = max(len(docs), 1)
    q = collections.Counter(_terms(query))
    scores = []
    for f, c in docs.items():
        tot = sum(c.values()) or 1
        s = sum((1 + math.log(c[w])) * math.log(1 + n / df[w]) * q[w] for w in q if w in c) / math.sqrt(tot)
        scores.append((f, s))
    try:
        import arch_telemetry
        arch_telemetry.emit("cache.rank_terms", hits=_TERMS_STATS["hit"], misses=_TERMS_STATS["miss"], files=len(docs))
    except Exception:
        pass
    return sorted(scores, key=lambda x: -x[1])


def outline(path, rel=None):
    """A compact view of a file: signatures only (Python AST; regexes for web/SQL; first lines otherwise)."""
    try:
        text, _ = read_text(path, 300000)
    except OSError:
        return ""
    ext = os.path.splitext(path)[1].lower()
    lines = []
    if ext == ".py":
        try:
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    lines.append(f"{node.lineno}: {'class' if isinstance(node, ast.ClassDef) else 'def'} {node.name}"
                                 + (f"({', '.join(a.arg for a in node.args.args)})" if hasattr(node, 'args') else ""))
        except SyntaxError:
            pass
    elif ext in (".js", ".mjs", ".ts", ".tsx", ".jsx"):
        lines = [f"{i + 1}: {m.strip()[:100]}" for i, m in enumerate(text.splitlines()) if re.match(r"\s*(export |async )?(function|class|const \w+ = \(|const \w+ = async)", m)]
    elif ext == ".sql":
        lines = [f"{i + 1}: {m.strip()[:100]}" for i, m in enumerate(text.splitlines()) if re.match(r"\s*(create|alter|drop|insert)\b", m, re.I)]
    elif ext in (".html", ".htm"):
        lines = [f"{i + 1}: {m.strip()[:100]}" for i, m in enumerate(text.splitlines()) if re.search(r"<(h[1-3]|form|section|nav|main|script|style|table)\b", m, re.I)]
    if not lines:
        lines = text.splitlines()[:25]
    return "\n".join(lines[:80])


def focus(path, query, max_chars=6000, boost=()):
    """The parts of a Python file that matter for this request: the top-level functions/classes whose text shares terms with the query, in full,
    with the rest as signatures. For a small context window this beats both "whole file" and "signatures only". -> text, or "" when nothing matches.
    `boost`: names the FTS5 index found relevant (arch_index); they are shown in full even when they share no term with the request."""
    try:
        text, _ = read_text(path, 300000)
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return ""
    q = set(_terms(query))
    boost = set(boost or ())
    if not q and not boost:
        return ""
    lines = text.splitlines()
    scored = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        body = "\n".join(lines[start:node.end_lineno])
        hit = len(q & set(_terms(node.name + " " + body)))
        if node.name in boost:
            hit += 3                                               # a semantic hit outranks a single shared word
        if hit and (node.name in boost or "_" in node.name or node.name.lower() in q or hit >= 2):
            scored.append((hit, start, body, node.name))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    out, used = [], 0
    for hit, start, body, name in scored:
        if used + len(body) > max_chars:
            continue
        out.append(f"# --- {name} (lines {start + 1}-{start + body.count(chr(10)) + 1}) ---\n{body}")
        used += len(body)
    if not out:
        return ""
    shown = {n for _, _, _, n in scored if any(n in o.split("\n", 1)[0] for o in out)}
    rest = [f"{n.lineno}: {'class' if isinstance(n, ast.ClassDef) else 'def'} {n.name}" for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name not in shown]
    return "\n\n".join(out) + ("\n\n# other definitions in this file (signatures only):\n" + "\n".join(rest[:40]) if rest else "")


# ----------------------------------------------------------------------------------------------
# Edit blocks
# ----------------------------------------------------------------------------------------------
HEADER_RE = re.compile(r"^[\s>*#`_-]*(FILE|EDIT|DEF)\s*:\s*[`*\"']*\s*(.+?)\s*[`*\"']*\s*$", re.I)
FENCE_RE = re.compile(r"^\s*```")


def _def_header(raw):
    """A DEF header as small models actually write it -> (path, target). Accepts `dates.py::days_in_month`, but also
    `dates.py::days_in_month(year, month)`, `dates.py: def days_in_month`, `dates.py#days_in_month`, `dates.py::days_in_month:` and a bare
    `dates.py` (then the target is taken from the block's one definition). Anything that is not `<file>.py` keeps the old split, so a
    non-Python path still gets the plain "Python files only" answer."""
    m = re.match(r"^(.+?\.py)[`'\"*]*\s*(?:::|:|#)?\s*(.*)$", raw.strip(), re.I)
    path, target = (m.group(1), m.group(2)) if m else (raw.partition("::")[0], raw.partition("::")[2])
    target = re.sub(r"^(?:async\s+)?(?:def|class)\s+", "", target.strip().strip("`'\"*"))
    target = re.split(r"[\s(:,]", target, 1)[0] if target else ""
    return path.strip().strip("`'\" "), target


def parse_blocks(text):
    """-> [{"kind":"file","path","code"} | {"kind":"edit","path","pairs":[(search, replace)]} | {"kind":"def","path","target","code"}].
    Tolerant of markdown noise; a file or def block must have a closed code fence (a cut-off reply is reported, not applied)."""
    text = strip_think(text).replace("\r\n", "\n")
    lines = text.split("\n")
    blocks, i = [], 0
    while i < len(lines):
        m = HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        kind, path = m.group(1).lower(), m.group(2)
        i += 1
        target = ""
        if kind == "def":                                    # DEF: path.py::name  or  DEF: path.py::Class.method
            path, target = _def_header(path)
        if kind in ("file", "def"):
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines) and FENCE_RE.match(lines[i]):
                i += 1
                body = []
                while i < len(lines) and not re.match(r"^```\s*$", lines[i]):
                    body.append(lines[i])
                    i += 1
                closed = i < len(lines)
                i += 1
                blk = {"kind": kind, "path": path, "code": "\n".join(body) + "\n", "truncated": not closed}
                if kind == "def":
                    blk["target"] = target
                blocks.append(blk)
        else:
            pairs, truncated = [], False
            while i < len(lines) and not HEADER_RE.match(lines[i]):
                if lines[i].startswith("<<<<<<<"):
                    i += 1
                    s, r = [], []
                    cur = s
                    while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                        if lines[i].startswith("=======") and cur is s:
                            cur = r
                        else:
                            cur.append(lines[i])
                        i += 1
                    if i >= len(lines):
                        truncated = True
                    pairs.append(("\n".join(s), "\n".join(r)))
                i += 1
            blocks.append({"kind": "edit", "path": path, "pairs": pairs, "truncated": truncated})
    if not blocks:
        blocks = _bare_path_blocks(lines)
    return blocks


_BARE_PATH = re.compile(r"^[\s>*#`_-]*((?:[\w.-]+/)*[\w.-]+\.[A-Za-z]{1,5})[`*:\s]*$")


def _bare_path_blocks(lines):
    """A reply with NO FILE:/EDIT:/DEF: header at all, but a line that is just a path right above a code block -- `tests/test_calc_extra.py`
    then ```python ... ```. Small models write exactly that, often (seen live: half of a 4B tester's replies), and the whole reply used
    to count as "nothing was written". Read as FILE blocks. Only ever a fallback: one real header anywhere and this never runs."""
    out, i = [], 0
    while i < len(lines):
        m = _BARE_PATH.match(lines[i])
        j = i + 1
        while m and j < len(lines) and not lines[j].strip():
            j += 1
        looks_like_file = m and ("/" in m.group(1) or re.search(r"\.(?:py|js|ts|html|css|json|ya?ml|toml|md|sql|sh|txt)$", m.group(1)))
        if looks_like_file and j < len(lines) and FENCE_RE.match(lines[j]):
            body, k = [], j + 1
            while k < len(lines) and not re.match(r"^```\s*$", lines[k]):
                body.append(lines[k])
                k += 1
            out.append({"kind": "file", "path": m.group(1), "code": "\n".join(body) + "\n", "truncated": k >= len(lines)})
            i = k + 1
            continue
        i += 1
    return out


def unified(old, new, rel):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))


def merge_partial(old, code):
    """A FILE block for an existing Python file that holds only definitions (and imports): the model sent the functions it changed, not
    the file. -> the old file with exactly those definitions replaced (or added), or None when the block is anything else. Seen live: a
    one-function FILE reply was refused as "would cut 30 to 8 lines" and the try was lost, although what it meant was unambiguous."""
    try:
        tree = ast.parse(code)
        old_tree = ast.parse(old)
    except SyntaxError:
        return None
    doc = lambda n: isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant) and isinstance(n.value.value, str)
    body = [n for n in tree.body if not doc(n)]
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    defs = [n for n in body if isinstance(n, kinds)]
    if not defs or any(not isinstance(n, kinds + (ast.Import, ast.ImportFrom)) for n in body):
        return None
    old_defs = {n.name: n for n in old_tree.body if isinstance(n, kinds)}
    if any(d.name not in old_defs for d in defs) or len({d.name for d in defs}) != len(defs):
        return None                       # a new or repeated name: where it belongs (and what uses it first) is not clear, so no guess
    stub = lambda n: isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and all(
        isinstance(x, ast.Pass) or (isinstance(x, ast.Expr) and isinstance(getattr(x, "value", None), ast.Constant) and x.value.value is Ellipsis)
        for x in n.body if not doc(x))
    if any(stub(d) and not stub(old_defs[d.name]) for d in defs):
        return None                       # a placeholder body over a real one is not an edit: refused, as before
    lines = code.splitlines()
    imports = "\n".join(ast.get_source_segment(code, n) or "" for n in body if isinstance(n, (ast.Import, ast.ImportFrom)))
    src = old
    for i, d in enumerate(defs):
        start = min([d.lineno] + [x.lineno for x in d.decorator_list])
        seg = "\n".join(lines[start - 1:d.end_lineno]) + "\n"
        src, _why = replace_def(src, d.name, (imports + "\n\n\n" + seg) if i == 0 and imports else seg)
        if src is None:
            return None                                         # e.g. a class sent without some of its methods: refused, as with DEF
    return src


def replace_def(src, target, code):
    """Replace (or add) ONE Python definition in `src`. -> (new_src, "") or (None, why).

    `target` is "name" (a top-level function or class) or "Class.method". The block holds that whole definition; every other line of the
    file is kept byte for byte. This is the edit a small model gets right: it writes one complete function -- which it does well -- instead
    of either a search text that must match exactly or a whole-file rewrite that quietly drops functions an earlier step built (seen live:
    days_in_month vanished from dates.py in a rewrite that only meant to fix day_of_year). Imports at the top of the block are moved to the
    file's imports. A target that is not there yet is added: a function before the `if __name__ == "__main__":` block, a method at the
    end of its class.

    Forgiving where a small model is predictably sloppy, strict where a mistake would lose code:
    - no target (`DEF: dates.py`): the block's one definition names it;
    - `::Class.method` sent wrapped in `class Class:` with just that method inside: unwrapped;
    - `::method` without its class, for a method that exists in exactly one class: treated as `Class.method`;
    - a class block that leaves out methods the class has: REFUSED (it would delete them), with the one-method form to use instead;
    - a function sent for a class (or the reverse): refused; a name defined twice: the LAST definition (the one Python uses) is replaced."""
    import ast
    import textwrap
    FN = (ast.FunctionDef, ast.AsyncFunctionDef)
    DEFS = FN + (ast.ClassDef,)
    target = (target or "").strip()
    if target and not re.match(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$", target):
        return None, f"DEF target '{target[:60]}' is not a name: write DEF: path.py::name or DEF: path.py::ClassName.method"
    body = textwrap.dedent(code).strip("\n")
    try:
        tree = ast.parse(body)
    except SyntaxError as ex:
        return None, f"the definition does not parse: {ex.msg} (line {ex.lineno})"

    def is_doc(n):
        return isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant)

    imps = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    imp_lines = [ast.get_source_segment(body, n) for n in imps]
    defs = [n for n in tree.body if isinstance(n, DEFS)]
    other = [n for n in tree.body if n not in defs and n not in imps and not is_doc(n)]
    cls, _, name = target.rpartition(".")
    if cls and len(defs) == 1 and isinstance(defs[0], ast.ClassDef) and defs[0].name == cls and not other:
        inner = [n for n in defs[0].body if isinstance(n, DEFS)]
        if len(inner) == 1 and inner[0].name == name and all(isinstance(n, DEFS) or is_doc(n) for n in defs[0].body):
            m = inner[0]                                         # `class Cart:` wrapped around the one method being changed
            blines = body.split("\n")
            body = textwrap.dedent("\n".join(blines[min([m.lineno] + [x.lineno for x in m.decorator_list]) - 1:m.end_lineno]))
            tree = ast.parse(body)
            defs = [n for n in tree.body if isinstance(n, DEFS)]
    if not target:
        if len(defs) != 1:
            return None, "a DEF block needs a name (DEF: path.py::function_name) and holds exactly one definition"
        name = defs[0].name
    if len(defs) != 1 or other or defs[0].name != name:
        return None, (f"a DEF block holds exactly one definition, named {name} (found: {', '.join(x.name for x in defs) or 'none'}"
                      + (", plus other statements" if other else "") + "). Send one DEF block per function, method or class.")
    d = defs[0]
    lines = body.split("\n")
    def_text = "\n".join(lines[min([d.lineno] + [x.lineno for x in d.decorator_list]) - 1:d.end_lineno])

    def indent(text, pad):
        return "\n".join((pad + x) if x.strip() else x for x in text.split("\n"))

    def kind(n):
        return "class" if isinstance(n, ast.ClassDef) else "function"

    if not src.strip():
        if cls:
            return None, f"class {cls} is not in the file yet; send DEF: path::{cls} with the whole class first"
        return "\n".join(imp_lines + ([""] if imp_lines else []) + [def_text]) + "\n", ""
    try:
        stree = ast.parse(src)
    except SyntaxError as ex:
        return None, f"the file does not parse now ({ex.msg}, line {ex.lineno}), so one definition cannot be swapped in; send the complete file with FILE:"
    slines = src.split("\n")
    owner, scope, pad = None, stree.body, ""
    if not cls and not any(isinstance(n, DEFS) and n.name == name for n in stree.body):
        a = d.args.args if isinstance(d, FN) else []
        decos = {getattr(x, "id", None) or getattr(x, "attr", None) for x in getattr(d, "decorator_list", [])}
        if isinstance(d, FN) and ((a and a[0].arg in ("self", "cls")) or decos & {"staticmethod", "classmethod", "property", "setter"}):
            owners = [n for n in stree.body if isinstance(n, ast.ClassDef) and any(isinstance(m, FN) and m.name == name for m in n.body)]
            if len(owners) > 1:
                return None, f"{name} is a method of {' and '.join(o.name for o in owners)}; name the class: DEF: path::{owners[0].name}.{name}"
            if not owners:
                return None, f"{name} looks like a method (its first argument is self); name its class: DEF: path::ClassName.{name}"
            cls = owners[0].name
    if cls:
        owner = next((n for n in reversed(stree.body) if isinstance(n, ast.ClassDef) and n.name == cls), None)
        if owner is None:
            return None, f"class {cls} is not in the file; send DEF: path::{cls} with the whole class"
        scope = owner.body
        pad = " " * (owner.body[0].col_offset if owner.body else owner.col_offset + 4)
    hits = [n for n in scope if isinstance(n, DEFS) and n.name == name]
    hit = hits[-1] if hits else None
    if hit is not None and kind(hit) != kind(d):
        return None, f"{name} is a {kind(hit)} in the file, and your block is a {kind(d)}; send a {kind(hit)}"
    if hit is not None and isinstance(hit, ast.ClassDef):
        gone = sorted({m.name for m in hit.body if isinstance(m, FN)} - {m.name for m in d.body if isinstance(m, FN)})
        if gone:
            return None, (f"your class {name} leaves out {', '.join(gone)}, which the file has; that would delete them. To change one method "
                          f"send DEF: path::{name}.<method>, or send the whole class with every method")
    if hit is not None:
        start = min([hit.lineno] + [x.lineno for x in hit.decorator_list]) - 1
        new_lines = slines[:start] + indent(def_text, pad).split("\n") + slines[hit.end_lineno:]
    elif cls:
        end = owner.end_lineno
        new_lines = slines[:end] + [""] + indent(def_text, pad).split("\n") + slines[end:]
    else:
        last_def = max([i for i, n in enumerate(stree.body) if isinstance(n, DEFS)], default=-1)
        tail = next((n for i, n in enumerate(stree.body) if i > last_def and (
            (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and getattr(n.test.left, "id", "") == "__name__")
            or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)))), None)
        if tail is None:
            new_lines = src.rstrip("\n").split("\n") + ["", ""] + def_text.split("\n") + [""]
        else:                                                # a script's main block runs at import: the new function must come before it
            at = tail.lineno - 1
            while at > 0 and slines[at - 1].lstrip().startswith("#"):
                at -= 1
            head = slines[:at]
            while head and not head[-1].strip():
                head.pop()
            new_lines = head + ["", ""] + def_text.split("\n") + ["", ""] + slines[at:]
    new = "\n".join(new_lines)
    if imp_lines:
        def keys(n):
            if isinstance(n, ast.Import):
                return {("import", a.name, a.asname) for a in n.names}
            return {("from", n.module, n.level, a.name, a.asname) for a in n.names}
        ntree = ast.parse(new)
        have = set()
        for n in ntree.body:                                 # imports at module level (also inside a top-level try/if), never inside a function
            for x in ([n] + ([y for y in ast.walk(n)] if isinstance(n, (ast.Try, ast.If)) else [])):
                if isinstance(x, (ast.Import, ast.ImportFrom)):
                    have |= keys(x)
        missing = [seg for n, seg in zip(imps, imp_lines) if not keys(n) <= have]
        if missing:
            top = [n for n in ntree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
            at = top[-1].end_lineno if top else (ntree.body[0].end_lineno if ntree.body and is_doc(ntree.body[0]) else 0)
            nl = new.split("\n")
            new = "\n".join(nl[:at] + missing + nl[at:])
    try:
        ast.parse(new)
    except SyntaxError as ex:
        return None, f"with this definition swapped in the file would not parse: {ex.msg} (line {ex.lineno})"
    if not new.endswith("\n"):
        new += "\n"
    return new, ""


def add_missing_stdlib_imports(src):
    """Add `import x` for standard-library modules a Python file uses (`re.sub(...)`) but never imports. Code fixes this, not a model:
    seen live (2026-11-04) a 4B and the 35B it escalated to both left `re` unimported through every retry of a passing design.
    Only names that are stdlib modules, used as `name.attr`, and not bound anywhere in the file; anything unparsable is left alone."""
    import ast
    import sys as _sys
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    std = set(getattr(_sys, "stdlib_module_names", ()))
    bound, used = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            for a in getattr(node, "args", None) and (node.args.args + node.args.kwonlyargs + node.args.posonlyargs) or []:
                bound.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and isinstance(node.value.ctx, ast.Load):
            used.add(node.value.id)
    missing = sorted(n for n in used - bound if n in std and not n.startswith("_"))
    if not missing:
        return src
    lines = src.split("\n")
    at = 0
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant) \
            and isinstance(tree.body[0].value.value, str):
        at = tree.body[0].end_lineno                             # after the module docstring
    while at < len(lines) and lines[at].startswith("from __future__"):
        at += 1
    return "\n".join(lines[:at] + [f"import {m}" for m in missing] + lines[at:])


def prepare_block(root, cfg, b):
    """-> dict(status ok|error|identical, rel, full, old, new, crlf, msg). Nothing is written here."""
    res = {"rel": b["path"], "status": "error", "msg": ""}
    if b.get("truncated"):
        res["msg"] = "reply was cut off in the middle of this block; send one smaller change"
        return res
    try:
        full = safe_path(root, b["path"], cfg)
    except ValueError as e:
        res["msg"] = str(e)
        return res
    res["rel"] = os.path.relpath(full, root).replace(os.sep, "/")
    res["full"] = full
    exists = os.path.isfile(full)
    old, crlf = ("", False)
    if exists:
        old, crlf = read_text(full)
    if b["kind"] == "file":
        new = b["code"]
        if not new.strip():
            res["msg"] = "empty file content"
            return res
    elif b["kind"] == "def":
        if not res["rel"].endswith(".py"):
            res["msg"] = "DEF: works for Python files only; use FILE: or EDIT: here"
            return res
        if not b["code"].strip():
            res["msg"] = "empty definition"
            return res
        new, why = replace_def(old.replace("\r\n", "\n") if crlf else old, b.get("target") or "", b["code"])
        if new is None:
            res["msg"] = why
            return res
    else:
        if not exists:
            res["msg"] = "EDIT needs an existing file; use FILE: to create new files"
            return res
        if not b["pairs"]:
            res["msg"] = "no SEARCH/REPLACE pair found"
            return res
        new = old
        for s, r in b["pairs"]:
            if not s.strip():
                res["msg"] = "empty SEARCH text"
                return res
            n = new.count(s)
            if n == 0:
                # tolerate trailing-whitespace differences
                norm = lambda t: "\n".join(x.rstrip() for x in t.split("\n"))
                if norm(s) and norm(new).count(norm(s)) == 1:
                    new = norm(new).replace(norm(s), r)
                    continue
                res["msg"] = "SEARCH text not found in the file (it must match existing lines exactly)"
                return res
            if n > 1:
                res["msg"] = "SEARCH text appears more than once; include more surrounding lines"
                return res
            i = new.index(s)
            lead = new[new.rfind("\n", 0, i) + 1:i]
            if lead and not lead.strip() and "\n" in r:
                # SEARCH matched part-way into a line: the model under-indented its block. The first REPLACE line lands after the existing
                # indent; shift the following lines by the same amount, or a 'with:' / 'if:' body ends up at the wrong depth.
                head, _, tail = r.partition("\n")
                r = head + "\n" + "\n".join((lead + x) if x.strip() else x for x in tail.split("\n"))
            new = new[:i] + r + new[i + len(s):]
    if new == old:
        res.update(status="identical", msg="already in place", old=old, new=new, crlf=crlf, existed=exists)
        return res
    if res["rel"].endswith(".py"):
        new = add_missing_stdlib_imports(new)
        if new == old:
            res.update(status="identical", msg="already in place", old=old, new=new, crlf=crlf, existed=exists)
            return res
    if res["rel"].endswith(".py") and cfg.get("behavior", {}).get("syntax_gate", "on") == "on":
        # Per-file syntax gate. Seen live (2026-11-04): one reply held a correct slugify.py AND a test file cut off mid-line; the
        # broken file made the whole attempt "worse" and the correct code was undone with it. Now only the broken file is refused,
        # with the parser's message, and every other block of the reply still applies. A file that already did not parse may still
        # be replaced (that is how it gets fixed).
        import ast as _ast
        try:
            _ast.parse(new)
        except SyntaxError as ex:
            was_ok = True
            if exists:
                try:
                    _ast.parse(old)
                except SyntaxError:
                    was_ok = False
            if was_ok:
                res["msg"] = (f"not applied: this version would not parse (line {ex.lineno}: {ex.msg})"
                              + ("; the file was kept as it was" if exists else "") + ". Resend the COMPLETE file, or a smaller EDIT")
                return res
    res.update(status="ok", old=old, new=new, crlf=crlf, existed=exists, diff=unified(old, new, res["rel"]))
    return res


# ----------------------------------------------------------------------------------------------
# Running commands and tests
# ----------------------------------------------------------------------------------------------
_NET = {"mode": "offline", "allow": [], "tools": "block"}


def set_net(cfg):
    """Network policy for code the Architect runs on your behalf (tests, benchmarks, model-written scripts).
    behavior.network: offline (loopback only) | practice (loopback + hostnames in behavior.net_allow) | open (no restriction).
    behavior.net_block_tools: also refuse to launch curl/nmap/ssh/git... from that code. The older offline_tests=false means open."""
    b = (cfg or {}).get("behavior", {})
    mode = str(b.get("network") or "offline").lower()
    if b.get("offline_tests") is False:
        mode = "open"
    _NET["mode"] = mode if mode in ("offline", "practice", "open") else "offline"
    _NET["allow"] = [h.strip() for h in (b.get("net_allow") or []) if str(h).strip()]
    _NET["tools"] = "block" if b.get("net_block_tools", True) else "allow"
    return dict(_NET)


def _same(path, data):
    try:
        with open(path, "rb") as f:
            return f.read() == data
    except OSError:
        return False


def _replace(tmp, path, data):
    """os.replace, patient with Windows: a file another run has open cannot be replaced for a moment ("Access is denied"). Seen live:
    four benchmark runs from one install crashed on `._guard/sitecustomize.py` before their first model call."""
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if _same(path, data) or i == 5:            # the other run already wrote exactly this, or it really is locked
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if _same(path, data):
                    return
                raise
            time.sleep(0.1 * (i + 1))


def _atomic_write(path, text):
    """Write a file so a reader sees either the old one or the new one, never half of it. An identical file is left alone."""
    data = text.encode("utf-8")
    if _same(path, data):
        return
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    _replace(tmp, path, data)


def _atomic_copy(src, dst):
    with open(src, "rb") as f:
        data = f.read()
    if _same(dst, data):
        return
    tmp = f"{dst}.{os.getpid()}.tmp"
    shutil.copy2(src, tmp)
    _replace(tmp, dst, data)


def guard_env(offline=True):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if offline and (_NET["mode"] != "open" or _NET["tools"] == "block") and os.path.isfile(os.path.join(HERE, "netguard.py")):
        g = os.path.join(HERE, "._guard")
        os.makedirs(g, exist_ok=True)
        _atomic_copy(os.path.join(HERE, "netguard.py"), os.path.join(g, "netguard.py"))   # two runs share this folder: a half-written guard must never be importable
        _atomic_write(os.path.join(g, "sitecustomize.py"),
                      "try:\n    import netguard\n    netguard.install_from_env()\nexcept Exception as e:\n"
                      "    import sys\n    sys.stderr.write('[netguard] %s\\n' % e)\n")
        env["PYTHONPATH"] = g + os.pathsep + env.get("PYTHONPATH", "")
        env["AGENT_NET_MODE"] = _NET["mode"]
        env["AGENT_NET_ALLOW"] = ",".join(_NET["allow"])
        env["AGENT_NET_TOOLS"] = _NET["tools"]
    return env


def run_cmd(cmd, cwd, timeout=300, offline=True):
    """cmd: list or string. -> (returncode, combined output tail). Never raises for a normal failure."""
    argv = cmd if isinstance(cmd, (list, tuple)) else shlex.split(str(cmd), posix=(os.name != "nt"))
    argv = [str(a).replace("{python}", sys.executable) for a in argv]
    try:
        rc, out = run_bounded(argv, cwd, timeout, guard_env(offline))
    except OSError as e:
        return 127, f"could not start: {e}"
    if rc is None:
        return 124, (f"timed out after {timeout}s (the whole process tree was stopped; a test that starts a server must stop it, "
                     f"or run it in a thread with a timeout)\n" + out[-3000:])
    return rc, out[-6000:]


def kill_tree(pid):
    """Stop a process AND everything it started. Killing only the direct child leaves a server a test started still running."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass


_SANDBOX = {"mem_mb": 0}     # set per run by the Engine from behavior.sandbox_mem_mb
_ECORES = {"on": False}       # set per run by the Engine from behavior.tests_on_ecores (children it starts inherit the affinity)


def run_bounded(argv, cwd, timeout, env=None):
    """Run with a hard wall-clock limit that really holds. -> (returncode or None on timeout, combined output).

    subprocess.run(timeout=) is not enough on Windows: it kills the direct child, then waits for the output pipes to close -- and a
    grandchild (a server the model's test started) keeps them open, so the wait never ends. One live e2e task sat there for two
    hours. Output goes to a temp file instead of a pipe, and on timeout the whole tree is stopped."""
    import tempfile as _tf
    import arch_sandbox
    mem = int(_SANDBOX.get("mem_mb") or 0)
    with _tf.TemporaryFile() as out:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        pre = arch_sandbox.posix_preexec(mem)
        if pre:
            kw["preexec_fn"] = pre
        p = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kw)
        job = arch_sandbox.attach(p, mem)                  # a memory cap, and everything it starts is stopped when it ends
        if _ECORES.get("on"):                              # tests on the efficiency cores while the model servers keep the performance cores
            try:
                import arch_hw
                arch_hw.pin_to_ecores(p._handle if os.name == "nt" else p.pid)
            except Exception:
                pass
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            rc = None
        arch_sandbox.close(job)
        out.seek(0)
        text = out.read().decode("utf-8", "replace")
    why = arch_sandbox.explain(rc, text, mem)
    if why:
        text = text + "\n[sandbox] " + why
    return rc, text


def has_module(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


def run_tests(root, cfg, targets=None):
    """-> ("pass"|"fail"|"none", log). Custom command from config wins; else pytest, else unittest."""
    set_net(cfg)
    t = cfg["limits"]["test_timeout"]
    off = True                                   # the policy itself (behavior.network) decides how open this is
    custom = cfg["tests"].get("command")
    if custom:
        rc, out = run_cmd(custom, root, t, off)
        return ("pass" if rc == 0 else "fail"), out
    if has_module("pytest"):
        cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--tb=short", "-p", "no:cacheprovider"] + list(targets or [])
        rc, out = run_cmd(cmd, root, t, off)
        if rc == 5:
            return "none", out
        return ("pass" if rc == 0 else "fail"), out
    rc, out = run_cmd([sys.executable, "-m", "unittest", "discover", "-q"], root, t, off)
    if "Ran 0 tests" in out:
        return "none", out
    return ("pass" if rc == 0 else "fail"), out


# ----------------------------------------------------------------------------------------------
# Engine: one run of one flow
# ----------------------------------------------------------------------------------------------
_SELFDEV = {}


def selfdev_brief(root):
    """The Architect-developer profile for a self-improve run: its rules, the project notes (CLAUDE.md, the start) and the master
    plan's progress log (the end), so the model knows the conventions and what was already done. Read once per process."""
    if root in _SELFDEV:
        return _SELFDEV[root]
    import arch_roles
    import glob
    parts = ["[role:architect_dev] " + arch_roles.ROLES["architect_dev"]["system"]]
    try:
        parts.append("PROJECT NOTES (CLAUDE.md, start):\n" + open(os.path.join(root, "CLAUDE.md"), encoding="utf-8").read()[:3000])
    except OSError:
        pass
    plans = sorted(glob.glob(os.path.join(root, "docs", "MASTER_PLAN_*.md")))
    if plans:
        try:
            txt = open(plans[-1], encoding="utf-8").read()
            i = txt.find("### Progress log")
            parts.append("PLAN PROGRESS LOG (what is already done):\n" + (txt[i:i + 2500] if i >= 0 else txt[-2500:]))
        except OSError:
            pass
    _SELFDEV[root] = "\n\n".join(parts)
    return _SELFDEV[root]


# Roles that keep reasoning on under behavior.thinking = plan_debug_only (the others answer straight away)
THINK_ROLES = ("planner", "architect", "bug_hunter", "debugger")

# --- provenance: which weights answered a run, as this computer knows them (the model's passport or its file's own header; never the
# network). One "provenance" trace record per model per run, and the same facts under "provenance" in run.json (Engine._provenance).
ORTHO_MARKERS = ("abliterat", "orthogon", "heretic")   # refusal direction removed from the weights (Heretic is an abliteration tool)
UNCENSOR_MARKERS = ("uncensor",)                       # often a fine-tune on filtered data (llama2-uncensored), so the method is not stated
# (arch_costmodel keeps its own, broader flag for the cost map; this is the receipt's statement of method.)
# Only CLEAR non-commercial / research-only terms: Creative Commons NC, "non-commercial", "research only", the Mistral Non-Production
# and Mistral Research (MRL) licences, the Qwen / Tongyi Qianwen Research licences. Llama/Gemma community licences, the plain Tongyi
# Qianwen licence, Apache, MIT, OpenRAIL and "other" are not flagged. Never blocks a run.
NONCOMMERCIAL_RX = re.compile(r"cc[-_ ]?by[-_ ]?nc|non[-_ ]?commercial|research[-_ ]only|research purposes only|not for commercial"
                              r"|non[-_ ]?production|\bmnpl\b|(?:qwen|qianwen)[-_ ]research|mistral[-_ ]?(?:ai[-_ ]?)?research|\bmrl\b", re.I)
_QUANT_RX = re.compile(r"(?:^|[^a-z0-9])((?:iq|q)\d(?:_[a-z0-9]{1,2}){0,2}|bf16|fp?16|fp?32|mxfp4)(?=$|[^a-z0-9])", re.I)


def weight_edit_of(*names, base_model=None):
    """What was done to the weights, from the names alone (the tag, the file's general.name / finetune / tags, and the base model's
    name, since a fine-tune of an abliterated model inherits the edit): "orthogonalized" for abliterat / orthogon / heretic;
    "uncensored" when only "uncensor" is said (method not stated); "none" when the file names a base model and no marker; else
    "unknown"."""
    low = " ".join(str(n or "") for n in (*names, base_model)).lower()
    if any(w in low for w in ORTHO_MARKERS):
        return "orthogonalized"
    if any(w in low for w in UNCENSOR_MARKERS):
        return "uncensored"
    return "none" if base_model else "unknown"


def license_terms(lic):
    """"non_commercial" when a model file's license clearly limits use to non-commercial or research work, else None. A note, never a gate."""
    return "non_commercial" if lic and NONCOMMERCIAL_RX.search(str(lic)) else None


def model_provenance(model):
    """-> {"model", "quant", "family", "params_b", "moe", "active_b", "weight_edit", "base_model", "license", "source", "digest"} for one
    model, from what is on this computer: its passport (hub_data/passports), else its GGUF header (a .gguf path, or the blob Ollama
    already has), else only its name. source says which: passport | gguf | name | claude (a cloud model: no local weights). digest:
    "sha256:..." from Ollama's manifest or blob name; for a plain file "stat:<bytes>:<mtime>" (hashing gigabytes per run would cost
    minutes); None when no file was found. Best effort: an unreadable passport or file just leaves fields None."""
    m = str(model or "")
    rec = {"model": m, "quant": None, "family": None, "params_b": None, "moe": None, "active_b": None, "weight_edit": "unknown",
           "base_model": None, "license": None, "source": "name", "digest": None}
    if m.startswith("claude:"):
        rec.update(family="claude", weight_edit="none", source="claude")
        return rec
    import arch_passport
    gguf = m.lower().endswith(".gguf")
    tail = re.split(r"[\\/]", m)[-1]          # the file name of a path (either slash) or the last part of a tag: never a folder's name
    p = None
    for nm in ([m, tail] if gguf else [m]):
        try:
            p = arch_passport.load(nm)
        except Exception:
            p = None
        if isinstance(p, dict) and p:
            break
        p = None
    lin, path, digest = {}, None, None
    names = [tail if gguf else m]             # a file's folder names are not the model's name
    if p:
        a = p.get("arch") if isinstance(p.get("arch"), dict) else {}
        rec.update(source="passport", quant=p.get("quant"), family=p.get("family"), params_b=a.get("params_b"),
                   moe=p.get("moe"), active_b=p.get("active_b"))
        lin = p.get("lineage") if isinstance(p.get("lineage"), dict) else {}
        path = p.get("path") if isinstance(p.get("path"), str) else None
        names.append(p.get("name"))
    if not path:
        try:
            if gguf:
                path = m if os.path.isfile(m) else None
            else:
                import arch_server
                ab = arch_server.adopt_ollama(m)              # a manifest read on disk: the blob path and its sha256
                if ab:
                    path, digest = ab.get("gguf"), ab.get("digest")
        except Exception:
            path = None
    if path and not (p and isinstance(p.get("lineage"), dict)):    # no passport, or one written before passports kept lineage
        # the whole header is read only when the quantization is still wanted (it often sits after the vocabulary)
        hf = arch_passport.header_facts(path, need_quant=not rec["quant"])
        if hf:
            lin = hf.get("lineage") or {}
            if not p:
                import arch_profiles
                total = hf.get("params_b")
                act = arch_profiles.active_from_arch(total, hf) if total else None
                rec.update(source="gguf", quant=hf.get("quant"), family=hf.get("family"), params_b=total, active_b=act,
                           moe=bool(act) if total else None)
            else:
                rec["quant"] = rec["quant"] or hf.get("quant")
                rec["family"] = rec["family"] or hf.get("family")
    if rec["source"] == "name":               # only with nothing read from a file: a guess from the tag is never shown as the file's word
        q = _QUANT_RX.search(tail)
        rec["quant"] = q.group(1).upper() if q else None
        try:
            import arch_profiles
            rec["params_b"] = arch_profiles.params_b(tail)
        except Exception:
            pass
    if path and not digest:
        b = os.path.basename(path)
        if re.fullmatch(r"sha256[-:][0-9a-fA-F]{64}", b):
            digest = "sha256:" + b[7:].lower()
        else:
            try:
                st = os.stat(path)
                digest = f"stat:{st.st_size}:{int(st.st_mtime)}"
            except OSError:
                digest = None
    rec["digest"] = digest
    rec["base_model"] = lin.get("base_model")
    rec["license"] = lin.get("license")
    tags = lin.get("tags") if isinstance(lin.get("tags"), list) else []
    rec["weight_edit"] = weight_edit_of(*names, lin.get("model_name"), lin.get("finetune"), " ".join(str(t) for t in tags),
                                        base_model=rec["base_model"])
    return rec


def keeps_prompt_memory(root):
    """Whether the worker's prompt memory (llama-server's saved slot, 180-380 MB a file here) is worth saving for this project at the end
    of a run. Not for a throwaway folder no run will ever reopen: an eval run (ARCHITECT_EVAL=1), a folder under the system temp
    directory (eval tasks, tests), or a live test's live_* folder in the data folder. Seen 2026-11-05: 19 eval_build_*.bin, 4.9 GB."""
    if os.environ.get("ARCHITECT_EVAL") == "1":
        return False
    import tempfile
    try:
        r = os.path.normcase(os.path.realpath(root))
    except (OSError, ValueError):
        return True
    try:
        tmp = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
        if os.path.commonpath([r, tmp]) == tmp:
            return False
    except (OSError, ValueError):            # a project on another drive than the temp folder: not under it
        pass
    try:
        dd = os.path.normcase(os.path.realpath(data_dir()))
        if os.path.dirname(r) == dd and os.path.basename(r).startswith("live_"):
            return False
    except (OSError, ValueError):
        pass
    return True


class Engine:
    def __init__(self, root, flow, goal, cfg=None, review=None, ask=None, say=None, persist=True):
        self.root = os.path.abspath(root)
        self.cfg = cfg or load_config(self.root)
        set_net(self.cfg)
        self.flow, self.goal = flow, goal
        self.review = review or self.cfg["behavior"]["review_mode"]
        self.ask = ask or self._ask
        self.say = say or (lambda m: print(m, flush=True))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{stamp}_{flow}"
        if persist:
            self.run_dir = os.path.join(self.root, ".arch", "runs", self.run_id)
            os.makedirs(self.run_dir, exist_ok=True)
        else:                                   # a throwaway engine (routing, previews): leave nothing in the project
            import tempfile
            self.run_dir = tempfile.mkdtemp(prefix="arch_tmp_")
        self.calls, self.seen, self.n_apply_all = 0, collections.Counter(), False
        self.created, self.backed_up, self.report_lines, self.changed_files = [], set(), [], []
        self.models_used = collections.Counter()
        self.deny_prefixes = []        # files a step may not edit (e.g. characterization tests during a refactor)
        self._installed = None
        self.stats = arch_tune.load_stats()    # per-model measured speed / chars-per-token (learned from real calls)
        self.ctx_floor = {}            # model -> window it needed after a prompt overflowed (kept for the rest of the run)
        self.ctx_events = []           # plain-language notes about overflow/clamping, also written to the run report
        try:
            import arch_power
            self.power = arch_power.battery()
            saved = arch_power.saver(self.cfg, self.power)
        except Exception:                                  # detection must never stop a run
            self.power, saved = {"present": False}, []
        self.pins = {}                 # role -> model it is temporarily handed to (escalation); cleared by the progress coach
        self._contexts = []            # the last project contexts built (newest first): what a prompt too big for its window cuts first
        self._srv_ctx = {}             # model -> the window its llama-server was started with (None = not served by one)
        self.persist_runs = persist
        self.down_hosts = set()        # machines found unreachable this run (their roles run locally for the rest of it)
        self.gpu_seen = set()          # (model, window) pairs already checked for GPU spill this run
        self.reuse = {"calls": 0, "tokens": 0, "cached": 0}   # calls served from a cached prefill, the tokens that saved (estimate; see call()), and whole replies reused (arch_cache)
        self._lock = threading.RLock()  # several candidates can be generated at once (best-of-N on a server with parallel slots); the
                                         # bookkeeping is serialised, only the wait for the model runs concurrently
        self.cache_keys = {}           # trace id -> reply-cache key, so finish() can record what each remembered reply led to
        self.prompt_ref = ""           # which prompt file, and which version of it, the current step is using (set by arch_dataflow.load_prompt)
        self._fp = {}                  # model -> a stamp of its own bytes, so a re-pulled model never serves the old model's answers
        _ECORES["on"] = (self.cfg["behavior"].get("tests_on_ecores", "auto") == "auto")
        _SANDBOX["mem_mb"] = int(self.cfg["behavior"].get("sandbox_mem_mb", 4096) or 0)
        self._inflight = collections.Counter()   # model -> calls waiting on it now (a server is never stopped under a call)
        self._aside = {}               # tier -> the names it answers to, for our tiers this run stopped so a bigger model got the graphics
                                       # card (also in the servers' state file with this process as owner: arch_server.step_aside)
        self.meter = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "secs": 0.0, "heavy_calls": 0, "escalations": 0}   # what this run has spent (arch_dataflow budgets)
        self.budgets = []              # a stack of {"name", "calls", "tokens", "seconds", "heavy_calls", "start": meter snapshot}; call() refuses to exceed any level
        import arch_chain
        self.swaps = arch_chain.Swaps()          # model changes between calls and what they cost in loading time (arch_chain.py)
        self.state_path = os.path.join(self.run_dir, "run.json") if persist else None
        self.state = {"run_id": self.run_id, "flow": flow, "goal": goal, "root": self.root, "status": "running", "pid": os.getpid(), "review": self.review,
                      "started": time.time(), "params": None, "checkpoints": {}}
        self._write_state()
        self.report(f"# Architect run {self.run_id}", "", f"- Flow: `{flow}`", f"- Review mode: {self.review}", f"- Goal: {goal}", "")
        if saved:
            self.say(f"[battery] on battery ({self.power.get('pct')}%): {', '.join(saved)} for this run (Settings > behavior.battery_saver = off to keep your settings)")
            self.report(f"- Battery saver: {', '.join(saved)} (this run only)")

    # --- run state (so an interrupted run can be resumed: arch_resume.py)
    def todo_set(self, items):
        """The run's live todo list (like Claude Code's task list): [{"id", "title"}] -> all pending. Shown by the hub as it ticks."""
        self.state["todo"] = [{"id": str(i.get("id")), "title": str(i.get("title") or i.get("id"))[:120], "status": "pending"} for i in items]
        self._write_state()
        try:                                                 # the hub's live panel shows the steps still to come
            self._trace({"type": "todo", "t": time.time(), "flow": self.flow, "items": self.state["todo"]})
        except Exception:
            pass

    def todo_mark(self, item_id, status):
        """status: doing | done | failed | skipped."""
        for it in self.state.get("todo") or []:
            if it["id"] == str(item_id):
                it["status"] = status
                self._write_state()
                return True
        return False

    def _write_state(self):
        if not self.state_path:
            return
        self.state["updated"] = time.time()
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1, default=str)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def checkpoint(self, **kv):
        """Save progress a resume can use (for the build flow: the plan and the sprints that passed)."""
        self.state["checkpoints"].update(json.loads(json.dumps(kv, default=str)))
        self._write_state()

    def confirm_memory(self, ok):
        """A run that passed its gates confirms the lessons it was shown; one that did not leaves them as they were (and lines that never
        help are dropped). Memory grows from evidence."""
        try:
            import arch_memory
            if getattr(self, "memory_used", None):
                if ok:
                    arch_memory.confirm(self.root, ("lessons.md", "gotchas.md"), set(self.memory_used), say=self.say,
                                        prune_after=(self.cfg.get("memory") or {}).get("prune_after"))
                else:
                    arch_memory.mark_used(self.root, ("lessons.md", "gotchas.md"), set(self.memory_used))
        except Exception:
            pass
        try:                                                     # a skill that was in front of the model when the run passed earns a point
            import arch_skills
            if ok and getattr(self, "skills_used", None):
                arch_skills.mark_helped(self.skills_used)
        except Exception:
            pass
        try:                                                     # a remembered reply records what it led to, so a later hit says so up front
            import arch_cache
            for k in set((getattr(self, "cache_keys", None) or {}).values()):
                arch_cache.attach_verdict(k, "pass" if ok else "fail")
        except Exception:
            pass

    def _slot_early_save(self, rec):
        """server.slot_persist (auto): after the FIRST llama-server call of a run, save that slot's prompt memory for this project, so
        the next run here restores the stable prefix (instructions + project context) even when this one is stopped before finish().
        off = the old behaviour (restore at the start, save only at the end). Best effort, never raises."""
        try:
            mode = str((self.cfg.get("server") or {}).get("slot_persist", "auto")).lower()
            if (mode == "off" or getattr(self, "_slot_saved", False) or rec.get("transport") != "llama"
                    or rec.get("tier") not in ("worker", "heavy") or not keeps_prompt_memory(self.root)):
                return
            self._slot_saved = True
            import arch_server
            arch_server.slot_io("save", self.cfg, os.path.abspath(self.root), tier=rec["tier"], slot=int(getattr(self, "slot_id", 0) or 0))
        except Exception:
            pass

    def finish(self, ok, error=None):
        self._bring_back()
        self._release_ollama()
        if self.calls and keeps_prompt_memory(self.root):      # never for a throwaway folder (eval, temp, live test): nothing reopens it
            try:
                import arch_server
                arch_server.slot_io("save", self.cfg, os.path.abspath(self.root), slot=int(getattr(self, "slot_id", 0) or 0))
            except Exception:
                pass                     # a tier that stepped aside for a bigger model is back before the run is called done
        if self.calls and self.state_path:
            self.report("", *self.swaps.lines(self.calls, self.reuse, time.time() - self.state["started"]))
        self.confirm_memory(bool(ok))
        self.state["status"] = "done" if ok else ("stopped" if error else "incomplete")
        if error:
            self.state["error"] = str(error)[:300]
        self._write_state()
        if self.state_path:                                  # outcomes of this run's calls go to the per-model track record (arch_ledger.py)
            try:
                import arch_ledger
                arch_ledger.record_run(self.run_dir, self.run_id)
            except (OSError, ValueError, KeyError):
                pass
            try:                                             # a copy on the archive drive, written in the background (arch_archive.py)
                import arch_archive
                arch_archive.archive_run(self.cfg, self.run_dir, os.path.basename(os.path.abspath(self.root)), self.run_id)
            except Exception:
                pass

    # --- reporting
    def report(self, *lines):
        self.report_lines.extend(lines)
        try:
            with open(os.path.join(self.run_dir, "report.md"), "w", encoding="utf-8") as f:
                f.write("\n".join(self.report_lines) + "\n")
        except OSError:
            pass

    def _ask(self, prompt):
        try:
            return input(prompt)
        except EOFError:
            return ""

    # --- budgets (flows as data): hard limits per flow and per step, checked before every call
    def push_budget(self, name, calls=None, tokens=None, seconds=None, heavy_calls=None, **_):
        self.budgets.append({"name": name, "calls": calls, "tokens": tokens, "seconds": seconds, "heavy_calls": heavy_calls, "t0": time.time(),
                             "start": dict(self.meter)})

    def pop_budget(self):
        if self.budgets:
            return self.budgets.pop()

    def budget_left(self):
        """-> {"tokens", "seconds", "calls"}: the tightest remaining allowance across the budget stack (None = unlimited)."""
        left = {"tokens": None, "seconds": None, "calls": None}
        for b in self.budgets:
            if b["tokens"] is not None:
                used = (self.meter["tokens_in"] + self.meter["tokens_out"]) - (b["start"]["tokens_in"] + b["start"]["tokens_out"])
                left["tokens"] = min(x for x in (left["tokens"], max(0, int(b["tokens"]) - used)) if x is not None)
            if b["seconds"] is not None:
                rem = max(0.0, float(b["seconds"]) - (time.time() - b["t0"]))
                left["seconds"] = min(x for x in (left["seconds"], rem) if x is not None)
            if b["calls"] is not None:
                rem = max(0, int(b["calls"]) - (self.meter["calls"] - b["start"]["calls"]))
                left["calls"] = min(x for x in (left["calls"], rem) if x is not None)
        return left

    def _check_budgets(self, model):
        stop_files = [os.path.join(self.root, ".arch", "STOP"), os.path.join(data_dir(), "STOP")]
        if any(os.path.isfile(p) for p in stop_files):
            note = self.inbox_note()
            raise BudgetExceeded(f"stopped to do this instead: {note}" if note else
                                 "stopped by the kill switch (a STOP file is present); remove it to continue")
        # calls already sent and still waiting on a model (other threads of a best-of-N batch) count as spent: the meter only moves when
        # they return, and N requests checked together must not all fit into the last free call. One caller at a time: always 0 here.
        pending = sum(getattr(self, "_inflight", {}).values())
        for b in self.budgets:
            if b["calls"] is not None and self.meter["calls"] + pending - b["start"]["calls"] >= int(b["calls"]):
                raise BudgetExceeded(f"{b['name']}: {b['calls']} model calls used up")
            if b["seconds"] is not None and time.time() - b["t0"] >= float(b["seconds"]):
                raise BudgetExceeded(f"{b['name']}: {int(b['seconds'])} seconds used up")
            if b["tokens"] is not None and (self.meter["tokens_in"] + self.meter["tokens_out"]) - (b["start"]["tokens_in"] + b["start"]["tokens_out"]) >= int(b["tokens"]):
                raise BudgetExceeded(f"{b['name']}: {b['tokens']} tokens used up")
            if b["heavy_calls"] is not None and model and model == self.tier_model("heavy") and self.cfg.get("tiers", {}).get("heavy") and self.meter["heavy_calls"] - b["start"]["heavy_calls"] >= int(b["heavy_calls"]):
                raise BudgetExceeded(f"{b['name']}: {b['heavy_calls']} heavy-tier calls used up")

    # --- models and calls
    def installed(self):
        if self._installed is None:
            self._installed = list_models()
        return self._installed

    def model_for(self, role):
        m = self._model_for(role)
        if str(m or "").startswith("claude:"):                                   # tell the cloud tier whose call this is (its effort)
            import arch_cloud
            arch_cloud.note_role(role)
        return m

    def _model_for(self, role):
        if self.pins.get(role):
            return self.pins[role]
        ct = (self.cfg.get("online") or {}).get("cloud_tier")
        if role != "router" and ct in ("all", "plan"):
            import arch_cloud
            if ct == "all" or role in arch_cloud.PLAN_ROLES:                     # plan = only the thinking roles; the rest stays local
                if arch_cloud.mode(self.cfg) == ct:                              # Settings > Online > Use Claude = all / plan
                    return arch_cloud.cloud_model(self.cfg)
        m = (self.cfg["roles"].get(role) or "").strip()
        if m:                                                                     # a model you assigned to this role wins
            return (self.tier_model(m[5:]) or m) if m.startswith("tier:") else m
        tm = self.tier_model(arch_roles.ROLES[role].get("tier", "worker"))        # no assignment: the role's own tier (router/worker/heavy)
        if tm:                                                                    # comes before borrowing a related role's model
            return tm
        for r in arch_roles.FALLBACK.get(role, []):                               # no tiers set up either: borrow a related role's model
            m = (self.cfg["roles"].get(r) or "").strip()
            if m:
                return (self.tier_model(m[5:]) or m) if m.startswith("tier:") else m
        if self.cfg.get("default_model"):
            return self.cfg["default_model"]
        import arch_index
        inst = [m for m in self.installed() if not arch_index.EMBED_RX.search(m)]     # one definition of "an embedding model" for the whole harness
        if inst:
            return inst[0]
        raise LLMError("No model chosen and none installed. Pick models in the hub (Roles tab) or run `ollama pull <model>`.")

    def tier_model(self, tier):
        """The model assigned to a tier (Settings > Tiers): heavy falls back to worker, router to worker; a draft only drafts. '' when none."""
        tiers = self.cfg.get("tiers") or {}
        for t in {"heavy": ("heavy", "worker"), "router": ("router", "worker"), "worker": ("worker",), "draft": ("draft",)}.get(tier, (tier,)):
            m = (tiers.get(t) or "").strip()
            if m:
                return m
        return ""

    def _model_defaults(self, model, role, ro, mo, options, free_temp=True, free_think=True):
        """behavior.model_defaults = auto: the model family's own sampling/thinking (arch_modelknow.settings_for) fill ONLY what
        the person left unset (no role/model override, no explicit argument). Changes `options` in place; -> what was applied.
        off (the default) -> {} and nothing changes."""
        if self.cfg["behavior"].get("model_defaults", "off") != "auto" or str(model).startswith("claude:"):
            return {}
        try:
            import arch_jobs
            import arch_modelknow
            job = arch_jobs.job_of(role) or "code"
            got = arch_modelknow.settings_for(model, job) or {}
        except Exception:
            return {}
        mine = set((mo.get("sampling") or {})) | set((ro.get("sampling") or {}))
        applied = {"job": job, "source": got.get("source")}
        samp = {k: v for k, v in (got.get("sampling") or {}).items()
                if k not in mine and not (k == "temperature" and not free_temp) and isinstance(v, (int, float))}
        if samp:
            options.update(samp)
            applied["sampling"] = samp
        if free_think and "think" not in ro and "think" not in mo and isinstance(got.get("thinking"), bool):
            applied["thinking"] = got["thinking"]
        if got.get("tool_format"):
            applied["tool_format"] = got["tool_format"]      # recorded for the trace; tool calls are parsed as before
        return applied if len(applied) > 2 else {}

    def call(self, role, user, extra_system="", json_out=False, think=None, temperature=None, num_predict=None, label=None, schema=None):
        """One model call as a role. Safe to call from several threads at once: counters, budgets and the trace are updated one at a time,
        while the wait for the model itself is shared -- which is the only part worth running side by side."""
        with self._lock:
            return self._call(role, user, extra_system, json_out, think, temperature, num_predict, label, schema)

    def _call(self, role, user, extra_system="", json_out=False, think=None, temperature=None, num_predict=None, label=None, schema=None):
        """One model call as a role. Returns dict(text, thinking, id, model, ...). Traced for training/audit."""
        lim = self.cfg["limits"]
        if self.calls >= lim["max_calls"]:
            raise BudgetExceeded(f"stopped after {self.calls} model calls (Settings > Limits > max calls)")
        if self.calls == 0 and self.persist_runs:
            self._prepare_server()
        key = hashlib.sha1((role + user).encode("utf-8", "replace")).hexdigest()
        self.seen[key] += 1
        if self.seen[key] > 2:
            raise LoopDetected(f"the {role} was asked the same thing 3 times; stopping instead of repeating")
        model = self.model_for(role)
        self._check_budgets(model)
        ro = (self.cfg["role_overrides"].get(role) or {})
        mo = self._model_override(model)
        spec = arch_roles.ROLES[role]
        options = {"num_ctx": max(int(mo.get("num_ctx", lim["num_ctx"])), self.ctx_floor.get(model, 0)),
                   "temperature": spec["sampling"].get("temperature", 0.2)}
        options.update({k: v for k, v in spec["sampling"].items()})
        options.update(mo.get("sampling") or {})
        options.update(ro.get("sampling") or {})
        for hk in ("num_batch", "num_thread"):                                # hardware knobs measured by `tune --deep`
            if mo.get(hk):
                options[hk] = int(mo[hk])
        srv_ctx = self._served_ctx(model)
        if not srv_ctx and self._keep_off_card(model):
            options["num_gpu"] = 0                # Ollama would put part of this model on the card our worker server owns: RAM only
        if srv_ctx and srv_ctx < options["num_ctx"]:
            options["num_ctx"] = srv_ctx          # llama-server's window is set when it starts: a bigger number here only overruns it
        if temperature is not None:
            options["temperature"] = temperature
        elif (getattr(self, "retry_temp", None) or {}).get(role) is not None:
            options["temperature"] = max(float(options.get("temperature") or 0), float(self.retry_temp[role]))   # a fresh try after a stall
        if self.cfg["behavior"].get("seed") is not None:
            options["seed"] = int(self.cfg["behavior"]["seed"])
        npred = num_predict or ro.get("num_predict") or mo.get("num_predict")
        # No explicit limit: a cap fitted to the call. Half the window (6144 tokens at 12288) let a model stuck in a repetition loop
        # write for minutes; real replies are 100-3000 tokens. Structured answers (plans, routing, judging) are short; code is longer.
        options["num_predict"] = int(npred) if npred else min(max(1024, options["num_ctx"] // 2), 1536 if (json_out or schema) else 3072)
        _rb_key = None
        if not npred:                             # learned per kind of call (arch_replybudget): p95 x 1.5, never after a recent cut-off
            try:
                import arch_replybudget as _rb
                if not _rb.off(self.cfg):
                    _rb_key = _rb.key(role, label, bool(json_out or schema))
                    learned = _rb.cap(_rb_key, options["num_predict"])
                    if learned:
                        options["num_predict"] = learned
            except Exception:
                _rb_key = None
        th = ro.get("think", mo.get("think", spec["think"]))
        if isinstance(th, dict):
            th = th.get("default", spec["think"])
        if think is not None:
            th = think
        elif self.cfg["behavior"].get("thinking", "roles") == "plan_debug_only":
            th = role in THINK_ROLES                 # reasoning only where it pays (planning, debugging); edits and reviews answer straight away
        if (self.cfg["behavior"].get("sampling", "roles") == "model_card" and temperature is None and role != "router"
                and re.search(r"qwen3", model, re.I) and not ro.get("sampling") and not mo.get("sampling")):
            # Qwen3's own card: thinking 0.6 / 0.95 / 20, otherwise 0.7 / 0.8 / 20, min_p 0; never greedy for a thinking model
            options.update({"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0} if th else
                           {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0})
        model_defaults = self._model_defaults(model, role, ro, mo, options, temperature is None, think is None)
        if "thinking" in model_defaults:
            th = model_defaults["thinking"]
        base_user = user
        fewshot_model = None                    # whose past reply rode along as FORMAT REFERENCE (recorded in the call trace)
        if (self.cfg["behavior"].get("learn_from_runs", "on") == "on" and not json_out and "FEEDBACK" not in user
                and not os.environ.get("ARCHITECT_EVAL")):     # a measurement never sees past answers
            # First tries only: on a retry the feedback is the better signal, and an example there competes with it.
            try:
                import arch_flows as _fl
                import arch_ledger
                ex = arch_ledger.example_for(role, user, root=self.root, named=_fl.named_files(self.goal or ""), model=model)
            except Exception:
                ex = None
            if ex:
                fewshot_model = ex.get("model") or model
            if ex:                              # at the END of the user turn, not in the system prompt: the system prompt + project context stay byte-identical
                user = user.rstrip() + ("\n\nFORMAT REFERENCE (a past reply of yours for a similar request that passed the checks; copy its SHAPE, "   # across retries, so the server reuses the cached prefill
                                        "not its content):\n" + ex["reply"])
        sys_text = arch_roles.system_prompt(role, extra_system)
        if os.environ.get("ARCHITECT_SELFDEV") == "1":           # a self-improve run: every call works as the Architect developer
            sys_text = sys_text + "\n\n" + selfdev_brief(self.root)
        style = arch_roles.edit_style_note(self.cfg["behavior"].get("edit_style", "patch"))
        if style and arch_roles.EDIT_PROTOCOL in sys_text:                 # only roles that write files
            head, sep, tail = sys_text.rpartition("\n\n[role:")
            sys_text = head + "\n\n" + style + sep + tail
        # --- fit the whole prompt to the window this model really has, keeping room for the answer. Seen live: a sprint prompt filled
        # ~7k of an 8192 window, the multi-file answer ran past it, and the run ended. The format example goes first, then the least
        # relevant end of the project context (outlines before whole files), at a section boundary.
        if str(model).startswith("claude:"):                   # Claude reads ~1M tokens: never cut its prompt to a LOCAL model's window.
            options["num_ctx"] = max(int(options["num_ctx"]), 200000)   # Seen live: an edit request lost the very line to change.
        cpt0 = arch_tune.cpt_for(self.stats, model)
        room = min(int(options["num_predict"]), arch_tune.REPLY_ROOM_TOKENS)
        fits = lambda u: int((len(sys_text) + len(u)) / max(cpt0, 1.0) * arch_tune.EST_MARGIN) + room + 64 <= options["num_ctx"]
        if not fits(user):
            what, size0 = [], len(user)
            if user != base_user:
                user = base_user
                what.append("the format example was left out")
            ctx_text = max((c for c in getattr(self, "_contexts", []) if c and c in user), key=len, default="")
            if not fits(user) and ctx_text:
                allowed = int((options["num_ctx"] - room - 64) * max(cpt0, 1.0) / arch_tune.EST_MARGIN) - len(sys_text)
                keep = max(1500, len(ctx_text) - (len(user) - allowed))
                cut = arch_tune.cut_context(ctx_text, keep)
                user = user.replace(ctx_text, cut, 1)
                what.append(f"the project context was cut from {len(ctx_text)} to {len(cut)} characters (least relevant files first)")
            fi = user.find("FEEDBACK:")
            if not fits(user) and fi >= 0 and len(user) - fi > 4000:
                # still too big: the failure log itself is huge (a long traceback). Keep its first lines and its last 2000 characters,
                # where the actual error is, and say what was cut.
                fb = user[fi:]
                user = user[:fi] + fb[:1200] + f"\n... [{len(fb) - 3200} characters of the failure log cut] ...\n" + fb[-2000:]
                what.append("the middle of a long failure log was cut (its start and end kept)")
            if what:
                self._ctx_note(f"[{role}] the prompt ({size0} characters) left too little room for the answer in the {options['num_ctx']}-token "
                               "window: " + "; ".join(what))
        quoted = self._quoted_lines()
        if quoted and quoted not in user:
            user = user.rstrip() + "\n\n" + quoted
        if mo.get("system_prompt") == "inline" or re.search(r"gemma|deepseek-r1", model, re.I):
            msgs = [{"role": "user", "content": sys_text + "\n\n" + user}]
        else:
            msgs = [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]
        # --- keep the call inside its context window (Ollama truncates silently otherwise)
        prompt_chars = sum(len(m["content"]) for m in msgs)
        cpt = arch_tune.cpt_for(self.stats, model)
        est = arch_tune.estimate_tokens(prompt_chars, cpt)
        wanted = options["num_predict"]
        options["num_predict"], clamped = arch_tune.clamp_num_predict(options["num_ctx"], est, wanted)
        if clamped:
            self._ctx_note(f"[{role}] reply limit lowered from {wanted} to {options['num_predict']} tokens: the prompt (~{est} tokens) "
                           f"leaves that much room in a {options['num_ctx']}-token window")
        left = self.budget_left()
        if left["tokens"] is not None and options["num_predict"] > max(64, left["tokens"] - est):
            options["num_predict"] = max(64, min(options["num_predict"], left["tokens"] - est))
        fmt = schema if schema else ("json" if json_out else None)
        # --- a reply this machine already gave to exactly this question (arch_cache): only at temperature 0, never during an eval.
        # A hit costs no tokens and no seconds, so it is left out of the speed statistics and the spend meter -- but it still counts
        # against max_calls, because a run making sixty free calls is still a runaway run.
        ck, ck_model = "", model          # ck_model: the key is this model's. A reply that ends up coming from a fallback model is NOT stored under it.
        try:
            import arch_cache
            if not arch_cache.why_not(self.cfg, options):
                ck = arch_cache.key(model, msgs, dict(options, think=bool(th)), schema=fmt, fingerprint=self._model_fp(model))
                hit = arch_cache.get(ck, self.cfg)
                if hit:
                    self.calls += 1
                    self.meter["calls"] += 1      # a call happened, so a per-step call budget still counts it; it just cost no tokens and no seconds
                    self.reuse["cached"] = self.reuse.get("cached", 0) + 1
                    tid = self.calls
                    self.cache_keys[tid] = ck
                    saved = int(hit["tokens_in"]) + int(hit["tokens_out"])
                    self.say(f"    reusing the reply already given to exactly this question"
                             + (f" (which ended in: {hit['verdict']})" if hit.get("verdict") else "")
                             + f" -- {saved} tokens and {round(hit['secs'])}s not spent again")
                    self._trace({"type": "call", "id": tid, "t": time.time(), "flow": self.flow, "role": role, "model": model,
                                 "label": label or role, "messages": msgs, "reply": hit["reply"], "thinking_chars": 0, "tok_s": None,
                                 "gen_tokens": int(hit["tokens_out"]), "secs": 0.0, "done_reason": "cached",
                                 "prompt_tokens": int(hit["tokens_in"]), "num_ctx": options["num_ctx"], "ctx_pct": None,
                                 "ctx_overflow": None, "gpu_pct": None, "prompt_est": est, "prefix_reused_est": 0, "load_s": None,
                                 "cached_exact": False,     # no server read this prompt, so there is no prefix-cache saving to claim
                                 "transport": "cache", "cached": True, "cached_verdict": hit.get("verdict", ""),
                                 "tokens_saved": saved, "secs_saved": hit["secs"],
                                 "tier": self.tier_of(model, role),
                                 "prompt_ref": getattr(self, "prompt_ref", ""),
                                 "system_sha": hashlib.sha256(sys_text.encode("utf-8", "replace")).hexdigest()[:12]})
                    self._provenance(model)          # a remembered reply still came from these weights (the key includes their stamp)
                    return {"text": hit["reply"], "thinking": "", "id": tid, "model": model, "truncated": False,
                            "ctx_overflow": None, "prompt_tokens": int(hit["tokens_in"]), "num_ctx": options["num_ctx"],
                            "cached": True, "cached_verdict": hit.get("verdict", "")}
        except Exception as ex:                            # the cache is a speed-up, never a dependency: any trouble and we just call the model
            ck = ""
            self._cache_note = f"{type(ex).__name__}: {ex}"
            if os.environ.get("ARCHITECT_STRICT") == "1":  # ...but a self-test must never pass because the cache quietly did nothing
                raise

        import arch_hosts
        rt0 = None if self.pins.get(role) else arch_hosts.route_of(self.cfg, role)
        if not (rt0 and rt0[0] not in self.down_hosts):          # only a model on THIS machine shares its graphics card
            if not str(model).startswith("claude:"):              # a cloud call uses no graphics memory here
                self._bring_back(model)
                self._make_room(model)
        first_call = self.models_used[model] == 0
        self.calls += 1
        tid = self.calls          # taken NOW, while the lock is held: by the time the model answers, other candidates have moved the counter
        self.models_used[model] += 1
        self.say(f"[{role}] {model} ...")
        t0 = time.time()

        import arch_hosts
        rt = None if self.pins.get(role) else arch_hosts.route_of(self.cfg, role)       # an escalated role runs on a local model, not on its machine
        cur = {"model": model, "host": rt[1] if rt and rt[0] not in self.down_hosts else None, "name": rt[0] if rt else None}

        def skey():                                    # speeds are remembered per machine: a model is not equally fast everywhere
            return f"{cur['model']}@{cur['name']}" if cur["host"] else cur["model"]

        def timeouts():
            if lim.get("timeout_mode", "auto") == "fixed":
                return lim["request_timeout"]
            return arch_tune.timeouts_for(self.stats.get(skey()), est, options["num_predict"], lim["request_timeout"], first_call)

        _tk = {"buf": "", "t": 0.0}

        def tok(piece):                                # live text for the hub: batched to about 3 events a second, never on without events
            _tk["buf"] += piece or ""
            now = time.time()
            if now - _tk["t"] >= 0.33 and _tk["buf"]:
                emit_event({"type": "token", "role": role, "model": cur["model"], "label": label or role, "text": _tk["buf"][-400:]}, getattr(self, "run_id", ""))
                _tk["buf"], _tk["t"] = "", now

        tok = tok if events_on() else None

        def chat():
            self._note_ollama(cur["model"], cur["host"])
            try:
                return stream_chat(cur["model"], msgs, options, th, self._ka(), timeouts(), fmt, on_token=tok, host=cur["host"])
            except LLMError as ex:
                big = self.pins.get(role)
                if big and cur["model"] == big:
                    # the larger model a stalled step was handed to could not answer (still loading beside the resident worker, or no
                    # room for it): the step goes back to the role's own model instead of ending the run on a machine problem
                    self.pins.pop(role, None)
                    own = self.model_for(role)
                    if own and own != big:
                        self.say(f"[!] {big} did not answer ({str(ex)[:90]}); {role} goes back to {own} for the rest of this step")
                        self.report(f"- Escalation to `{big}` failed ({str(ex)[:140]}): `{role}` stayed on `{own}`.")
                        cur.update(model=own, host=None)
                        with self._lock:                   # the worker may have stepped aside for the bigger model: back first
                            self._bring_back(own, allow=1)
                        # the request was sized for the bigger model: size it again for this one (its window, its hardware knobs)
                        mo2 = self._model_override(own)
                        opts = {k: v for k, v in options.items() if k not in ("num_batch", "num_thread")}
                        opts["num_ctx"] = max(int(mo2.get("num_ctx", lim["num_ctx"])), self.ctx_floor.get(own, 0))
                        for hk in ("num_batch", "num_thread"):
                            if mo2.get(hk):
                                opts[hk] = int(mo2[hk])
                        est2 = arch_tune.estimate_tokens(prompt_chars, arch_tune.cpt_for(self.stats, own))
                        opts["num_predict"] = arch_tune.clamp_num_predict(opts["num_ctx"], est2, options["num_predict"])[0]
                        options.clear()
                        options.update(opts)
                        self._note_ollama(own, None)
                        return stream_chat(own, msgs, options, th, self._ka(), timeouts(), fmt, host=None, on_token=tok)
                    self.pins[role] = big                  # nothing else to step back to (a step that pins a tier): the normal recovery below
                if not cur["host"] and "not reachable" in str(ex) and self._restart_local(ex):
                    return stream_chat(cur["model"], msgs, options, th, self._ka(), timeouts(), fmt, host=None, on_token=tok)
                if not cur["host"] or "not reachable" not in str(ex):
                    raise
                local = self.cfg.get("default_model") or next(iter(self.installed()), None)
                if not local:
                    raise
                self.down_hosts.add(cur["name"])
                self.say(f"[!] {cur['name']} is not reachable; the {role} role continues on {local} on this computer for the rest of this run")
                self.report(f"- Host `{cur['name']}` was unreachable: `{role}` ran on `{local}` here instead of `{cur['model']}` there.")
                cur.update(model=local, host=None)
                self._note_ollama(local, None)
                return stream_chat(local, msgs, options, th, self._ka(), timeouts(), fmt, on_token=tok)

        self._inflight[model] += 1
        try:
            r = self._unlocked(chat)
        finally:
            self._inflight[model] -= 1
        model = cur["model"]
        ov = arch_tune.overflow(r["prompt_tokens"], options["num_ctx"], options["num_predict"])
        if ov == "truncated" and lim.get("ctx_autogrow", True):
            bigger = arch_tune.grow_ctx(options["num_ctx"], lim.get("num_ctx_max", 32768))
            if bigger > options["num_ctx"]:
                self._ctx_note(f"[{role}] the prompt filled the whole {options['num_ctx']}-token window, so the server dropped the start of it "
                               f"(the role instructions). Retrying once with a {bigger}-token window; it stays at {bigger} for {model} for the rest of this run")
                self.ctx_floor[model] = bigger
                options["num_ctx"] = bigger
                if self.calls >= lim["max_calls"]:
                    raise BudgetExceeded(f"stopped after {self.calls} model calls (Settings > Limits > max calls)")
                self.calls += 1
                r = self._unlocked(chat)
                model = cur["model"]
                ov = arch_tune.overflow(r["prompt_tokens"], options["num_ctx"], options["num_predict"])
        if ov:
            self._ctx_note(f"[{role}] WARNING: {r['prompt_tokens']} prompt tokens in a {options['num_ctx']}-token window ({ov}): "
                           f"{model} may have ignored part of its instructions. Raise Settings > Limits > Context size, or lower "
                           "Context size for prompts (chars)")
        # prompt_eval_count is what the server READ. On the Ollama versions that serve a repeated prefix from the cached prefill it is
        # far below the prompt's size; prompts here keep their stable part first (arch_flows templates) so that happens on every retry.
        # The semantics are not documented, so three rules keep every number honest whichever way the server counts:
        #   1. the overflow check also looks at the ESTIMATED size: a prompt that by estimate fills the window is warned about even when
        #      the server read little of it (a truncated retry must never be reported as a saving);
        #   2. the chars-per-token ratio is only learned from calls that read at least 90% of the estimate (a partial read would teach a
        #      wrong ratio; `tune` measures the ratio directly, so missing a lesson here is the safe direction);
        #   3. a read far below the estimate is counted as "reuse" and labelled an estimate in the report.
        if not ov and est >= 0.95 * options["num_ctx"]:
            ov = "estimated full"
            self._ctx_note(f"[{role}] by estimate the prompt (~{est} tokens) fills the {options['num_ctx']}-token window; the server reported reading {r['prompt_tokens']} "
                           "(a cached prefix, or a truncated prompt). Raise Settings > Limits > Context size, or lower Context size for prompts (chars)")
        reused = 0
        if r.get("cached_tokens") is not None:                                     # llama-server says exactly how much of the prefix came from its cache
            reused = int(r["cached_tokens"])
            if reused:
                self.reuse["calls"] += 1
                self.reuse["tokens"] += reused
        elif r["prompt_tokens"] and self.models_used[model] > 1 and r["prompt_tokens"] < 0.6 * est and not ov:
            reused = int(est - r["prompt_tokens"])
            self.reuse["calls"] += 1
            self.reuse["tokens"] += reused
        teach = prompt_chars if (r["prompt_tokens"] and r["prompt_tokens"] >= 0.9 * est) else None
        arch_tune.observe(self.stats, skey(), prompt_chars=teach, prompt_tokens=r["prompt_tokens"], num_ctx=options["num_ctx"],
                          gen_tok_s=r["tok_s"], prefill_tok_s=r.get("prefill_tok_s"), load_s=r.get("load_s"))
        arch_tune.save_stats(self.stats)
        gpu = self._gpu_check(model, options["num_ctx"], cur["host"])
        swap_s = self.swaps.note(None if cur["host"] else model, r.get("load_s"), self.stats,     # a call on another machine changes nothing on this GPU
                                 resident=(r.get("transport") == "llama"))                       # ...and a tier with its own always-loaded server never swaps
        if swap_s >= 3:
            self.say(f"    (swapped to {model}: {round(swap_s)}s loading)")
        text = strip_think(r["text"])
        self.meter["calls"] += 1
        self.meter["tokens_in"] += int(r["prompt_tokens"] or 0)
        self.meter["tokens_out"] += int(r["gen_tokens"] or 0)
        self.meter["secs"] = round(self.meter["secs"] + (time.time() - t0), 1)
        if (self.cfg.get("tiers") or {}).get("heavy") and model == self.cfg["tiers"]["heavy"]:
            self.meter["heavy_calls"] += 1
        ctx_pct =round(100 * r["prompt_tokens"] / options["num_ctx"]) if r["prompt_tokens"] and options["num_ctx"] else None
        rec = {"type": "call", "id": tid, "t": time.time(), "flow": self.flow, "role": role, "model": model, "label": label or role, "usd": r.get("usd"),
               "messages": msgs, "reply": text, "thinking_chars": len(r["thinking"]), "tok_s": r["tok_s"],
               "gen_tokens": r["gen_tokens"], "secs": round(time.time() - t0, 1), "done_reason": r["done_reason"],
               "prompt_tokens": r["prompt_tokens"], "num_ctx": options["num_ctx"], "ctx_pct": ctx_pct, "ctx_overflow": ov, "gpu_pct": gpu,
               "prompt_est": est, "prefix_reused_est": reused, "load_s": r.get("load_s"), "transport": r.get("transport") or "ollama",
               "cached_exact": r.get("cached_tokens") is not None, "tier": self.tier_of(model, role),
               "cache_n": r.get("cached_tokens"),        # llama-server timings.cache_n (exact prefix tokens served from its cache); None elsewhere
               "prompt_ref": getattr(self, "prompt_ref", ""), "system_sha": hashlib.sha256(sys_text.encode("utf-8", "replace")).hexdigest()[:12],
               "fewshot_model": fewshot_model, "model_defaults": model_defaults or None}
        if cur["host"]:
            rec["host"] = cur["name"]
        self._trace(rec)
        self._slot_early_save(rec)
        if _rb_key and rec.get("transport") != "claude":
            try:
                import arch_replybudget as _rb
                _rb.record(_rb_key, r.get("gen_tokens"), r.get("done_reason") == "length")
            except Exception:
                pass
        self._provenance(model, host=cur["name"] if cur["host"] else None)   # the model that actually answered (a fallback can differ)
        # the host-unreachable fallback can swap the model underneath (that reply answers a different question); a reply cut off at its
        # limit depends on a server window (llama-server's -c) that is not part of the key, so it is never replayed
        if ck and model == ck_model and r.get("done_reason") != "length":
            try:
                import arch_cache
                arch_cache.put(ck, model, text, tokens_in=int(r["prompt_tokens"] or 0), tokens_out=int(r["gen_tokens"] or 0),
                               secs=round(time.time() - t0, 1), tier=rec["tier"],
                               schema_name=("schema" if schema else "json" if json_out else ""), cfg=self.cfg)
                self.cache_keys[tid] = ck
            except Exception:
                pass
        with_tok = f"{r['tok_s']} tok/s" if r["tok_s"] else f"{rec['secs']}s"
        self.say(f"    done: {r['gen_tokens']} tokens, {with_tok}" + (f", context {ctx_pct}% full" if ctx_pct is not None else ""))
        return {"text": text, "thinking": r["thinking"], "id": tid, "model": model, "truncated": r["done_reason"] == "length",
                "ctx_overflow": ov, "prompt_tokens": r["prompt_tokens"], "num_ctx": options["num_ctx"]}

    def step_name(self):
        """Whatever the run is working on right now, for records that are not themselves model calls. The flows set this as they go;
        before anything sets it, the flow's own name is a truthful answer."""
        return getattr(self, "_step", "") or self.flow

    def set_step(self, name):
        """Name the step that is starting, and say so in the trace, so a live view can show a plan rather than a wall of text."""
        self._step = str(name or "")
        self._trace({"type": "step", "t": time.time(), "flow": self.flow, "step": self._step, "status": "started"})

    def end_step(self, name=None, ok=None, unsure=False, changed=(), tries=None, secs=None):
        self._trace({"type": "step", "t": time.time(), "flow": self.flow, "step": str(name or self.step_name()),
                     "ok": (None if ok is None else bool(ok)), "unsure": bool(unsure),
                     "changed": list(changed or []), "tries": tries, "secs": secs})

    def inbox_path(self):
        return os.path.join(self.run_dir, "inbox.jsonl")

    def inbox(self):
        """Messages sent to this run while it was working: a person changing their mind, in their own words. Oldest first."""
        out = []
        try:
            with open(self.inbox_path(), encoding="utf-8") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                        if isinstance(j, dict) and j.get("message"):
                            out.append(j)
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    def inbox_note(self):
        """The newest redirect message, or ''. Used to explain WHY a run stopped: 'stopped by a kill switch' is true but unhelpful when
        the real reason is that the person asked for something else."""
        msgs = self.inbox()
        return str(msgs[-1]["message"])[:300] if msgs else ""

    def _restart_local(self, ex, limit=2):
        """The resident server on THIS machine dropped the connection mid-run (a crash, often out of memory). Start it again and let the
        same call be sent once more, at most `limit` times per run. -> True when it is serving again. A live build lost its last two
        sprints to one such crash: the run ended "fail" with the server back up seconds later."""
        n = getattr(self, "_local_restarts", 0)
        if n >= limit:
            return False
        self._local_restarts = n + 1
        try:
            import arch_server
            self.say(f"[!] the local model server stopped answering ({str(ex)[:80]}); starting it again (restart {n + 1} of {limit})")
            st = arch_server.ensure(self.cfg, say=self.say)
            self._srv_ctx = {}                     # a restarted server is read again
            if st.get("mode") == "ollama" or st.get("error"):
                return False
            self.report(f"- The local model server crashed during the run and was restarted ({n + 1}); the call was sent again.")
            return True
        except Exception:
            return False

    def _make_room(self, model):
        """Before a call to a model no llama-server here serves (Ollama loads it into whatever graphics memory is left): when it does
        not fit beside our resident tiers, those tiers step off the card for it and come back when a call needs them or the run ends.
        Seen live: a 14B beside the resident 4B worker got ~1.5 GB of the 6 GB card and wrote 2.45 tokens a second, the rest from RAM.
        Never under a running call, and only in a run that owns its ending (persist: a Studio reply or a proposal never calls finish(),
        so it never moves a server). Settings > Server > Make room = off keeps the tiers where they are."""
        if (not self.persist_runs or (self.cfg.get("server") or {}).get("make_room", "auto") == "off"
                or sum(getattr(self, "_inflight", {}).values())):
            return
        try:
            import arch_server
            d = arch_server.step_aside_for(model, self.cfg)
            if not d["tiers"]:
                if d.get("reload"):                        # it fits, but Ollama holds it split from when the card was fuller: load it again
                    import arch_ollama
                    arch_ollama.unload(model)
                return
            arch_server.step_aside(d["tiers"], model, d["names"], owner=os.getpid(), run=self.run_id, say=lambda m: None)
        except Exception:                                  # room-making is a speed-up: any trouble and the call runs as before
            return
        self._aside.update(d["names"])
        self._srv_ctx = {}
        who = " and ".join(d["tiers"])
        self.say(f"    the {who} steps off the graphics card so {model} can use it ({d['why']}); it comes back when it is needed")
        self.report(f"- `{model}` needed the graphics card ({d['why']}): the {who} stepped aside for it.")
        try:                                               # a copy Ollama already split between card and RAM keeps that split: load it again
            import arch_ollama
            held = next((m for m in (arch_ollama.status(timeout=2).get("loaded") or []) if m.get("name") == model), None)
            if held and held.get("size") and (held.get("size_vram") or 0) < 0.95 * held["size"]:
                arch_ollama.unload(model)
        except Exception:
            pass

    def _ka(self):
        return ollama_keep_alive(self.cfg)

    def _note_ollama(self, model, host):
        """Remember a model this run sent to LOCAL Ollama (not llama-server, not a remote host, not the cloud): finish unloads exactly these."""
        if host or not model or str(model).startswith("claude:"):
            return
        try:
            import arch_transport
            if not isinstance(arch_transport.route(model, None, self.cfg), arch_transport.OllamaTransport):
                return
        except Exception:
            return
        used = self.__dict__.setdefault("ollama_used", set())
        if model not in used:
            used.add(model)
            with _OLLAMA_LOCK:
                _OLLAMA_USERS[model] = _OLLAMA_USERS.get(model, 0) + 1

    def _release_ollama(self):
        """End of run: unload (keep_alive 0) only the models THIS run loaded through Ollama, and only when no other Architect job holds the
        job lock and no other run in this process still uses that model. Another app's model is never in the list. -> names unloaded."""
        used = self.__dict__.pop("ollama_used", None) or set()
        if not used:
            return []
        with _OLLAMA_LOCK:
            for m in used:
                _OLLAMA_USERS[m] = max(0, _OLLAMA_USERS.get(m, 0) - 1)
            free = sorted(m for m in used if not _OLLAMA_USERS.get(m))
        try:
            import arch_cleanup
            if arch_cleanup._job_running():
                return []
        except Exception:
            pass
        gone = []
        try:
            import arch_ollama
            held = self._held_longer_elsewhere(arch_ollama)
            for m in free:
                if m in held:                             # someone else asked Ollama to keep it longer than we do: not ours to drop
                    continue
                if arch_ollama.unload(m):
                    gone.append(m)
        except Exception:
            return gone
        if gone:
            try:
                self.report(f"- Unloaded from Ollama at the end of the run: {', '.join('`%s`' % m for m in gone)}.")
            except Exception:
                pass
        return gone

    def _held_longer_elsewhere(self, arch_ollama):
        """Names Ollama will keep loaded clearly past what our own keep_alive asks: another client (another app, a benchmark, a chat
        window) requested a longer hold after our last call, so that model is in use elsewhere. Unknown expiry -> not counted."""
        ka = str(self._ka()).strip().lower()
        m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([smh]?)", ka)
        if not m:
            return set()
        secs = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
        out = set()
        try:
            loaded = arch_ollama.status(timeout=2).get("loaded") or []
        except Exception:
            return out
        now = time.time()
        for row in loaded:
            until = str(row.get("until") or "")
            try:
                t = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", until).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if secs < 0 or t - now > secs + 120:
                out.add(row.get("name"))
        return out

    def _bring_back(self, model=None, allow=0):
        """Our tiers that stepped aside come back: when a call needs one of them (`model` is a name it answers to), or at the end of the
        run (`model` None), so the machine ends as it began. The models they made room for leave Ollama first (arch_server.come_back).
        `allow` = calls in flight that may be ignored (the failed call whose fallback needs the worker)."""
        if model is None and sum(self._inflight.values()) <= allow:
            self._release_ollama()                        # the run is over (finish, or run_flow's exception path): what it loaded leaves
        aside = getattr(self, "_aside", None)
        if (not aside or (model is not None and not any(model in names for names in aside.values()))
                or sum(self._inflight.values()) > allow):
            return
        tiers = sorted(aside)
        aside.clear()
        try:
            import arch_server
            back = arch_server.come_back(self.cfg, say=self.say, owner=os.getpid(), run=self.run_id)
        except Exception:
            back = []
        self._srv_ctx = {}
        missing = [t for t in tiers if t not in back]
        if missing:
            self.say(f"[!] the {' and '.join(missing)} did not come back after stepping aside; the next run starts it again")
            self.report(f"- The {' and '.join(missing)} did not come back after stepping aside (see hub_data/server/*.log); the next run starts it again.")
        else:
            self.report(f"- The {' and '.join(tiers)} came back on the graphics card.")

    def _quoted_lines(self):
        """When the goal quotes exact text ('...' or "..."), the lines that contain it, with line numbers, from any project file. A big
        file's context gets cut to fit the window, and seen live (2026-11-04) the one line a fix had to change was cut: the model
        never saw it and its edit missed. These lines are always shown. -> text, or "" (cached per run)."""
        if getattr(self, "_quoted_cache", None) is not None:
            return self._quoted_cache
        out, used = [], 0
        try:
            quotes = [a or b for a, b in re.findall(r"'([^'\n]{12,160})'|\"([^\"\n]{12,160})\"", self.goal or "")]
            for q in quotes[:3]:
                for rel in list_files(self.root, self.cfg):
                    try:
                        lines = read_text(os.path.join(self.root, rel), 3_000_000)[0].splitlines()
                    except OSError:
                        continue
                    hits = [i for i, ln in enumerate(lines) if q in ln][:2]
                    for i in hits:
                        a, b = max(0, i - 4), min(len(lines), i + 5)
                        block = f"{rel}, lines {a + 1}-{b}:\n" + "\n".join(f"{n + 1}: {lines[n][:300]}" for n in range(a, b))
                        if used + len(block) > 3000:
                            break
                        out.append(block)
                        used += len(block)
            # No quotes (or nothing found): the request's RARE words in the files it names. 'the option with value xhigh in
            # hub_ui.html' -> the 1-4 lines of that 245 KB file that contain 'xhigh'. Seen live (2026-11-04): Claude was sent the
            # first 2% of hub_ui.html, never saw the line, and rightly refused to guess an edit.
            if not out:
                named = [f for f in list_files(self.root, self.cfg) if os.path.basename(f) in (self.goal or "")]
                words = {w for w in re.findall(r"[A-Za-z_][\w-]{3,40}", self.goal or "")
                         if w.lower() not in _COMMON_WORDS and not any(w in f for f in named)}
                for rel in named[:3]:
                    try:
                        lines = read_text(os.path.join(self.root, rel), 3_000_000)[0].splitlines()
                    except OSError:
                        continue
                    for w in sorted(words):
                        hits = [i for i, ln in enumerate(lines) if w in ln]
                        if not 1 <= len(hits) <= 4:                       # a word on every other line says nothing about where
                            continue
                        for i in hits:
                            a, b = max(0, i - 2), min(len(lines), i + 3)
                            block = f"{rel}, lines {a + 1}-{b} (contain '{w}'):\n" + "\n".join(f"{n + 1}: {lines[n][:600]}" for n in range(a, b))
                            if used + len(block) > 4000 or block in out:
                                continue
                            out.append(block)
                            used += len(block)
        except Exception:
            out = []
        self._quoted_cache = ("EXACT LINES THE REQUEST POINTS AT (copy SEARCH text from here, character for character):\n" + "\n\n".join(out)) if out else ""
        return self._quoted_cache

    def _unlocked(self, fn):
        """Run `fn` (the network wait) with the engine's lock released, then take it back. Under a single caller this changes nothing."""
        self._lock.release()
        try:
            return fn()
        finally:
            self._lock.acquire()

    def tier_of(self, model, role=None):
        """Which tier actually answered this call.

        A role's declared tier is only a default. A YAML step can pin a tier, and a stalled fix loop escalates to a bigger model
        mid-step. Training data keyed on the declared tier would credit a 14B's answer to the 7B worker, and the worker would then be
        trained to imitate replies it never produced -- so the tier is read back from the model that actually ran.
        """
        if str(model or "").startswith("claude:"):
            return "cloud"
        tiers = self.cfg.get("tiers") or {}
        for t in ("draft", "router", "worker", "heavy"):
            if tiers.get(t) and tiers[t] == model:
                return t
        return (arch_roles.ROLES.get(role) or {}).get("tier", "worker") if role else "worker"

    def _model_fp(self, model):
        """A stamp of the model's own bytes, worked out once per model per run. A re-pulled or re-quantized model has a different stamp,
        so the reply cache never hands out what the previous weights said. Empty when the file cannot be found: then the tag alone is the key."""
        if model in self._fp:
            return self._fp[model]
        fp = ""
        try:
            import arch_server
            p = ((arch_server.resolve_model(model, self.cfg) or {}).get("gguf")) or ""
            if p and os.path.isfile(p):
                st = os.stat(p)
                fp = f"{st.st_size}:{int(st.st_mtime)}"
        except Exception:
            fp = ""
        self._fp[model] = fp
        return fp

    def _prepare_server(self):
        """Once per run, before the first call: is Ollama up (plain message and a short wait if it is starting), and free memory held by models outside the team."""
        import arch_ollama
        import arch_server
        try:
            wanted = {m for m in (self.model_for(r) for r in arch_roles.ROLES) if m}   # only the tiers this run's roles will call
        except Exception:
            wanted = None                                                          # cannot tell: start what is planned, as before
        arch_server.ensure(self.cfg, say=self.say, wanted=wanted)                  # the resident llama-server, when installed and planned; else nothing happens
        if arch_server.slot_io("restore", self.cfg, os.path.abspath(self.root), slot=int(getattr(self, "slot_id", 0) or 0)):
            self.say("[server] this project's prompt memory restored from disk")
        self._srv_ctx = {}                                                         # windows read before the servers started are stale
        team = {m for m in (self.cfg.get("roles") or {}).values() if m} | {m for m in (self.cfg.get("tiers") or {}).values() if m} | ({self.cfg["default_model"]} if self.cfg.get("default_model") else set())
        served = {m for m in team if arch_server.serving(m, self.cfg)}
        if served and served >= {m for m in team if not m.startswith("tier:")}:
            self.report("- Model server: llama-server (" + ", ".join(sorted(served)) + " resident).")
            return
        st = arch_ollama.prepare(self.cfg, team, say=self.say)
        if not st["up"] and not st["port_open"]:
            arch_ollama.wait_up(seconds=8, say=self.say)
        if st.get("freed"):
            self.report(f"- Freed memory before the run: unloaded {', '.join(st['freed'])} (not part of this team).")

    def _gpu_check(self, model, num_ctx, host=None):
        """Is the loaded model fully on the GPU at this window? Checked once per model and window per run (`tune` measures it once; the window can grow
        mid-run). Spilling to the CPU is the biggest silent slowdown on local hardware. -> percent on GPU, or None when unknown."""
        if (model, num_ctx, host) in self.gpu_seen:
            return None
        self.gpu_seen.add((model, num_ctx, host))
        try:
            with urllib.request.urlopen((host or ollama_host()) + "/api/ps", timeout=4) as r:
                loaded = json.loads(r.read().decode()).get("models", [])
        except (urllib.error.URLError, OSError, ValueError):
            return None
        m = next((x for x in loaded if x.get("name") == model or x.get("model") == model), None)
        if not m or not m.get("size"):
            return None
        pct = round(100 * m.get("size_vram", 0) / m["size"])
        had_gpu = ((load_json(os.path.join(data_dir(), "profile.json")) or {}).get("hw") or {}).get("vram_gb") if not host else None
        if (0 < pct < 90) or (pct == 0 and had_gpu):
            self._ctx_note(f"[gpu] {model} is only {pct}% on the GPU at a {num_ctx}-token window: the rest runs on the CPU and generation will be much slower. "
                           "Lower Settings > Limits > Context size, or use a smaller quant")
        return pct

    def _ctx_note(self, msg):
        if msg not in self.ctx_events:
            self.ctx_events.append(msg)
            self.say("    ! " + msg)
            self.report("", f"> Context note: {msg}")

    def effective_context_chars(self):
        """Prompt budget for project context: the user's `context_chars`, but never more than the smallest window in use
        can hold once the instructions and a reply reserve are subtracted (using each model's measured chars/token)."""
        lim = self.cfg["limits"]
        base = int(lim["num_ctx"])
        ctxs, cpts = [], []
        models = {(m or "").strip() for r, m in self.cfg["roles"].items()                 # the router never gets project context
                  if (arch_roles.ROLES.get(r) or {}).get("tier") != "router"} - {""}
        if self.cfg.get("default_model"):
            models.add(self.cfg["default_model"])
        try:
            wm = self.tier_model("worker")                     # with the recommended team the roles are empty: the worker writes the code
        except Exception:
            wm = None
        if wm:
            models.add(wm)
        for m in models:
            w = max(int(self._model_override(m).get("num_ctx", base)), self.ctx_floor.get(m, 0))
            ctxs.append(min(w, self._served_ctx(m) or w))
            cpts.append(arch_tune.cpt_for(self.stats, m))
        return arch_tune.context_chars_for(min(ctxs or [base]), min(cpts or [arch_tune.DEFAULT_CPT]), lim["context_chars"])

    def _keep_off_card(self, model):
        """Should a model that Ollama (not our llama-server) answers stay entirely in RAM? Yes when our worker server is running with
        layers on the graphics card and server.ollama_gpu is "auto": seen 2026-11-05, Ollama loaded the heavy MoE 28% onto the card
        for the planner/evaluator roles, the plan then saw a full card and gave the worker 9 of its 36 layers, so every worker call ran
        mostly on the CPU. A mixture-of-experts model reads only its active experts per token, so it stays usable from RAM.
        server.ollama_gpu = "allow" restores Ollama's own choice. Cloud models and machines elsewhere are never affected."""
        if not hasattr(self, "_off_card"):
            self._off_card = {}
        if model not in self._off_card:
            keep = False
            try:
                moe = model == self.tier_model("heavy")
                if not moe:
                    try:
                        import arch_passport
                        moe = bool((arch_passport.load(model) or {}).get("moe"))
                    except Exception:
                        moe = False
                host = os.environ.get("OLLAMA_HOST", "127.0.0.1")
                local = any(h in host for h in ("127.0.0.1", "localhost", "0.0.0.0")) or ":" not in host and "." not in host
                # only a mixture-of-experts (or the heavy tier) on THIS machine's Ollama: a dense model would crawl from RAM, and a
                # model on another machine does not use this card (review 2026-11-05)
                if (str((self.cfg.get("server") or {}).get("ollama_gpu", "auto")) == "auto" and not str(model).startswith("claude")
                        and model != self.tier_model("worker") and moe and local):
                    import arch_server
                    rec = ((arch_server.load_state().get("processes") or {}).get("worker") or {})
                    keep = bool(arch_server.serving("tier:worker", self.cfg)) and int(rec.get("ngl") or 0) > 0
            except Exception:
                keep = False
            self._off_card[model] = keep
        return self._off_card[model]

    def _served_ctx(self, model):
        """The window a running llama-server gives `model` (fixed at its start), or None when no llama-server serves it."""
        if not hasattr(self, "_srv_ctx"):
            self._srv_ctx = {}
        if model not in self._srv_ctx:
            try:
                import arch_server
                self._srv_ctx[model] = int((arch_server.serving(model, self.cfg) or {}).get("ctx") or 0) or None
            except Exception:
                self._srv_ctx[model] = None
        return self._srv_ctx[model]

    def _model_override(self, model):
        mo = self.cfg["model_overrides"]
        return mo.get(model) or mo.get(model.lower()) or mo.get(model.split(":")[0]) or {}

    def _trace(self, rec):
        try:
            with open(os.path.join(self.run_dir, "trace.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as ex:
            self.trace_drops = getattr(self, "trace_drops", 0) + 1
            _tel_swallow("Engine._trace", ex)
        _tel_mirror(rec, getattr(self, "run_id", ""))
        emit_event(rec, getattr(self, "run_id", ""))

    def _provenance(self, model, host=None):
        """The first answer from a model in this run writes one "provenance" record (quantization, weight edit, base model, license: see
        model_provenance) and the same facts under run.json "provenance". Worked out once per model per run and cached here; best
        effort -- a missing or unreadable passport never touches the call, and nothing here can raise. host: the other machine that
        answered; its file cannot be checked from here, so no digest is claimed for it."""
        try:
            seen = self.__dict__.setdefault("_prov", {})
            if not model or model in seen:
                return
            seen[model] = None                              # taken first: a failure below is not retried on every later call
            rec = model_provenance(model)
            if host:
                rec = dict(rec, digest=None, host=host)
            seen[model] = rec
            self._trace(dict({"type": "provenance", "t": time.time()}, **rec))
            if isinstance(getattr(self, "state", None), dict):
                self.state.setdefault("provenance", {})[model] = rec
                self._write_state()
        except Exception:
            pass

    def mark(self, call_id, accepted, reason=""):
        """Label a past call with the outcome of the code checks. This is what makes training data trustworthy.

        Three outcomes, not two. True: the checks ran and passed. False: they ran and failed. None: they could not run at all -- no test
        command, no linter installed, a scanner that crashed. That is not a failure, and treating it as one is how a harness ends up
        teaching its own model that unverifiable work is bad work. A None mark is excluded from training in both directions.
        """
        self._trace({"type": "mark", "id": call_id, "accepted": (None if accepted is None else bool(accepted)),
                     "reason": reason, "t": time.time()})

    def ask_json(self, role, user, extra="", schema=None):
        """-> (reply, parsed or None). With a schema the server is told to constrain the reply, and the result is validated again here: an answer that
        breaks the schema is None, never a half-valid object."""
        r = self.call(role, user, extra, json_out=True, schema=schema)
        j = parse_json(r["text"])
        if schema is not None and j is not None:
            bad = arch_schemas.errors(j, schema)
            if bad:
                self.say("    [!] the reply did not match the expected shape: " + "; ".join(bad[:2]))
                j = None
        return r, j

    # --- file changes
    def apply(self, reply_text, label=""):
        """Parse a reply, show diffs, ask (review mode), write. -> list of result dicts."""
        blocks = parse_blocks(reply_text)
        lim = self.cfg["limits"]["max_files_per_step"]
        results = []
        self.last_apply_noop = False
        self.last_apply_errors = []
        if not blocks:
            self.say("[-] no FILE:/EDIT:/DEF: blocks in the reply")
            msg = "no FILE:/EDIT:/DEF: blocks found"
            if ASKS_RX.search((reply_text or "")[-400:]):
                msg += "; the reply asks a question instead of acting. Do not ask: act on the most reasonable reading and state what you assumed"
                self.say("    (the model asked a question instead of acting; it will be told to act)")
            self.last_apply_errors = [f"- reply: {msg}"]
            return [{"status": "error", "rel": "", "msg": msg}]
        # The per-step cap limits FILES, not blocks (several DEF blocks for one file are one file), and never drops a file the sprint
        # was planned to create. Before this, a laptop profile's cap of 2 silently discarded the rest of a 5-file package.
        norm = lambda x: posixpath.normpath(str(x or "").strip().strip("`'\"").replace("\\", "/")).lstrip("/")
        must = {norm(x) for x in (getattr(self, "sprint_files", None) or ())}
        keep, seen, lost = [], set(), []
        for b in blocks:
            rel = norm(b.get("path") or b.get("rel"))
            if rel in must or rel in seen or len(seen) < lim:
                keep.append(b)
                if rel not in must:
                    seen.add(rel)
            elif rel not in lost:
                lost.append(rel)
        dropped = len(blocks) - len(keep)
        for b in keep:
            p = prepare_block(self.root, self.cfg, b)
            if p["status"] == "ok" and any(p["rel"].startswith(d) for d in self.deny_prefixes):
                p.update(status="error", msg="this file is protected during this step (do not edit the safety-net tests)")
            if p["status"] == "ok":
                cut = self._shrink_problem(p)
                if cut and b.get("kind") == "file" and p["rel"].endswith(".py"):
                    merged = merge_partial(p.get("old") or "", p.get("new") or "")
                    if merged:                                  # only some functions were sent: apply them as function edits
                        p.update(new=merged, diff=unified(p.get("old") or "", merged, p["rel"]))
                        self.say(f"[i] {p['rel']}: the reply held only some functions; applied as function edits, every other line kept")
                        cut = self._shrink_problem(p)
                if cut:
                    p.update(status="error", msg=cut)
            if p["status"] == "error":
                self.say(f"[-] {p['rel']}: {p['msg']}")
                results.append(p)
                continue
            if p["status"] == "identical":
                self.say(f"[=] {p['rel']}: already in place")
                results.append(p)
                continue
            results.append(self._commit(p))
            if p["status"] == "quit":
                break
        self.last_apply_noop = bool(results) and all(r["status"] == "identical" for r in results)
        if self.last_apply_noop:
            self.say("[=] none of these edits changed anything on disk")
        if dropped:
            self.say(f"[i] {dropped} more block(s) ignored: max {lim} files per step")
            results.append({"status": "error", "rel": "", "msg": f"{len(lost)} file(s) were not changed ({', '.join(lost[:6])}): at most {lim} "
                            "files per step beyond the sprint's own files. Send those in your next reply."})
        # Every rejected block, said in one place: a reply where one block applied and another failed used to lose the failure silently
        # (the next prompt held only the test results), so the model resent the broken block unchanged. arch_progress.Coach reads this.
        self.last_apply_errors = [f"- {r.get('rel') or 'reply'}: {r.get('msg')}" for r in results if r.get("status") == "error" and r.get("msg")]
        self.report(f"- {label or 'change'}: " + ", ".join(f"`{r.get('rel')}` {r['status']}" for r in results))
        return results

    def _shrink_problem(self, p):
        """A reply that would delete most of an existing file is almost always a model that returned only the part it changed. -> message or None.
        Refactor and purge legitimately shrink files, so they are exempt; behavior.max_shrink_pct = 0 turns it off."""
        pct = int(self.cfg["behavior"].get("max_shrink_pct", 60) or 0)
        if not pct or self.flow in ("refactor", "purge") or not p.get("existed", True):
            return None
        old_n, new_n = len((p.get("old") or "").splitlines()), len((p.get("new") or "").splitlines())
        if old_n >= 25 and new_n < old_n * (100 - pct) / 100:
            one = " To change one function, send a DEF: block with just that function." if p["rel"].endswith(".py") else ""
            return (f"this would cut {p['rel']} from {old_n} to {new_n} lines. If you only meant to change part of it, use EDIT blocks; "
                    "otherwise send the COMPLETE file. Never reply with just the new section." + one)
        return None

    def _commit(self, p, quiet=False):
        """Show the diff, ask according to the review mode, write. Sets and returns p with its final status."""
        if not quiet:
            self.say(f"\n----- CHANGE: {p['rel']}{'' if p['existed'] else ' (new file)'}")
            self.say("\n".join("  " + ln for ln in p["diff"].splitlines()[:120]))
        decision = "yes" if quiet and self.review != "dry" else self._decide(p["rel"])
        if decision == "quit":
            p["status"] = "quit"
        elif decision != "yes":
            p["status"] = "declined"
            self.say(f"[-] {p['rel']}: not applied")
        else:
            self._backup(p)
            write_text(p["full"], p["new"], p["crlf"])
            self.changed_files.append(p["rel"])
            if getattr(self, "_retriever", None) is not None:
                self._retriever.touch()                        # the index re-embeds only the chunks this edit changed, before the next prompt
            p["status"] = "applied"
            if not quiet:
                self.say(f"[+] {p['rel']}: written")
        return p

    def put_file(self, rel, text, label="", quiet=False):
        """Write a whole (document) file through the same safety + review path as model edits.
        quiet=True: no diff and no per-file question (the caller already got approval for the whole set)."""
        p = prepare_block(self.root, self.cfg, {"kind": "file", "path": rel, "code": text.rstrip("\n") + "\n"})
        if p["status"] == "ok":
            p = self._commit(p, quiet)
        else:
            self.say(f"[{'=' if p['status'] == 'identical' else '-'}] {p['rel']}: {p['msg']}")
        self.report(f"- {label or 'write'}: `{p['rel']}` {p['status']}")
        return p

    def _decide(self, rel):
        if self.review == "dry":
            return "no"
        if self.review == "auto" or self.n_apply_all:
            return "yes"
        a = (self.ask(f"  Apply {rel}?  [y]es  [n]o  [a]ll remaining  [q]uit run > ") or "").strip().lower()
        if a in ("a", "all"):
            self.n_apply_all = True
            return "yes"
        if a in ("q", "quit"):
            return "quit"
        return "yes" if a in ("y", "yes") else "no"

    def _backup(self, p):
        if p["rel"] in self.backed_up:
            return
        self.backed_up.add(p["rel"])
        if p["existed"]:
            dst = os.path.join(self.run_dir, "backup", p["rel"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p["full"], dst)
        else:
            self.created.append(p["rel"])
            with open(os.path.join(self.run_dir, "created.txt"), "a", encoding="utf-8") as f:
                f.write(p["rel"] + "\n")

    def all_ok(self, results):
        return bool(results) and all(r["status"] in ("applied", "identical") for r in results)

    # --- context for prompts
    def retriever(self):
        """The hybrid (FTS5 functions + TF-IDF files) ranker for this run, built on first use (arch_index.py)."""
        if getattr(self, "_retriever", None) is None:
            import arch_index
            self._retriever = arch_index.Retriever(self.root, self.cfg, self.installed(), ollama_host(), read_text, say=self.say)
            if self._retriever.off_reason and self.persist_runs:
                self.report(f"- Retrieval: keyword ranking only ({self._retriever.off_reason})")
        return self._retriever

    def context(self, query, budget=None, always=()):
        """Project context: handoff + spec if present, then the most relevant files in full, the rest outlined."""
        budget = budget or self.effective_context_chars()
        files = list_files(self.root, self.cfg)
        # Prompt-cache stability (measured 2026-11-05 on 41 eval traces: 30-32% of local input reused). Within one run the context for
        # one (flow, step) is pinned: the memory lines, the practice cards and the file order are those of its first call, and while no
        # file changed the whole text is returned byte-identical (retries vary only the query's feedback, which goes at the prompt end).
        stable = bool((self.cfg.get("behavior") or {}).get("stable_context", True))
        g = str(getattr(self, "goal", "") or "")
        qkey = g if (g and str(query).startswith(g)) else str(query)[:160]   # retries append failure text to the goal; sprints differ early
        pin_key = (self.flow, self.step_name(), qkey, tuple(always))
        pins = self.__dict__.setdefault("_ctx_pins", {})
        pin = pins.setdefault(pin_key, {}) if stable else {}
        sig = None
        if stable:
            snap = self.__dict__.setdefault("_ctx_mtimes", {})
            sig = []
            for f in files:
                try:
                    st = os.stat(os.path.join(self.root, f))
                    sig.append((f, st.st_mtime_ns, st.st_size))
                except OSError:
                    sig.append((f, 0, -1))
                snap.setdefault(f, sig[-1][1:])
            sig = (tuple(sig), budget, tuple(always), os.environ.get("ARCHITECT_RETRY_HINT", ""))
            if pin.get("sig") == sig and pin.get("out") is not None:
                out = pin["out"]
                self._contexts = ([out] + [c for c in getattr(self, "_contexts", []) if c != out])[:6]
                return out
        parts, used = [], 0
        try_mem = None
        try:
            if stable and "mem" in self.__dict__.setdefault("_ctx_run", {}):
                try_mem = self._ctx_run["mem"]
            else:
                import arch_memory
                mcfg = self.cfg.get("memory") or {}
                try_mem, mem_used = arch_memory.for_prompt(self.root, int(mcfg.get("prompt_chars", 1800)), top=mcfg.get("top_lines"))   # project.md plus the lessons and traps that earned their place
                self.memory_used = sorted(set(getattr(self, "memory_used", []) + mem_used))
                if stable:
                    self._ctx_run["mem"] = try_mem
        except Exception:
            try_mem = None
        if try_mem:
            parts.append(f"=== what is known about this project ===\n{try_mem.strip()}")
            used += len(parts[-1])
        hint = os.environ.get("ARCHITECT_RETRY_HINT", "").strip()
        if hint:                                    # set by the supervisor for a retry: the failed attempt's own explanation (arch_postmortem)
            parts.append("=== an earlier attempt at this same task failed; its review (advice, check it against the code) ===\n" + hint[:900])
            used += len(parts[-1])
        sk_cfg = (self.cfg.get("skills") or {})
        try:                                                                  # skills: one page about one way of working, only when the task calls for it
            import arch_skills
            if "skills" in pin:                                               # re-picked per query, the cards changed on every retry
                sk_text, sk_used = pin["skills"], []
            else:
                sk_text, sk_used = ("", []) if not sk_cfg.get("enabled", True) else arch_skills.for_prompt(
                    query, self.root, files=files, tier=getattr(self, "current_tier", ""),
                    limit=int(sk_cfg.get("max", 2)), budget=min(int(sk_cfg.get("budget", 4000)), max(0, int(budget * 0.2))),
                    min_score=sk_cfg.get("min_score"))
                if stable:
                    pin["skills"] = sk_text
            if sk_text:
                parts.append(sk_text.strip())
                used += len(parts[-1])
            if sk_text and sk_used:
                self.skills_used = sorted(set(getattr(self, "skills_used", []) + sk_used))
                arch_skills.mark_used(sk_used)
        except Exception:
            pass
        for name in always:
            p = os.path.join(self.root, name)
            if os.path.isfile(p):
                t, _ = read_text(p, 20000)
                parts.append(f"=== {name} ===\n{t.strip()}")
                used += len(parts[-1])
        ranked, found = self.retriever().rank(query, files, rank_files(self.root, self.cfg, query, files))
        present = set(files)
        ranked = [f for f in ranked if f not in always and f in present]
        # Prompt-cache stability. The server reuses a prompt only up to its first changed byte, and re-ranking reshuffled this list
        # on every retry (measured: 14-46% of repeated input reused). Within a run, a query keeps its first order, new files go to the
        # end, and the files this run is editing go last, so everything before the first edited file is reused as-is.
        order = getattr(self, "_ctx_order", None)
        if order is None:
            order = self._ctx_order = {}
        prev = [f for f in (order.get(query) or (order.get(pin_key) if stable else None) or []) if f in ranked]
        ranked = prev + [f for f in ranked if f not in prev]
        editing = set(getattr(self, "changed_files", []) or []) | set(getattr(self, "created", []) or [])
        if stable:                                    # a file that changed on disk since the run first saw it is being edited too
            snap = self._ctx_mtimes
            editing |= {f for f, mt, sz in sig[0] if snap.get(f) != (mt, sz)}
        ranked = [f for f in ranked if f not in editing] + [f for f in ranked if f in editing]
        order[query] = list(ranked)
        if stable:
            order[pin_key] = list(ranked)
        shown, outlined = [], []
        for f in ranked:
            t, _ = read_text(os.path.join(self.root, f), 60000)
            if used + len(t) < budget * 0.85 and len(shown) < 8:
                parts.append(f"=== {f} ===\n{t.rstrip()}")
                used += len(parts[-1])
                shown.append(f)
            else:
                outlined.append(f)
        for f in outlined[:25]:
            p = os.path.join(self.root, f)
            fo = focus(p, query, max_chars=max(1500, int((budget - used) * 0.25)), boost=found.get(f, ())) if f.endswith(".py") and used < budget * 0.9 else ""
            if fo:
                parts.append(f"=== {f} (the parts relevant to this request; the rest as signatures) ===\n{fo}")
                used += len(parts[-1])
                continue
            o = outline(p)
            if o and used + len(o) <= budget:                   # outlines count against the budget too (they used to be added
                parts.append(f"=== {f} (outline only; not fully shown) ===\n{o}")   # without limit: 36k chars for a 4k request)
                used += len(parts[-1])
        if not files:
            parts.append("(the project folder has no source files yet)")
        out = "\n\n".join(parts)
        if stable:
            pin.update(sig=sig, out=out)
        self._contexts = ([out] + [c for c in getattr(self, "_contexts", []) if c != out])[:6]   # a prompt too big for its window cuts this first
        return out

    # --- undo
    def undo(self):
        return undo_run(self.root, self.run_dir)


_COMMON_WORDS = set("""about after again also because before being below between both change changed changes could does doing during each
effort file files from have here into keep label labelled like line lines make more most only other option over same should
show some such than that their them then there these they this those through under until very visible want were what when where
which while will with would your value values text read reads only change update rename should called named""".split())


EVENT_MARK = "\x1e"          # ASCII record separator: it cannot appear in the console text a person reads in the same stream, and no
                             # model reply can produce it, so the hub can tell events and output apart with no escaping scheme at all.
_EVENT_DROP = ("messages",)                                          # the full prompt is in the trace file; the live panel fetches it on demand
_EVENT_TRIM = {"reply": 500, "result": 500, "text": 500, "why": 300, "reason": 300}


def events_on():
    return os.environ.get("ARCHITECT_EVENTS") == "1"


def emit_event(rec, run_id=""):
    """Print one compact event line so a watching interface can follow a run as it happens, instead of re-reading a log.

    Off unless ARCHITECT_EVENTS=1, and even then it only ever ADDS a line to stdout: the ordinary human-readable output is untouched, so
    the command line behaves exactly the same whether anything is watching or not.
    """
    if not events_on() or not isinstance(rec, dict):
        return
    try:
        ev = {}
        for k, v in rec.items():
            if k in _EVENT_DROP:
                continue
            if k in _EVENT_TRIM and isinstance(v, str) and len(v) > _EVENT_TRIM[k]:
                ev[k] = v[:_EVENT_TRIM[k]] + " ..."
                ev[k + "_full_chars"] = len(v)
            else:
                ev[k] = v
        if run_id:
            ev.setdefault("run", run_id)
        sys.stdout.write(EVENT_MARK + json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    except Exception:
        pass                 # a watcher must never be able to break a run


def git_checkpoint(root, message, say=print):
    """Commit the project after a successful run when behavior.git == 'commit'. Best effort, never raises.
    Creates the repository if there is none; uses its own author so it works without git configuration."""
    import shutil as _sh
    if not _sh.which("git"):
        say("[i] git is not installed: skipping the checkpoint")
        return False
    try:
        if not os.path.isdir(os.path.join(root, ".git")):
            run_cmd(["git", "init", "-q"], root, 30, False)
        run_cmd(["git", "add", "-A"], root, 60, False)
        rc, out = run_cmd(["git", "-c", "user.name=Architect", "-c", "user.email=architect@localhost", "commit", "-q", "-m", message[:200]], root, 60, False)
        say("[git] checkpoint committed" if rc == 0 else "[git] nothing new to commit" if "nothing to commit" in out else "[git] could not commit: " + out[-120:])
        return rc == 0
    except Exception as ex:                                      # a checkpoint must never break a run
        say(f"[git] skipped: {ex}")
        return False


def undo_run(root, run_dir):
    n = 0
    bk = os.path.join(run_dir, "backup")
    if os.path.isdir(bk):
        for d, _, files in os.walk(bk):
            for f in files:
                src = os.path.join(d, f)
                rel = os.path.relpath(src, bk)
                shutil.copy2(src, os.path.join(root, rel))
                n += 1
    cr = os.path.join(run_dir, "created.txt")
    if os.path.isfile(cr):
        for rel in open(cr, encoding="utf-8").read().split("\n"):
            p = os.path.join(root, rel)
            if rel and os.path.isfile(p):
                os.remove(p)
                n += 1
    return n


def latest_run(root):
    d = os.path.join(root, ".arch", "runs")
    if not os.path.isdir(d):
        return None
    runs = sorted(os.listdir(d))
    return os.path.join(d, runs[-1]) if runs else None


def parse_json(text):
    """First JSON object or array in text, or None."""
    text = strip_think(text)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    cands = [m.group(1)] if m else []
    cands.append(text)
    for c in cands:
        for opener, closer in (("{", "}"), ("[", "]")):
            a, b = c.find(opener), c.rfind(closer)
            if a != -1 and b > a:
                try:
                    return json.loads(c[a:b + 1])
                except ValueError:
                    continue
    return None
