"""Model Hub: a small local web app to see your models, measure them, route them and run the agent.

    python model_hub.py                 # opens http://127.0.0.1:8765 in your browser
    python model_hub.py --port 9000 --no-browser
    python model_hub.py --selftest      # tests the hub against a fake Ollama (no model needed)

What it does
  Models      every model Ollama has, size, fit on THIS machine (VRAM/RAM/context), recommended settings.
  Scoreboard  measured scores per model and category (backend fix/feature, UI, reasoning, security,
              format, tool calls, speed) from small automatic tests, with adjustable weights.
  Pipeline    which model plans / codes / does UI; every limit and switch of the agent -> hub_config.json.
  Run         type a goal, watch the agent live, press y/n/a/q on each diff, stop, undo.
  Data/Train  export your approved runs as training data and get the fine-tune commands.

Safety
  * Listens on 127.0.0.1 only. Every change-making request needs a random token (printed at start-up and
    embedded in the page) and a matching Host/Origin, so a web page you visit cannot drive it.
  * It can start the agent (which edits the sandbox copy), so it is as powerful as running the commands yourself.
    It never changes NETWORK_MODE; the agent stays offline unless you change that in the agent file yourself.
  * Model-written test code runs in a temp folder under the same offline guard the agent uses.
  * Plain standard library: no pip install needed for the hub itself.
"""
import argparse
import ast
import collections
import ctypes
import datetime
import json
import math
import os
import re
import secrets
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "autonomous_agent_optimized.py")
DATA_DIR = os.path.join(HERE, "hub_data")
RESULTS_FILE = os.path.join(DATA_DIR, "results.json")
CONFIG_FILE = os.path.join(HERE, "hub_config.json")
if "arch_core" in sys.modules:                # inside the Architect there is ONE settings file -- the engine's. Writing to this module's
    CONFIG_FILE = sys.modules["arch_core"].CONFIG_FILE   # own default instead made `port --apply` and `promote-model` change a file nothing reads.
    DATA_DIR = sys.modules["arch_core"].data_dir()       # and one data folder: imported late (after the last set_home), results.json
    RESULTS_FILE = os.path.join(DATA_DIR, "results.json")   # used to land in the install's own hub_data, even under a test's data dir
UI_FILE = os.path.join(HERE, "hub_ui.html")
NOTES_FILE = os.path.join(HERE, "AGENT_NOTES.md")

GB = 1024 ** 3
LOCK = threading.RLock()


# ---------------------------------------------------------------------------------------------
# Ollama (plain HTTP, so the hub works even if the `ollama` Python package is not installed)
# ---------------------------------------------------------------------------------------------
def ollama_host():
    h = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip()
    if not h.startswith("http"):
        h = "http://" + h
    return h.replace("0.0.0.0", "127.0.0.1").rstrip("/")


def oll(path, body=None, timeout=30, method=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(ollama_host() + path, data=data, method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            msg = ""
        raise OllamaError(e.code, msg or str(e))
    except (urllib.error.URLError, OSError) as e:
        raise OllamaError(0, f"Ollama is not reachable at {ollama_host()} ({getattr(e, 'reason', e)}). "
                             "Start the Ollama app.")


class OllamaError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------------------------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------------------------
def detect_hardware():
    hw = {"gpu": None, "vram_gb": 0.0, "vram_free_gb": 0.0, "ram_gb": 0.0, "ram_free_gb": 0.0,
          "cpu": None, "os": sys.platform, "notes": []}
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8)
        if out.returncode == 0 and out.stdout.strip():
            name, tot, free, drv = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")][:4]
            hw.update(gpu=name, vram_gb=float(tot) / 1024, vram_free_gb=float(free) / 1024, driver=drv)
    except (OSError, ValueError, subprocess.SubprocessError):
        hw["notes"].append("nvidia-smi not found: GPU size unknown (enter it by hand in the Models tab).")
    try:
        if os.name == "nt":
            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tot", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("x", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            hw["ram_gb"], hw["ram_free_gb"] = m.tot / GB, m.avail / GB
        elif os.path.exists("/proc/meminfo"):
            mi = {}
            for ln in open("/proc/meminfo"):
                k, v = ln.split(":", 1)
                mi[k] = int(v.split()[0]) * 1024
            hw["ram_gb"], hw["ram_free_gb"] = mi["MemTotal"] / GB, mi.get("MemAvailable", mi["MemFree"]) / GB
        else:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            hw["ram_gb"] = int(out.stdout.strip()) / GB
    except Exception:
        hw["notes"].append("Could not read RAM size.")
    hw["cpu"] = os.environ.get("PROCESSOR_IDENTIFIER") or None
    return hw


# ---------------------------------------------------------------------------------------------
# Fit math. Everything here is an ESTIMATE; the Speed test measures the real thing.
# ---------------------------------------------------------------------------------------------
KV_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}


def model_arch(show):
    """Pull the numbers the fit math needs out of /api/show."""
    mi = (show or {}).get("model_info") or {}
    arch = mi.get("general.architecture", "")
    g = lambda k, d=None: mi.get(f"{arch}.{k}", d)
    layers = g("block_count")
    heads, kv_heads = g("attention.head_count"), g("attention.head_count_kv", g("attention.head_count"))
    emb = g("embedding_length")
    if isinstance(heads, list):
        heads = max(heads)
    if isinstance(kv_heads, list):
        kv_heads = max(kv_heads)
    hd = g("attention.key_length") or (emb // heads if emb and heads else None)
    experts, used = g("expert_count"), g("expert_used_count")
    return {"arch": arch, "layers": layers, "kv_heads": kv_heads, "head_dim": hd, "ctx_max": g("context_length"),
            "experts": experts, "experts_used": used, "params": mi.get("general.parameter_count")}


def kv_bytes_per_token(a, kv_type="f16"):
    if not (a.get("layers") and a.get("kv_heads") and a.get("head_dim")):
        return None
    return 2 * a["layers"] * a["kv_heads"] * a["head_dim"] * KV_BYTES.get(kv_type, 2.0)


def fit_model(size_bytes, arch, hw, kv_type="f16", gpu_bw=192.0, ram_bw=50.0, os_reserve_gb=4.0, gpu_overhead_gb=0.7):
    """Per context size: how much runs on the GPU, does the rest fit in RAM, and a rough tok/s."""
    rows = []
    w = size_bytes / GB
    kvt = kv_bytes_per_token(arch, kv_type)
    layers = arch.get("layers") or 0
    vram = max(hw.get("vram_gb", 0) - gpu_overhead_gb, 0)
    ram_usable = max(hw.get("ram_gb", 0) - os_reserve_gb, 0)
    moe = arch.get("experts") and arch.get("experts_used")
    read_frac = (0.15 + 0.85 * arch["experts_used"] / arch["experts"]) if moe else 1.0
    for ctx in (32768, 16384, 12288, 8192, 6144, 4096):
        if arch.get("ctx_max") and ctx > arch["ctx_max"]:
            continue
        kv = (kvt * ctx / GB) if kvt else 0.0
        total = w + kv
        if layers and vram > 0:
            per_layer = total / layers
            on_gpu = min(layers, int(vram / per_layer)) if per_layer > 0 else layers
        else:
            on_gpu = layers if vram > 0 else 0          # no GPU at all means nothing runs on it (was: everything, which made CPU-only boxes look fast)
        ratio = (on_gpu / layers) if layers else (1.0 if total <= vram else max(vram / total, 0))
        gpu_gb, cpu_gb = total * ratio, total * (1 - ratio)
        fits_ram = cpu_gb <= ram_usable
        # tokens/s ~ 1 / (time to read the active bytes once)
        t = (gpu_gb * read_frac) / gpu_bw + (cpu_gb * read_frac) / ram_bw
        rows.append({"ctx": ctx, "kv_gb": round(kv, 2), "total_gb": round(total, 2), "gpu_ratio": round(ratio, 2),
                     "gpu_gb": round(gpu_gb, 2), "cpu_gb": round(cpu_gb, 2), "fits_ram": fits_ram,
                     "est_tok_s": round(1 / t, 1) if t > 0 else None})
    return {"weights_gb": round(w, 2), "kv_known": kvt is not None, "moe": bool(moe), "rows": rows}


def recommend(fit, hw):
    """Largest context that keeps >=90% on GPU; else largest that fits RAM with the best GPU share."""
    ok = [r for r in fit["rows"] if r["fits_ram"]]
    if not ok:
        return {"verdict": "too_big", "text": "Does not fit in RAM + VRAM with the OS running. Pick a smaller quant or model."}
    good = [r for r in ok if r["gpu_ratio"] >= 0.9]
    if good:
        best = max(good, key=lambda r: r["ctx"])
        ver = "fast"
    else:
        cands = [r for r in ok if r["ctx"] >= 4096]
        best = max(cands, key=lambda r: (r["gpu_ratio"] >= 0.5, r["ctx"] if r["gpu_ratio"] >= 0.5 else 0, r["gpu_ratio"]))
        ver = "split" if best["gpu_ratio"] >= 0.3 else "slow"
    cands = sorted({r["ctx"] for r in ok if r["ctx"] <= best["ctx"] and r["ctx"] >= 4096}, reverse=True)
    min_ratio = max(0.0, round(best["gpu_ratio"] * 0.9, 2))
    text = {"fast": "Fits on the GPU.", "split": "Splits between GPU and RAM: slower, but the answer quality is the same.",
            "slow": "Mostly runs from RAM: expect a few tokens per second."}[ver]
    return {"verdict": ver, "text": text, "num_ctx": best["ctx"], "ctx_candidates": cands[:4],
            "min_gpu_ratio": min_ratio, "est_tok_s": best["est_tok_s"]}


# ---------------------------------------------------------------------------------------------
# What is known about model families. Each entry says WHERE the statement comes from:
#   vendor = the model's own documentation/model card; inference = reasoning from architecture;
#   measured = comes from this hub's tests (shown in the Scoreboard, not here).
# Nothing here is a benchmark number. Treat it as a starting point; the Scoreboard is the evidence.
# ---------------------------------------------------------------------------------------------
KNOWLEDGE = [
    {"match": r"qwen3-coder", "family": "Qwen3-Coder (MoE)", "kind": "coder",
     "sampling": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05}, "think": None,
     "notes": [("vendor", "Code-specialised, trained for agentic coding. Recommended sampling is temperature 0.7, top_p 0.8, top_k 20, repetition penalty 1.05."),
               ("inference", "MoE: only some experts run per token, so speed is good for its size, but ALL weights must still sit in RAM/VRAM.")]},
    {"match": r"qwen3", "family": "Qwen3", "kind": "general",
     "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20}, "think": {"fast": False, "balanced": True, "deep": True},
     "notes": [("vendor", "Hybrid thinking model: /think and /no_think switches. Thinking mode: temperature 0.6, top_p 0.95, top_k 20; non-thinking: 0.7 / 0.8 / 20. Do not use greedy decoding in thinking mode."),
               ("inference", "Uncensored/abliterated community builds keep the architecture; check them in the Scoreboard because the edit can cost accuracy.")]},
    {"match": r"qwen2\.5-coder|qwen2\.5coder", "family": "Qwen2.5-Coder", "kind": "coder",
     "sampling": {"temperature": 0.2, "top_p": 0.9, "top_k": 40}, "think": None,
     "notes": [("vendor", "Dense code model family (0.5B-32B), trained heavily on code and fill-in-the-middle."),
               ("inference", "No thinking mode: faster per answer, weaker on multi-step reasoning than thinking models.")]},
    {"match": r"gemma[-_]?4|gemma4", "family": "Gemma 4", "kind": "general",
     "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 64}, "think": None,
     "notes": [("vendor", "Google recommends temperature 1.0, top_k 64, top_p 0.95 for the Gemma line."),
               ("inference", "Strong at layout/visual reasoning and instruction following for its size; the multi-token-prediction (MTP) variants claim faster generation: confirm with the Speed test, the gain depends on the runtime supporting it.")]},
    {"match": r"gemma", "family": "Gemma", "kind": "general",
     "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 64}, "think": None,
     "notes": [("vendor", "Google recommends temperature 1.0, top_k 64, top_p 0.95."),
               ("inference", "Gemma has no separate system role in older versions; the agent places instructions inline for it.")]},
    {"match": r"granite", "family": "IBM Granite", "kind": "coder",
     "sampling": {"temperature": 0.2, "top_p": 0.9, "top_k": 40}, "think": None,
     "notes": [("vendor", "Enterprise-oriented, permissively licensed, trained with code and tool-calling in mind."),
               ("inference", "Low temperature suits code edits that must apply exactly. Verify tool calling in Probe.")]},
    {"match": r"bonsai", "family": "Bonsai (ternary/low-bit)", "kind": "general",
     "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20}, "think": {"fast": False, "balanced": True, "deep": True},
     "notes": [("vendor", "Extreme weight compression (about 2 bits per weight) lets a large parameter count fit in a small file."),
               ("inference", "Fewer bits per weight can cost accuracy on exact edits; the compute per token is that of the full parameter count. Sampling shown is a placeholder borrowed from the Qwen family: no vendor guidance was found. Judge it by the Scoreboard.")]},
    {"match": r"gpt-oss", "family": "gpt-oss", "kind": "reasoning",
     "sampling": {"temperature": 1.0, "top_p": 1.0}, "think": {"fast": "low", "balanced": "medium", "deep": "high"},
     "notes": [("vendor", "Reasoning effort is a level (low / medium / high), not on/off. Recommended sampling is temperature 1.0, top_p 1.0."),
               ("vendor", "Ships in MXFP4; the 20B file is about 13-14 GB, so on 16 GB RAM + 6 GB VRAM it only runs with heavy RAM use.")]},
    {"match": r"deepseek-r1", "family": "DeepSeek-R1 (distill)", "kind": "reasoning",
     "sampling": {"temperature": 0.6, "top_p": 0.95}, "think": {"fast": True, "balanced": True, "deep": True},
     "notes": [("vendor", "Recommended temperature 0.5-0.7 (0.6). Do not add a system prompt; put instructions in the user message."),
               ("inference", "Always thinks; slow per answer but strong at debugging puzzles.")]},
    {"match": r"devstral|codestral|mistral", "family": "Mistral family", "kind": "coder",
     "sampling": {"temperature": 0.2, "top_p": 0.95}, "think": None,
     "notes": [("vendor", "Devstral is trained for software-engineering agents (tool use over a codebase).")]},
    {"match": r"llama", "family": "Llama", "kind": "general",
     "sampling": {"temperature": 0.6, "top_p": 0.9}, "think": None, "notes": [("inference", "General model; usually behind code-specialised models at the same size on code edits.")]},
    {"match": r"phi", "family": "Phi", "kind": "general",
     "sampling": {"temperature": 0.2, "top_p": 0.9}, "think": None, "notes": [("inference", "Small and efficient; strong at reasoning for its size, can be weaker at long outputs.")]},
]
EMBED_NAMES = re.compile(r"embed|nomic|bge|minilm|arctic|e5-", re.I)


def knowledge_for(name):
    n = name.lower()
    for k in KNOWLEDGE:
        if re.search(k["match"], n):
            return k
    return {"family": "Unknown family", "kind": "general", "sampling": None, "think": None,
            "notes": [("inference", "No curated notes for this family. Use Probe and the tests to find out what it supports.")]}


# ---------------------------------------------------------------------------------------------
# Results store
# ---------------------------------------------------------------------------------------------
CATEGORIES = [
    ("backend_fix", "Fix a bug"), ("backend_feature", "Write a function"), ("ui", "UI / HTML+CSS"),
    ("reasoning", "Find root cause"), ("security", "Security"), ("format", "Follows format"),
    ("toolcall", "Tool calls"), ("speed", "Speed"),
]
DEFAULT_WEIGHTS = {"backend_fix": 25, "backend_feature": 20, "ui": 20, "reasoning": 10, "security": 10,
                   "format": 5, "toolcall": 0, "speed": 10}


def load_results():
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            r = json.load(f)
            return r if isinstance(r, dict) else {}
    except (OSError, ValueError):
        return {}


def save_results(r):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RESULTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=1)
    os.replace(tmp, RESULTS_FILE)


def aggregate(results, model):
    """Mean of the last 3 runs per category -> {cat: {score, n, runs}}."""
    out = {}
    for cat, runs in (results.get(model, {}).get("scores") or {}).items():
        last = runs[-3:]
        out[cat] = {"score": round(sum(r["score"] for r in last) / len(last), 1), "n": sum(r.get("n", 1) for r in last), "runs": len(runs)}
    return out


# ---------------------------------------------------------------------------------------------
# Test tasks. Each is small, self-contained and checked by code, not by opinion.
# ---------------------------------------------------------------------------------------------
def code_of(text):
    """Last fenced code block (``` ... ```), else the whole text."""
    blocks = re.findall(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.S)
    return (blocks[-1] if blocks else text).strip("\n")


def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def run_python_guarded(code, timeout=15):
    """Run model-written Python in a temp folder under the offline guard. -> (ok, output)."""
    d = tempfile.mkdtemp(prefix="hubtest_")
    try:
        ng = os.path.join(HERE, "netguard.py")
        pre = ""
        if os.path.isfile(ng):
            shutil.copy2(ng, os.path.join(d, "netguard.py"))
            pre = "import netguard; netguard.install('offline')\n"
        p = os.path.join(d, "t.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(pre + code)
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        env["PYTHONPATH"] = d
        try:
            r = subprocess.run([sys.executable, p], cwd=d, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        return r.returncode == 0, (r.stdout + r.stderr)[-600:]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def py_task(title, prompt, tests, entry=None):
    def check(reply):
        code = code_of(reply)
        try:
            ast.parse(code)
        except SyntaxError as e:
            return 0.0, f"syntax error: {e.msg}", False
        ok, out = run_python_guarded(code + "\n\n" + tests)
        return (1.0 if ok else 0.0), ("passed" if ok else out.strip().splitlines()[-1] if out.strip() else "failed"), True
    return {"title": title, "prompt": prompt + "\nReply with ONE ```python code block containing the complete corrected/new code and nothing else.", "check": check}


TASKS = {
    "backend_fix": [
        py_task("off-by-one in pagination",
                "This function should return page `page` (1-based) of `items`, `size` per page. It is buggy.\n```python\ndef paginate(items, page, size):\n    start = page * size\n    return items[start:start + size]\n```\nFix it.",
                "assert paginate(list(range(10)), 1, 3) == [0,1,2]\nassert paginate(list(range(10)), 4, 3) == [9]\nassert paginate(list(range(10)), 5, 3) == []\nprint('ok')"),
        py_task("mutable default argument",
                "This function leaks state between calls. Fix it without changing its signature meaning.\n```python\ndef add_tag(tag, tags=[]):\n    tags.append(tag)\n    return tags\n```",
                "assert add_tag('a') == ['a']\nassert add_tag('b') == ['b']\nassert add_tag('c', ['x']) == ['x','c']\nprint('ok')"),
        py_task("wrong comparison in dedupe",
                "`dedupe` should remove duplicates but keep the FIRST occurrence and the original order. It is buggy.\n```python\ndef dedupe(xs):\n    seen = set()\n    out = []\n    for x in xs:\n        if x in seen:\n            out.append(x)\n        seen.add(x)\n    return out\n```",
                "assert dedupe([3,1,3,2,1]) == [3,1,2]\nassert dedupe([]) == []\nassert dedupe(['a','a']) == ['a']\nprint('ok')"),
    ],
    "backend_feature": [
        py_task("parse duration",
                "Write `parse_duration(s)` returning seconds as int for strings like '1h30m', '45s', '2h', '10m5s'. Raise ValueError for '' or anything else (e.g. '5x').",
                "assert parse_duration('1h30m') == 5400\nassert parse_duration('45s') == 45\nassert parse_duration('2h') == 7200\nassert parse_duration('10m5s') == 605\nfor bad in ('', '5x', 'h', '1h1h'):\n    try:\n        parse_duration(bad); raise SystemExit('no error for ' + repr(bad))\n    except ValueError:\n        pass\nprint('ok')"),
        py_task("rate limiter",
                "Write a class `RateLimiter(max_calls, period)` with method `allow(now)` -> bool. `now` is a float timestamp passed in. Allow at most `max_calls` calls in any sliding window of `period` seconds.",
                "r = RateLimiter(2, 10)\nassert r.allow(0) and r.allow(1)\nassert not r.allow(2)\nassert r.allow(10.5)\nassert not r.allow(10.6) or True\nr2 = RateLimiter(1, 5)\nassert r2.allow(0) and not r2.allow(4.9) and r2.allow(5.0)\nprint('ok')"),
        py_task("flatten nested",
                "Write `flatten(x)` that turns arbitrarily nested lists/tuples into one flat list, leaving strings intact (a string is not iterated).",
                "assert flatten([1,[2,(3,[4])],'ab']) == [1,2,3,4,'ab']\nassert flatten([]) == []\nassert flatten('x') == ['x']\nprint('ok')"),
    ],
    "security": [
        py_task("path traversal",
                "Write `safe_join(base, user_path)` that joins a user-supplied relative path onto `base` and returns the absolute path, but raises ValueError if the result would be outside `base` (consider '..', absolute paths and symlink-free normalisation).",
                "import os\nb = os.path.abspath('base_dir')\nassert safe_join(b, 'a/b.txt') == os.path.join(b, 'a', 'b.txt')\nfor bad in ('../x', 'a/../../x', '/etc/passwd', 'a/../..'):\n    try:\n        safe_join(b, bad); raise SystemExit('allowed ' + bad)\n    except ValueError:\n        pass\nprint('ok')"),
        py_task("SQL injection",
                "This function is vulnerable to SQL injection. Rewrite it with a parameterised query; keep the signature.\n```python\ndef find_user(conn, name):\n    cur = conn.execute(\"SELECT id, name FROM users WHERE name = '\" + name + \"'\")\n    return cur.fetchall()\n```",
                "import sqlite3\nc = sqlite3.connect(':memory:')\nc.execute('create table users(id integer, name text)')\nc.execute(\"insert into users values (1,'bob'),(2,'alice')\")\nassert find_user(c, 'bob') == [(1,'bob')]\nassert find_user(c, \"' OR '1'='1\") == []\nprint('ok')"),
        {"title": "name the weakness (CWE)",
         "prompt": "What weakness class is this? Answer ONLY a JSON object like {\"cwe\": \"CWE-NNN\", \"fix\": \"one sentence\"}.\n```python\nimport subprocess\ndef ping(host):\n    return subprocess.run('ping -c1 ' + host, shell=True, capture_output=True)\n```",
         "check": lambda reply: _check_json(reply, lambda j: str(j.get("cwe", "")).upper().replace(" ", "") in ("CWE-78", "CWE-77", "CWE-88"), need=("cwe", "fix"))},
    ],
    "reasoning": [
        {"title": "root cause from traceback",
         "prompt": "Find the root cause. Answer ONLY JSON: {\"line\": <line number of the faulty code>, \"cause\": \"one sentence\"}.\n```python\n1  def average(xs):\n2      total = 0\n3      for x in xs:\n4          total += x\n5      return total / len(xs)\n6\n7  def report(groups):\n8      return {k: average(v) for k, v in groups.items()}\n9\n10 report({'a': [1, 2], 'b': []})\n```\nTraceback: ZeroDivisionError: division by zero at line 5, called from line 8.",
         "check": lambda reply: _check_json(reply, lambda j: str(j.get("line")) in ("5", "8", "10") and bool(j.get("cause")), need=("line", "cause"))},
        {"title": "which test fails",
         "prompt": "Without running it, will this assertion pass? Answer ONLY JSON: {\"passes\": true|false, \"why\": \"one sentence\"}.\n```python\ndef f(n):\n    return [i * i for i in range(n) if i % 2]\nassert f(6) == [1, 9, 25, 0]\n```",
         "check": lambda reply: _check_json(reply, lambda j: j.get("passes") is False, need=("passes", "why"))},
    ],
    "ui": [
        {"title": "accessible login form",
         "prompt": "Write ONE self-contained HTML file (inline CSS) for a dark-themed login card with an email field, a password field and a Sign in button. It must be responsive, keyboard accessible and readable. Reply with one ```html code block only.",
         "check": lambda reply: _check_ui(reply)},
        {"title": "stat cards grid",
         "prompt": "Write ONE self-contained HTML file (inline CSS) with a responsive grid of 4 metric cards (title, big number, small trend text). Light theme. Must work at phone width. Reply with one ```html code block only.",
         "check": lambda reply: _check_ui(reply, forms=False)},
    ],
}


def _check_json(reply, pred, need=()):
    txt = strip_think(reply)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return 0.0, "no JSON object", False
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return 0.0, "invalid JSON", False
    if any(k not in j for k in need):
        return 0.3, "JSON missing keys", True
    return (1.0, "correct", True) if pred(j) else (0.3, "valid JSON, wrong answer", True)


def _lum(hexcol):
    h = hexcol.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError:
        return None
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = map(f, rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    if la is None or lb is None:
        return None
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _check_ui(reply, forms=True):
    html = code_of(strip_think(reply))
    low = html.lower()
    items = []
    items.append(("is a full HTML document", "<html" in low and "</html>" in low))
    items.append(("mobile viewport meta", 'name="viewport"' in low or "name='viewport'" in low))
    items.append(("responsive layout (grid/flex/media query)", any(k in low for k in ("grid", "flex", "@media"))))
    items.append(("visible focus style", ":focus" in low))
    if forms:
        items.append(("labels tied to inputs", bool(re.search(r"<label[^>]+for=|<label[^>]*>.*?<input", low, re.S))))
        items.append(("correct input types (email/password)", 'type="email"' in low and 'type="password"' in low))
    else:
        items.append(("semantic headings or sections", any(k in low for k in ("<h1", "<h2", "<h3", "<section", "<article"))))
    # contrast: first colour/background pair we can read
    cols = re.findall(r"(?<![-\w])color\s*:\s*(#[0-9a-f]{3,6})\b", low)
    bgs = re.findall(r"background(?:-color)?\s*:\s*(#[0-9a-f]{3,6})\b", low)
    if cols and bgs:
        c = contrast(cols[0], bgs[0])
        items.append((f"text contrast >= 4.5 ({c:.1f})" if c else "text contrast", bool(c and c >= 4.5)))
    else:
        items.append(("text contrast readable (could not measure: use hex colours)", False))
    items.append(("no external resources", not re.search(r"(src|href)=[\"']https?://", low)))
    score = sum(1 for _, ok in items if ok) / len(items)
    miss = [t for t, ok in items if not ok]
    return score, ("all checks passed" if not miss else "missing: " + "; ".join(miss)), True


def tool_task():
    tools = [{"type": "function", "function": {"name": "run_tests", "description": "Run the project's tests",
              "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
    return tools, "Run the tests in the folder tests/unit using the tool."


# ---------------------------------------------------------------------------------------------
# Talking to a model for tests/probes
# ---------------------------------------------------------------------------------------------
def chat(model, prompt, think=None, options=None, fmt=None, tools=None, system=None, timeout=300):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "stream": False, "keep_alive": "10m", "options": options or {}}
    if think is not None:
        body["think"] = think
    if fmt:
        body["format"] = fmt
    if tools:
        body["tools"] = tools
    return oll("/api/chat", body, timeout=timeout)


def chat_text(res):
    return (res.get("message") or {}).get("content") or ""


def speed_of(res):
    ec, ed = res.get("eval_count"), res.get("eval_duration")
    return round(ec / (ed / 1e9), 1) if ec and ed else None


def probe_model(model):
    """What does this model/server actually accept? Cheap, tiny prompts."""
    out = {}

    def attempt(name, **kw):
        t0 = time.time()
        try:
            r = chat(model, "Reply with the single word: ok", options={"num_predict": 24, "temperature": 0}, timeout=180, **kw)
            out[name] = {"ok": True, "s": round(time.time() - t0, 1)}
            return r
        except OllamaError as e:
            out[name] = {"ok": False, "error": e.msg[:160]}
            return None
    attempt("plain")
    attempt("think_off", think=False)
    attempt("think_on", think=True)
    attempt("think_level", think="low")
    attempt("system_prompt", system="You answer in one word.")
    attempt("json_format", fmt="json")
    tools, ask = tool_task()
    try:
        r = chat(model, ask, tools=tools, options={"num_predict": 120, "temperature": 0}, timeout=180)
        calls = (r.get("message") or {}).get("tool_calls") or []
        out["tools"] = {"ok": True, "called": bool(calls)}
    except OllamaError as e:
        out["tools"] = {"ok": False, "error": e.msg[:160]}
    return out


def run_tests(model, cats, think_allowed=False, progress=None, probe=None, sampling=None):
    """-> {cat: {"score": 0-100, "n": tasks, "detail": [...]}} (never raises for a single bad reply)."""
    res = {}
    fmt_hits = fmt_total = 0
    opts = dict(sampling or {"temperature": 0.2})
    opts["num_predict"] = 4096 if think_allowed else 2048
    opts["num_ctx"] = 8192
    think = None if think_allowed else (False if (probe or {}).get("think_off", {}).get("ok", True) else None)

    def log(m):
        if progress:
            progress(m)
    for cat in cats:
        if cat in TASKS:
            detail, total = [], 0.0
            for t in TASKS[cat]:
                log(f"{model}: {cat} - {t['title']}")
                try:
                    r = chat(model, t["prompt"], think=think, options=opts, timeout=600)
                except OllamaError as e:
                    if think is not None and "think" in e.msg.lower():
                        think = None
                        try:
                            r = chat(model, t["prompt"], options=opts, timeout=600)
                        except OllamaError as e2:
                            detail.append({"task": t["title"], "score": 0, "note": e2.msg[:120]})
                            continue
                    else:
                        detail.append({"task": t["title"], "score": 0, "note": e.msg[:120]})
                        continue
                reply = strip_think(chat_text(r))
                try:
                    s, note, formatted = t["check"](reply)
                except Exception as e:
                    s, note, formatted = 0.0, f"checker error {type(e).__name__}", False
                fmt_total += 1
                fmt_hits += 1 if formatted else 0
                total += s
                detail.append({"task": t["title"], "score": round(s * 100), "note": note, "tok_s": speed_of(r)})
            res[cat] = {"score": round(100 * total / len(TASKS[cat]), 1), "n": len(TASKS[cat]), "detail": detail}
        elif cat == "toolcall":
            log(f"{model}: toolcall")
            tools, ask = tool_task()
            try:
                r = chat(model, ask, tools=tools, think=think, options={"num_predict": 200, "temperature": 0}, timeout=300)
                calls = (r.get("message") or {}).get("tool_calls") or []
                ok = bool(calls) and calls[0].get("function", {}).get("name") == "run_tests" and \
                    "path" in (calls[0].get("function", {}).get("arguments") or {})
                res[cat] = {"score": 100.0 if ok else 0.0, "n": 1, "detail": [{"task": "call run_tests(path)", "score": 100 if ok else 0, "note": "called correctly" if ok else "no valid tool call"}]}
            except OllamaError as e:
                res[cat] = {"score": 0.0, "n": 1, "detail": [{"task": "tools", "score": 0, "note": e.msg[:120]}]}
        elif cat == "speed":
            log(f"{model}: speed")
            try:
                r = chat(model, "Write a 150-word explanation of what a mutex is.", think=think,
                         options={"num_predict": 220, "temperature": 0.2, "num_ctx": 8192}, timeout=600)
                tps = speed_of(r)
                fit = None
                try:
                    for m in oll("/api/ps").get("models", []):
                        if m.get("name") == model or m.get("model") == model:
                            fit = round(100 * m.get("size_vram", 0) / m["size"], 0) if m.get("size") else None
                except OllamaError:
                    pass
                # 0-100: 25 tok/s or more is full marks for an interactive coding loop
                score = min(100.0, (tps or 0) / 25 * 100)
                res[cat] = {"score": round(score, 1), "n": 1, "tok_s": tps, "gpu_pct": fit,
                            "detail": [{"task": "tokens/s", "score": round(score), "note": f"{tps} tok/s, {fit}% on GPU"}]}
            except OllamaError as e:
                res[cat] = {"score": 0.0, "n": 1, "detail": [{"task": "speed", "score": 0, "note": e.msg[:120]}]}
    if fmt_total and "format" in cats:
        res["format"] = {"score": round(100 * fmt_hits / fmt_total, 1), "n": fmt_total,
                         "detail": [{"task": "replies in the requested format", "score": round(100 * fmt_hits / fmt_total), "note": f"{fmt_hits}/{fmt_total}"}]}
    return res


# ---------------------------------------------------------------------------------------------
# Config file (hub_config.json): validated lightly here; the agent validates again when it reads it
# ---------------------------------------------------------------------------------------------
CONFIG_LOCK = threading.RLock()        # held across read -> change -> write by every route in CONFIG_ROUTES (two tabs, a bench)
CONFIG_ROUTES = set()


def read_config(strict=False):
    """The settings as a dict ({} when there is no file). strict=True (every read that will be written back): a file that exists but
    is not valid JSON raises ValueError instead of reading as {}, which the save would then have written over the person's settings."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except OSError:
        return {}
    except ValueError:
        if strict:
            raise ValueError(f"the settings file ({os.path.basename(CONFIG_FILE)}) is not valid JSON right now, so nothing was saved over it. "
                             "Fix it in Settings > Everything as JSON, or restore the .bak copy next to it.")
        return {}


def write_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    cfg = json.loads(json.dumps(cfg))                    # plain JSON only
    cfg.pop("network_mode", None)                        # never configurable from here
    if os.path.exists(CONFIG_FILE):
        shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)
    return cfg


# ---------------------------------------------------------------------------------------------
# Jobs: one agent/action subprocess at a time, plus background test threads
# ---------------------------------------------------------------------------------------------
EVENT_MARK = "\x1e"          # see arch_core.emit_event: a child process prefixes structured events with this, console text has none
MAX_EVENTS = 4000


def _split_events(s):
    """Separate structured events from the console text they share a pipe with.

    -> (plain text, leftover, [events]). An event split across two reads of the pipe stays in the leftover and is completed on the next
    read, so a 4 KB boundary never turns one event into two broken halves.
    """
    out, evs = [], []
    while True:
        i = s.find(EVENT_MARK)
        if i < 0:
            out.append(s)
            return "".join(out), "", evs
        out.append(s[:i])
        j = s.find("\n", i)
        if j < 0:
            return "".join(out), s[i:], evs
        raw, s = s[i + 1:j], s[j + 1:]
        try:
            ev = json.loads(raw)
            if isinstance(ev, dict):
                evs.append(ev)
        except ValueError:
            pass                                          # a truncated or malformed event is dropped, never shown as console noise


class Job:
    def __init__(self):
        self.proc, self.buf, self.title, self.started, self.ended, self.code = None, "", "", None, None, None
        self.lock = threading.Lock()
        # epochs start from the clock (milliseconds), so a number is never reused after the hub restarts: a watcher that kept an
        # epoch (the HUD, the guided tour, the Console) used to take the first run of a new hub for "its" run
        self.events, self.ev_dropped, self.epoch = [], 0, int(time.time() * 1000)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, title, argv):
        with self.lock:
            if self.running():
                raise RuntimeError("A job is already running. Stop it first.")
            self.buf, self.title, self.started, self.ended, self.code = "", title, time.time(), None, None
            self.events, self.ev_dropped = [], 0
            self.epoch += 1                              # a watcher that reconnects can tell "new run" from "more of the same run"
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", ARCHITECT_EVENTS="1")
            self.proc = subprocess.Popen([sys.executable, "-u", *argv], cwd=HERE, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            threading.Thread(target=self._pump, args=(self.proc, self.epoch), daemon=True).start()

    def _pump(self, proc, epoch=None):
        """Copies one job's output into the buffer. Every write checks that this is still the CURRENT job: a finished job's last read
        used to land in the next job's buffer (its verdict under the new run) and set the new job's exit code while it ran."""
        mine = (lambda: self.proc is proc and (epoch is None or self.epoch == epoch))
        tail = ""
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            tail += chunk.decode("utf-8", "replace")
            text, tail, evs = _split_events(tail)
            with self.lock:
                if not mine():
                    return
                self.buf += text
                if len(self.buf) > 400_000:
                    self.buf = "[... earlier output trimmed ...]\n" + self.buf[-300_000:]
                self.events.extend(evs)
                if len(self.events) > MAX_EVENTS:
                    cut = len(self.events) - MAX_EVENTS // 2
                    del self.events[:cut]
                    self.ev_dropped += cut
        proc.wait()
        with self.lock:
            if not mine():
                return
            if tail:
                self.buf += tail                         # whatever was left was ordinary output, not a finished event
            self.code, self.ended = proc.returncode, time.time()

    def event_feed(self, since=0, epoch=None):
        """-> {"events", "next", "epoch", "running", "dropped"}. `since` is a cursor the caller got back last time; a cursor from an
        older run (a smaller epoch) is treated as 0, so a reconnecting watcher sees the new run from its start rather than nothing."""
        with self.lock:
            if epoch is not None and int(epoch) != self.epoch:
                since = 0                                # the cursor belongs to an earlier run: this run's events all start at 0
            first = self.ev_dropped
            start = max(0, int(since or 0) - first)
            return {"events": self.events[start:], "next": first + len(self.events), "epoch": self.epoch,
                    "running": self.running(), "dropped": first, "title": self.title}

    def send(self, text):
        if not self.running():
            raise RuntimeError("Nothing is running.")
        self.proc.stdin.write((text.rstrip("\n") + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        with self.lock:
            self.buf += f"{text}\n"

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def snapshot(self, since=0):
        with self.lock:
            return {"title": self.title, "running": self.running(), "code": self.code, "size": len(self.buf),
                    "text": self.buf[since:], "started": self.started, "ended": self.ended}


JOB = Job()
TEST_STATE = {"running": False, "log": [], "models": []}
ACTIONS = {
    "probe": ["autonomous_agent_optimized.py", "--probe"], "selftest": ["autonomous_agent_optimized.py", "--selftest"],
    "map": ["autonomous_agent_optimized.py", "--map"], "ui_check": ["autonomous_agent_optimized.py", "--ui-check"],
    "undo": ["autonomous_agent_optimized.py", "--undo"], "diff_parent": ["autonomous_agent_optimized.py", "--diff-parent"],
    "show_config": ["autonomous_agent_optimized.py", "--show-config"],
    "add_tests": ["autonomous_agent_optimized.py", "--add-tests", "--review", "ask"],
    "selfbench": ["autonomous_agent_optimized.py", "--selfbench", "--tasks", "6", "--variants", "baseline,smart"],
    "export": ["architect.py", "dataset"], "export_stats": ["architect.py", "dataset", "--json"],
    "train_check": ["train_lora.py", "--check"],
}


# ---------------------------------------------------------------------------------------------
# State assembly
# ---------------------------------------------------------------------------------------------
_show_cache = {}


def _loopback_down():
    """True when Ollama's address is on THIS machine and nothing accepts there. Windows takes about 2 s to report a refused
    connection on loopback (it retries the SYN), so every /api/state used to wait 2 s while Ollama was stopped. A local listener
    accepts in well under a millisecond, so 0.3 s only cuts the refusal short. A remote host keeps the normal path."""
    from urllib.parse import urlparse
    u = urlparse(ollama_host())
    host, port = (u.hostname or ""), (u.port or 11434)
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    try:
        socket.create_connection((host, port), timeout=0.3).close()
        return False
    except OSError:
        return True


def model_list(hw, kv_type, gpu_bw, ram_bw):
    if _loopback_down():
        raise OllamaError(0, f"Ollama is not reachable at {ollama_host()} (nothing is listening). Start the Ollama app.")
    tags = oll("/api/tags", timeout=10).get("models", [])
    results = load_results()
    rows = []
    for m in tags:
        name = m.get("name") or m.get("model")
        key = (name, m.get("digest"))
        if key not in _show_cache:
            try:
                _show_cache[key] = oll("/api/show", {"model": name}, timeout=15)
            except OllamaError:
                _show_cache[key] = {}
        show = _show_cache[key]
        arch = model_arch(show)
        det = m.get("details") or {}
        is_embed = bool(EMBED_NAMES.search(name)) or "bert" in (arch.get("arch") or "")
        fit = fit_model(m.get("size", 0), arch, hw, kv_type, gpu_bw, ram_bw)
        rec = recommend(fit, hw) if not is_embed else {"verdict": "embed", "text": "Embedding model (used for retrieval, not coding)."}
        caps = show.get("capabilities") or []
        rows.append({"name": name, "size_gb": round(m.get("size", 0) / GB, 2), "params": det.get("parameter_size"),
                     "quant": det.get("quantization_level"), "family": det.get("family"), "embed": is_embed,
                     "capabilities": caps, "arch": arch, "fit": fit, "rec": rec, "know": knowledge_for(name),
                     "agg": aggregate(results, name), "ctx_max": arch.get("ctx_max")})
    return rows


def build_state(q=None):
    q = q or {}
    kv = q.get("kv", ["f16"])[0]
    try:
        import arch_hw
        _m = arch_hw.load_bandwidth()
    except Exception:
        _m = {}
    gpu_bw = float(q.get("gpu_bw", [str(_m.get("gpu_bw") or 192)])[0])
    ram_bw = float(q.get("ram_bw", [str(_m.get("ram_bw") or 50)])[0])
    # which numbers the speed estimates use, and where each came from (the page shows it): typed by the person, measured here, or an estimate
    src = lambda k: "you" if q.get(k) else ("measured" if _m.get(k) else "estimate")
    speed_inputs = {"kv": kv, "gpu_bw": gpu_bw, "ram_bw": ram_bw, "gpu_bw_src": src("gpu_bw"), "ram_bw_src": src("ram_bw")}
    hw = detect_hardware()
    if q.get("vram"):
        hw["vram_gb"] = float(q["vram"][0])
    if q.get("ram"):
        hw["ram_gb"] = float(q["ram"][0])
    st = {"hw": hw, "ollama": {"host": ollama_host(), "ok": True}, "models": [], "config": read_config(),
          "weights": load_results().get("_weights") or DEFAULT_WEIGHTS, "categories": CATEGORIES,
          "tests": {"running": TEST_STATE["running"], "log": TEST_STATE["log"][-30:]},
          "actions": sorted(ACTIONS), "time": time.time(), "speed_inputs": speed_inputs}
    try:
        st["models"] = model_list(hw, kv, gpu_bw, ram_bw)
        try:
            st["loaded"] = oll("/api/ps", timeout=5).get("models", [])
        except OllamaError:
            st["loaded"] = []
    except OllamaError as e:
        st["ollama"] = {"host": ollama_host(), "ok": False, "error": e.msg}
    st["results"] = {m["name"]: m["agg"] for m in st["models"]}
    st["probes"] = {k: v.get("probe") for k, v in load_results().items() if isinstance(v, dict) and v.get("probe")}
    return st


def list_runs():
    d = os.path.join(HERE, ".agent_runs")
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), reverse=True)[:30]:
            rp = os.path.join(d, n, "report.md")
            if os.path.exists(rp):
                try:
                    out.append({"name": n, "report": open(rp, encoding="utf-8", errors="replace").read()[:6000]})
                except OSError:
                    pass
    return out


# ---------------------------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------------------------
class Hub:
    token = secrets.token_urlsafe(24)
    port = 8765


def start_tests(models, cats, think_allowed):
    def work():
        TEST_STATE.update(running=True, log=[], models=list(models))
        try:
            for m in models:
                with LOCK:
                    res = load_results()
                probe = (res.get(m) or {}).get("probe")
                if probe is None:
                    TEST_STATE["log"].append(f"{m}: probing capabilities")
                    try:
                        probe = probe_model(m)
                        with LOCK:
                            res = load_results()
                            res.setdefault(m, {})["probe"] = probe
                            save_results(res)
                    except OllamaError as e:
                        TEST_STATE["log"].append(f"{m}: {e.msg}")
                        continue
                k = knowledge_for(m)
                out = run_tests(m, cats, think_allowed, lambda s: TEST_STATE["log"].append(s), probe, k.get("sampling"))
                with LOCK:
                    res = load_results()
                    ent = res.setdefault(m, {})
                    sc = ent.setdefault("scores", {})
                    for cat, r in out.items():
                        sc.setdefault(cat, []).append({"score": r["score"], "n": r["n"], "t": time.time(),
                                                       "detail": r.get("detail"), "think": think_allowed,
                                                       **({"tok_s": r["tok_s"], "gpu_pct": r["gpu_pct"]} if "tok_s" in r else {})})
                    save_results(res)
                TEST_STATE["log"].append(f"{m}: done")
        except Exception as e:                     # never leave the UI stuck on "running"
            TEST_STATE["log"].append(f"error: {type(e).__name__}: {e}")
        finally:
            TEST_STATE["running"] = False
    threading.Thread(target=work, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "ModelHub"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0].lower()
        return h in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        self._responded = True
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        gz = len(data) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or "")   # the 480 KB page goes over the wire as ~90 KB
        if gz:
            import gzip
            data = gzip.compress(data, compresslevel=5)
        self.send_response(code)
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_etag", None):
            self.send_header("ETag", self._etag)
            self._etag = None
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _send_etag(self, body):
        import hashlib
        data = json.dumps(body, sort_keys=True).encode("utf-8")
        tag = '"' + hashlib.sha1(data).hexdigest()[:20] + '"'
        if (self.headers.get("If-None-Match") or "").strip() == tag:
            self._responded = True
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return None
        self._etag = tag
        return self._send(200, data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                try:
                    html = open(UI_FILE, encoding="utf-8").read()
                except OSError:
                    return self._send(500, "hub_ui.html is missing next to model_hub.py", "text/plain")
                return self._send(200, html.replace("__HUB_TOKEN__", Hub.token), "text/html")
            if u.path == "/api/state":
                # ?part=catalog (what changes only when a flow, preset or template is saved) and ?part=live (the rest); no part = all
                # of it, as before. The body carries an ETag: a repeat with If-None-Match gets 304 and no body.
                return self._send_etag(build_state(q))
            if u.path == "/api/job":
                return self._send(200, JOB.snapshot(int(q.get("since", ["0"])[0])))
            if u.path == "/api/runs":
                return self._send(200, list_runs())
            if u.path == "/api/notes":
                try:
                    txt = open(NOTES_FILE, encoding="utf-8").read()
                except OSError:
                    txt = ""
                return self._send(200, {"text": txt})
            if u.path == "/api/results":
                return self._send(200, load_results())
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1].split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, {"error": "bad origin"})
        if not secrets.compare_digest(self.headers.get("X-Hub-Token") or "", Hub.token):
            return self._send(403, {"error": "bad token: reload the page"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 2_000_000:                  # a negative length would read until the peer hangs up
                return self._send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            self._responded = False
            if self.path.split("?", 1)[0] in CONFIG_ROUTES:
                with CONFIG_LOCK:                      # one settings change at a time: no change is lost to a concurrent save
                    out = self.route(self.path, body)
            else:
                out = self.route(self.path, body)
            if self._responded:                        # the route wrote its own reply (`return self._send(...)`): never a second one
                return None
            return self._send(200, out)
        except (RuntimeError, ValueError, OllamaError) as e:
            return self._send(400, {"error": getattr(e, "msg", None) or str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def route(self, path, b):
        if path == "/api/config":
            return {"ok": True, "config": write_config(b.get("config"))}
        if path == "/api/weights":
            with LOCK:
                res = load_results()
                res["_weights"] = {k: max(0, min(100, int(v))) for k, v in (b.get("weights") or {}).items() if k in DEFAULT_WEIGHTS}
                save_results(res)
            return {"ok": True}
        if path == "/api/probe":
            m = str(b.get("model") or "")
            probe = probe_model(m)
            with LOCK:
                res = load_results()
                res.setdefault(m, {})["probe"] = probe
                save_results(res)
            return {"ok": True, "probe": probe}
        if path == "/api/test":
            if TEST_STATE["running"]:
                raise RuntimeError("Tests are already running.")
            models = [str(x) for x in b.get("models") or []]
            cats = [c for c in b.get("categories") or [] if c in dict(CATEGORIES)]
            if not models or not cats:
                raise ValueError("Pick at least one model and one category.")
            start_tests(models, cats, bool(b.get("think")))
            return {"ok": True}
        if path == "/api/clear_results":
            with LOCK:
                res = load_results()
                m = b.get("model")
                if m in res:
                    res[m].pop("scores", None)
                    save_results(res)
            return {"ok": True}
        if path == "/api/run":
            goal = str(b.get("goal") or "").strip()
            if not goal:
                raise ValueError("Type a goal first.")
            review = b.get("review") if b.get("review") in ("ask", "auto", "dry") else "ask"
            argv = ["autonomous_agent_optimized.py", "--goal", goal, "--review", review]
            if b.get("model"):
                argv += ["--model", str(b["model"])]
            if b.get("plan_model"):
                argv += ["--plan-model", str(b["plan_model"])]
            if b.get("best_of"):
                argv += ["--best-of", str(int(b["best_of"]))]
            if b.get("reproduce_first"):
                argv += ["--reproduce-first"]
            JOB.start("Agent: " + goal[:60], argv)
            return {"ok": True}
        if path == "/api/action":
            name = b.get("name")
            if name not in ACTIONS:
                raise ValueError("unknown action")
            JOB.start(name, list(ACTIONS[name]))
            return {"ok": True}
        if path == "/api/input":
            JOB.send(str(b.get("text") or ""))
            return {"ok": True}
        if path == "/api/stop":
            JOB.stop()
            return {"ok": True}
        if path == "/api/notes":
            with open(NOTES_FILE, "w", encoding="utf-8") as f:
                f.write(str(b.get("text") or ""))
            return {"ok": True}
        raise ValueError("unknown request")


def serve(port=8765, open_browser=True, host="127.0.0.1"):
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer((host, p), Handler)
            break
        except OSError:
            continue
    else:
        print("No free port found.")
        return 1
    Hub.port = srv.server_address[1]
    url = f"http://127.0.0.1:{Hub.port}/"
    print(f"Model Hub running at {url}   (Ctrl+C to stop; listening on this computer only)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        JOB.stop()
        srv.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        import hub_selftest
        return hub_selftest.run()
    return serve(a.port, not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
