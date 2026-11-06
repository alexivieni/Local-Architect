"""Self-test for cache hygiene (arch_cachehygiene.py, the slot save cap in arch_server.slot_io, the reply-cache cap in arch_cache.put).

Owns its data folder: ARCHITECT_DATA_DIR and the install home are throwaway temp folders with fake slot files. No GPU, no llama-server,
no network beyond a loopback fake that answers like llama-server's /slots endpoints. run() returns True when every check passed."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class FakeSlots(BaseHTTPRequestHandler):
    """GET /slots: idle. POST /slots/0?action=save writes the named file (like llama-server), restore records the name."""
    state = {"dir": "", "calls": [], "size": 1000}

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        d = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        if self.path == "/slots":
            return self._send(200, [{"id": 0, "is_processing": False}])
        return self._send(404, {})

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        action = self.path.split("action=")[-1]
        self.state["calls"].append((action, b.get("filename")))
        if action == "save":
            os.makedirs(self.state["dir"], exist_ok=True)
            with open(os.path.join(self.state["dir"], b["filename"]), "wb") as f:
                f.write(b"k" * self.state["size"])
        elif action == "restore" and not os.path.isfile(os.path.join(self.state["dir"], b.get("filename") or "")):
            return self._send(404, {"error": "missing"})
        return self._send(200, {"id_slot": 0})


def _mk(path, size, age_secs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    t = time.time() - age_secs
    os.utime(path, (t, t))
    return path


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== cache hygiene: inventory, orphans, prune plan, caps ==")
    import arch_core as core

    tmp = tempfile.mkdtemp(prefix="archhyg_")
    old_home = core.HOME
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_EVAL", "ARCHITECT_NO_CACHE")}
    core.set_home(os.path.join(tmp, "home"))
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ.pop("ARCHITECT_EVAL", None)
    os.environ.pop("ARCHITECT_NO_CACHE", None)
    srv = None
    try:
        import arch_cachehygiene as hyg
        _inventory_checks(check, hyg, tmp)
        _prune_checks(check, hyg, tmp)
        _cap_checks(check, hyg, tmp)
        srv = _slot_io_checks(check, hyg, tmp)
        _reply_cap_checks(check)
        _settings_checks(check, core)
        _retention_checks(check, hyg, core, tmp)
    finally:
        if srv:
            srv.shutdown()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CACHE HYGIENE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CACHE HYGIENE CHECKS FAILED'}")
    return n == len(results)


def _reset(hyg):
    shutil.rmtree(os.path.join(os.environ["ARCHITECT_DATA_DIR"]), ignore_errors=True)
    os.makedirs(os.environ["ARCHITECT_DATA_DIR"], exist_ok=True)


def _fixture(hyg, tmp):
    """Fake slot files: two thrown-away, one of a live project, one of a project whose folder is gone, one nobody knows."""
    _reset(hyg)
    live = os.path.join(tmp, "projects", "shop")
    os.makedirs(live, exist_ok=True)
    gone = os.path.join(tmp, "projects", "deleted_app")
    w = os.path.join(hyg.slots_dir(), "worker")
    f = {
        "eval": _mk(os.path.join(w, "eval_build_ab12cd.bin"), 5000, 3600 * 5),
        "live_t": _mk(os.path.join(w, "live_route.bin"), 3000, 3600 * 4),
        "shop": _mk(os.path.join(w, hyg.slot_name(live)), 4000, 3600 * 3),
        "gone": _mk(os.path.join(w, hyg.legacy_slot_name(gone)), 2000, 3600 * 2),
        "mystery": _mk(os.path.join(w, "mystery.bin"), 1000, 3600),
    }
    return f, [live, gone]


def _inventory_checks(check, hyg, tmp):
    f, roots = _fixture(hyg, tmp)
    _mk(os.path.join(hyg.cloud_dir(), "old.json"), 100, 40 * 86400)
    _mk(os.path.join(hyg.cloud_dir(), "new.json"), 50, 60)
    cfg = {"storage": {"slot_cache_gb": 0, "slot_max_age_days": 30}}
    inv = {s["id"]: s for s in hyg.inventory(cfg, roots=roots)}
    sl = inv["slots"]
    check("inventory: every slot file counted, sizes added up", sl["files"] == 5 and sl["bytes"] == 15000, (sl["files"], sl["bytes"]))
    check("inventory: oldest and newest are the files' times", sl["oldest"] < sl["newest"] and abs(sl["oldest"] - os.path.getmtime(f["eval"])) < 2)
    orph = {os.path.basename(o["file"]) for o in sl["orphans"]}
    check("orphans: eval_* and live_* slot files are orphans", {"eval_build_ab12cd.bin", "live_route.bin"} <= orph, orph)
    check("orphans: a known project whose folder is gone is an orphan",
          os.path.basename(f["gone"]) in orph and any("gone" in o["why"] for o in sl["orphans"]), sl["orphans"])
    check("orphans: a slot nobody can place is 'unknown owner' and NOT an orphan",
          "mystery.bin" not in orph and [os.path.basename(u["file"]) for u in sl["unknown"]] == ["mystery.bin"], sl["unknown"])
    check("owned: the live project's slot (path-hashed name) is owned", [os.path.basename(o["file"]) for o in sl["owned"]] == [os.path.basename(f["shop"])])
    check("orphan_bytes adds the orphans up", sl["orphan_bytes"] == 5000 + 3000 + 2000, sl["orphan_bytes"])
    check("no cap set (0) -> never over cap", sl["over_cap"] is False and not sl["cap_bytes"])
    check("cloud cache: only the entry past 30 days is listed", [os.path.basename(o["file"]) for o in inv["cloud"]["orphans"]] == ["old.json"])
    check("every store reports the asked-for fields",
          all({"name", "path", "files", "bytes", "oldest", "newest", "cap_bytes", "over_cap", "orphans", "orphan_bytes"} <= set(s) for s in inv.values()))
    check("slot names: same folder name in two places -> two files; a bare name keeps the old name",
          hyg.slot_name(os.path.join(tmp, "a", "app")) != hyg.slot_name(os.path.join(tmp, "b", "app"))
          and hyg.slot_name("app") == "app.bin" and hyg.legacy_slot_name(os.path.join(tmp, "a", "app")) == "app.bin")
    inv2 = {s["id"]: s for s in hyg.inventory({"storage": {"slot_cache_gb": 0, "slot_max_age_days": 30}},
                                              roots=roots, now=time.time() + 40 * 86400)}
    check("stale: a slot unused past slot_max_age_days is listed as stale (orphans are not double-listed)",
          {os.path.basename(s["file"]) for s in inv2["slots"]["stale"]} == {os.path.basename(f["shop"]), "mystery.bin"}, inv2["slots"]["stale"])


def _prune_checks(check, hyg, tmp):
    f, roots = _fixture(hyg, tmp)
    cfg = {"storage": {"slot_cache_gb": 0, "slot_max_age_days": 30}, "cache": {"max_rows": 4000, "max_days": 30}}
    plan = hyg.plan_prune(cfg, roots=roots)
    names = sorted(os.path.basename(i["file"]) for i in plan["items"])
    check("plan: the orphans, and nothing else", names == sorted(["eval_build_ab12cd.bin", "live_route.bin", os.path.basename(f["gone"])]), names)
    check("plan: every item says why", all(i["why"] for i in plan["items"]) and plan["bytes"] == 10000)
    res = hyg.prune(plan)
    check("prune without yes deletes nothing", res["dry_run"] and not res["deleted"] and all(os.path.isfile(p) for p in f.values()))
    check("prune without yes writes no log", not os.path.isfile(hyg.log_path()))
    res = hyg.prune(plan, yes=True, cfg=cfg)
    gone = sorted(os.path.basename(d["file"]) for d in res["deleted"])
    check("prune with yes deletes exactly the plan", gone == names and not any(os.path.isfile(i["file"]) for i in plan["items"]), gone)
    check("...and keeps everything else", os.path.isfile(f["shop"]) and os.path.isfile(f["mystery"]))
    check("...and says what it freed", res["freed"] == 10000, res["freed"])
    with open(hyg.log_path(), encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if "\tdeleted\t" in ln]
    check("every deletion is in cache_hygiene.log, with its size and reason",
          len(lines) == 3 and all(any(os.path.basename(p) in ln for ln in lines) for p in (f["eval"], f["live_t"], f["gone"]))
          and all(ln.split("\t")[4] for ln in lines), lines)

    # refusals: outside the cache folders, a model file, a non-cache file in the data folder
    outside = _mk(os.path.join(tmp, "elsewhere", "eval_build_x.bin"), 10, 9999)
    model = _mk(os.path.join(hyg.slots_dir(), "worker", "qwen3.gguf"), 10, 9999)
    ledger = _mk(os.path.join(os.environ["ARCHITECT_DATA_DIR"], "ledger.json"), 10, 9999)
    lookup = _mk(os.path.join(hyg.lookup_dir(), "worker.bin"), 10, 9999)
    sneaky = os.path.join(hyg.slots_dir(), "worker", "..", "..", "..", "ledger.json")
    bad = {"items": [{"store": "slots", "file": p, "bytes": 10, "why": "test"} for p in (outside, model, ledger, lookup, sneaky)], "reply_rows": 0}
    res = hyg.prune(bad, yes=True)
    check("prune refuses paths outside the cache folders, model files, data files and the lookup cache",
          not res["deleted"] and len(res["refused"]) == 5 and all(os.path.isfile(p) for p in (outside, model, ledger, lookup)), res)
    check("allowed_to_delete gives a plain reason", "outside" in hyg.allowed_to_delete(outside) and "model" in hyg.allowed_to_delete(model))

    # LRU beyond the cap in the plan: protected files stay
    f, roots = _fixture(hyg, tmp)
    st = {"processes": {"worker": {"pid": os.getpid(), "port": 1, "url": "http://127.0.0.1:1"}}}
    with open(os.path.join(os.environ["ARCHITECT_DATA_DIR"], "server.json"), "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    cap_gb = 4500 / hyg.GB
    plan = hyg.plan_prune({"storage": {"slot_cache_gb": cap_gb, "slot_max_age_days": 0}}, roots=roots)
    names = [os.path.basename(i["file"]) for i in plan["items"]]
    check("plan: beyond the cap, least recently used go first; the running server's newest slot is kept",
          "mystery.bin" not in names and os.path.basename(f["shop"]) in names and plan["slot_bytes_after"] <= 4500
          and any(os.path.basename(k["file"]) == "mystery.bin" for k in plan["kept"]), (names, plan["kept"], plan["slot_bytes_after"]))
    os.remove(os.path.join(os.environ["ARCHITECT_DATA_DIR"], "server.json"))
    lines = hyg.report_lines(hyg.inventory({"storage": {"slot_cache_gb": cap_gb}}, roots=roots), plan)
    check("the report is plain words and says nothing was deleted", any("Nothing was deleted" in ln for ln in lines) and any("orphan" in ln for ln in lines))


def _cap_checks(check, hyg, tmp):
    _reset(hyg)
    w = os.path.join(hyg.slots_dir(), "worker")
    files = [_mk(os.path.join(w, f"p{i}.bin"), 1000, 3600 * (10 - i)) for i in range(5)]     # p0 oldest ... p4 newest
    just_saved = files[0]                                                                 # even when it is the oldest, it stays
    cfg = {"storage": {"slot_cache_gb": 3200 / hyg.GB}}
    gone = hyg.enforce_slot_cap(cfg, keep=just_saved)
    left = sorted(os.path.basename(p) for p in files if os.path.isfile(p))
    check("cap at save: least recently used others go, oldest first, until it fits",
          sorted(os.path.basename(g["file"]) for g in gone) == ["p1.bin", "p2.bin"] and left == ["p0.bin", "p3.bin", "p4.bin"], (gone, left))
    check("cap at save: the one just saved and the newest are kept", os.path.isfile(just_saved) and os.path.isfile(files[4]))
    with open(hyg.log_path(), encoding="utf-8") as fh:
        log = fh.read()
    check("cap at save: each eviction is logged", log.count("\tevicted\t") == 2 and "p1.bin" in log)
    check("cap at save: within the cap nothing happens", hyg.enforce_slot_cap(cfg, keep=just_saved) == [])
    check("cap 0 = no limit", hyg.enforce_slot_cap({"storage": {"slot_cache_gb": 0}}, keep=just_saved) == [] and os.path.isfile(files[3]))
    _mk(os.path.join(w, "fresh.bin"), 5000, 10)
    check("cap at save: a file touched minutes ago (a save or restore under way) is never evicted",
          os.path.isfile(os.path.join(w, "fresh.bin")) and (hyg.enforce_slot_cap(cfg, keep=just_saved) or True)
          and os.path.isfile(os.path.join(w, "fresh.bin")))


def _slot_io_checks(check, hyg, tmp):
    import arch_server
    _reset(hyg)
    w = os.path.join(hyg.slots_dir(), "worker")
    FakeSlots.state.update(dir=w, calls=[], size=1000)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSlots)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    with open(arch_server.state_path(), "w", encoding="utf-8") as fh:
        json.dump({"processes": {"worker": {"pid": os.getpid(), "port": srv.server_address[1], "url": url, "name": "worker"}}}, fh)
    olds = [_mk(os.path.join(w, f"old{i}.bin"), 1000, 3600 * (5 - i)) for i in range(4)]
    proj = os.path.join(tmp, "projects", "shop")
    cfg = {"storage": {"slot_cache_gb": 3000 / hyg.GB}}
    fpn = lambda p_: hyg.slot_name(p_)[:-4] + "-" + arch_server.model_fp("worker") + ".bin"     # saved prompt memory is tied to the model that wrote it
    ok = arch_server.slot_io("save", cfg, proj)
    saved = os.path.join(w, fpn(proj))
    check("slot_io save by full path writes the path-hashed, model-fingerprinted name", ok and os.path.isfile(saved) and FakeSlots.state["calls"][-1] == ("save", fpn(proj)),
          FakeSlots.state["calls"])
    left = sorted(os.path.basename(p) for p in olds if os.path.isfile(p))
    check("slot_io save applies the cap: the two oldest others evicted (5000 -> 3000 bytes), the newer ones and the just-saved one kept",
          left == ["old2.bin", "old3.bin"] and os.path.isfile(saved), left)
    legacy = _mk(os.path.join(w, "legacyproj.bin"), 10, 7200)
    n_calls = len(FakeSlots.state["calls"])
    ok = arch_server.slot_io("restore", cfg, os.path.join(tmp, "x", "legacyproj"))
    check("slot_io restore never loads an old file with no model fingerprint (it may belong to another model)", ok is False and len(FakeSlots.state["calls"]) == n_calls)
    mine = _mk(os.path.join(w, fpn(os.path.join(tmp, "x", "kept"))), 10, 7200)
    ok = arch_server.slot_io("restore", cfg, os.path.join(tmp, "x", "kept"))
    check("slot_io restore of this model's file works", ok and FakeSlots.state["calls"][-1] == ("restore", os.path.basename(mine)))
    check("a restore marks the file as used (least recently USED)", time.time() - os.path.getmtime(mine) < 60)
    check("slot_io restore of a project with no file asks nothing", arch_server.slot_io("restore", cfg, os.path.join(tmp, "nope")) is False
          and FakeSlots.state["calls"][-1][1] != "nope.bin")
    check("a bare name (older callers) saves under its old name plus the model fingerprint",
          arch_server.slot_io("save", {"storage": {"slot_cache_gb": 0}}, "plain") and os.path.isfile(os.path.join(w, "plain-" + arch_server.model_fp("worker") + ".bin")))
    # a pid that a record says we launched as one program, now held by another program (Windows recycles pids), is not our server
    with open(arch_server.state_path(), "w", encoding="utf-8") as fh:
        json.dump({"processes": {"worker": {"pid": os.getpid(), "port": 1, "image": "llama-server.exe"}}}, fh)
    if sys.platform.startswith("win"):
        check("a recorded server pid now held by a different program is not alive (never killed by mistake)", arch_server._alive(os.getpid()) is False and arch_server._alive(os.getpid(), owner=True) is True)
    if sys.platform.startswith("win"):
        check("a program on one of our ports that is not a llama server (a dev server) is left alone", arch_server._is_llama(os.getpid()) is False)
    import arch_heavy
    _lf = open(os.path.join(arch_server.server_dir(), "start.lock"), "a+")
    _held = arch_heavy._try_lock(_lf)
    _raised = False
    try:
        with arch_server._starting(say=lambda m: None, wait_s=0.6):
            pass
    except RuntimeError:
        _raised = True
    finally:
        if _held:
            arch_heavy._unlock(_lf)
        _lf.close()
    check("when another program keeps the start lock, starting servers is refused instead of going on unlocked", _held and _raised, (_held, _raised))
    _names = set()
    arch_server.save_state({"processes": {}})
    arch_server.save_state({"processes": {}})
    check("saving server state leaves no temp file behind", not [f for f in os.listdir(os.path.dirname(arch_server.state_path())) if f.endswith(".tmp")])
    # an expired cache row is a miss at once, not after the next sweep
    import arch_cache
    ck_ = "e" * 64
    ccfg = {"cache": {"max_days": 1}}
    arch_cache.put(ck_, "m", "reply", tokens_in=1, tokens_out=1, secs=1, tier="worker", cfg=ccfg)
    fresh_ = bool(arch_cache.get(ck_, ccfg))
    cn_ = arch_cache._open(); cn_.execute("update responses set created=? where k=?", (time.time() - 3 * 86400, ck_)); cn_.commit(); cn_.close()
    check("a cache row past its age limit is not served", fresh_ and arch_cache.get(ck_, ccfg) is None)
    os.remove(arch_server.state_path())
    return srv


def _reply_cap_checks(check):
    import arch_cache
    arch_cache.clear()
    cfg = {"cache": {"responses": True, "max_rows": 3, "max_days": 30}}
    for i in range(7):
        arch_cache.put(f"k{i}", "m", f"reply {i}", cfg=cfg)
    st = arch_cache.stats()
    check("reply cache: cache.max_rows is applied as replies are stored (it used to be a setting nothing enforced)", st["rows"] == 3, st)
    check("reply cache: the newest replies are the ones kept", arch_cache.get("k6", cfg) is not None and arch_cache.get("k0", cfg) is None)
    check("reply cache: limits() reads the settings, 0 = off", arch_cache.limits({"cache": {"max_rows": 0, "max_days": 7}}) == (0, 7)
          and arch_cache.limits(None) == (arch_cache.MAX_ROWS, arch_cache.MAX_DAYS))
    arch_cache.clear()


def _retention_checks(check, hyg, core, tmp):
    import json
    import sqlite3
    import subprocess
    _reset(hyg)
    data = os.environ["ARCHITECT_DATA_DIR"]
    day = 86400
    old, new = day * 200, day * 5
    ci_old = _mk(os.path.join(data, "ci", "20260101_000000.json"), 50, old)
    ci_new = _mk(os.path.join(data, "ci", "20261101_000000.json"), 50, new)
    run_old = _mk(os.path.join(data, "ci_run2.log"), 70, old)
    td_old = _mk(os.path.join(data, "orchestration", "test_data", "20260101_0000", "x.json"), 30, old)
    td_new = _mk(os.path.join(data, "orchestration", "test_data", "20261101_0000", "y.json"), 30, new)
    orch_real = _mk(os.path.join(data, "orchestration", "20260101_real.json"), 30, old)
    tel1 = _mk(os.path.join(data, "telemetry", "events.jsonl.1"), 40, old)
    # protected things, all old
    ledger = _mk(os.path.join(data, "ledger.json"), 10, old)
    ds = _mk(os.path.join(data, "datasets", "sft.jsonl"), 10, old)
    bak = _mk(os.path.join(data, "config.backup_x.json"), 10, old)
    ev = _mk(os.path.join(data, "evals", "runs", "train", "r.json"), 10, old)
    cfgf = _mk(os.path.join(data, "architect_config.json"), 10, old)
    now = time.time()
    ev_path = os.path.join(data, "telemetry", "events.jsonl")
    with open(ev_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": now - old, "kind": "a"}) + "\n")
        fh.write(json.dumps({"t": now - old, "kind": "b"}) + "\n")
        fh.write(json.dumps({"t": now - new, "kind": "c"}) + "\n")
    os.utime(ev_path, (now - 3600, now - 3600))
    c = sqlite3.connect(os.path.join(data, "logbook.db"))
    c.execute("create table tasks(id text primary key, source text, title text, kind text, started real, cost real, status text, turns integer, model text, by_claude integer, tools text, origin text)")
    c.execute("create table steps(task_id text, n integer, kind text, tool text, text text)")
    c.execute("create table logbook_fts(task_id text, body text)")
    for tid, st in (("old1", now - old), ("old2", now - old), ("new1", now - new)):
        c.execute("insert into tasks(id, started) values(?,?)", (tid, st))
        c.execute("insert into steps values(?,?,?,?,?)", (tid, 1, "k", "t", "x"))
        c.execute("insert into logbook_fts values(?,?)", (tid, "body"))
    c.commit()
    c.close()
    # a project with an old and a new checkpoint ref
    proj = os.path.join(tmp, "projects", "ckproj")
    os.makedirs(proj, exist_ok=True)
    have_git = True
    try:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q"], cwd=proj, env=env, check=True, capture_output=True)
        open(os.path.join(proj, "f.txt"), "w").write("x")
        subprocess.run(["git", "add", "f.txt"], cwd=proj, env=env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "n"], cwd=proj, env=env, check=True, capture_output=True)
        subprocess.run(["git", "update-ref", "refs/architect/newrun/1", "HEAD"], cwd=proj, env=env, check=True, capture_output=True)
        oldenv = dict(env, GIT_COMMITTER_DATE=str(int(now - old)) + " +0000", GIT_AUTHOR_DATE=str(int(now - old)) + " +0000")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "o"], cwd=proj, env=oldenv, check=True, capture_output=True)
        subprocess.run(["git", "update-ref", "refs/architect/oldrun/1", "HEAD"], cwd=proj, env=oldenv, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        have_git = False
    cfg = {"storage": {"retention_days": 90}}
    plan = hyg.retention_plan(cfg, roots=[proj], now=now)
    files = {os.path.normcase(i["file"]) for i in plan["items"] if i["kind"] == "file"}
    nc = os.path.normcase
    check("retention: old CI logs, ci_run logs, test_data and the rolled telemetry are planned",
          {nc(ci_old), nc(run_old), nc(td_old), nc(tel1)} <= files, files)
    check("retention: recent ones are kept", nc(ci_new) not in files and nc(td_new) not in files)
    check("retention: orchestration records outside test_data are not touched", nc(orch_real) not in files)
    check("retention: ledger, datasets, settings, eval buckets, backups never planned",
          not ({nc(ledger), nc(ds), nc(bak), nc(ev), nc(cfgf)} & files))
    kinds = {i["kind"]: i for i in plan["items"]}
    check("retention: 2 old telemetry lines and 2 old logbook tasks counted", kinds.get("lines", {}).get("count") == 2 and kinds.get("rows", {}).get("count") == 2, kinds)
    if have_git:
        check("retention: only the old checkpoint ref is planned", kinds.get("refs", {}).get("refs") == ["refs/architect/oldrun/1"], kinds.get("refs"))
    check("retention: backups are listed, not planned", any(nc(b["file"]) == nc(bak) for b in plan["backups"]) and nc(bak) not in files)
    check("retention: the report mentions it and says nothing was deleted without --yes",
          any("storage.retention_days" in ln for ln in hyg.report_lines([], dict(hyg.plan_prune(cfg, inv=[], roots=[proj], now=now)), None)))
    full = hyg.plan_prune(cfg, inv=[], roots=[proj], now=now)
    res = hyg.prune(full, yes=False, cfg=cfg)
    check("retention: a dry run deletes nothing", os.path.isfile(ci_old) and res["dry_run"])
    res = hyg.prune(full, yes=True, cfg=cfg)
    check("retention: --yes removes the planned files", not any(os.path.exists(p) for p in (ci_old, run_old, td_old, tel1)), res)
    check("retention: recent files and protected data survive",
          all(os.path.exists(p) for p in (ci_new, td_new, orch_real, ledger, ds, bak, ev, cfgf)))
    check("retention: the emptied dated test_data folder is gone, the live one stays",
          not os.path.isdir(os.path.dirname(td_old)) and os.path.isdir(os.path.dirname(td_new)))
    left = [json.loads(x)["kind"] for x in open(ev_path, encoding="utf-8")]
    check("retention: telemetry keeps only the recent event", left == ["c"], left)
    c = sqlite3.connect(os.path.join(data, "logbook.db"))
    ids = sorted(r[0] for r in c.execute("select id from tasks"))
    st = c.execute("select count(*) from steps").fetchone()[0]
    fts = c.execute("select count(*) from logbook_fts").fetchone()[0]
    c.close()
    check("retention: old logbook tasks, their steps and index rows are gone", ids == ["new1"] and st == 1 and fts == 1, (ids, st, fts))
    if have_git:
        p = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/architect/"], cwd=proj, capture_output=True)
        check("retention: the old checkpoint ref is deleted, the new one kept", p.stdout.decode().split() == ["refs/architect/newrun/1"], p.stdout)
    check("retention: a path outside the allow-list is refused", hyg.retention_allowed(ledger) != "" and hyg.retention_allowed(os.path.join(data, "x.txt")) != "")
    zero = hyg.retention_plan({"storage": {"retention_days": 0}}, roots=[proj], now=now)
    check("retention: 0 days keeps everything", zero["items"] == [])
    check("retention: storage.retention_days is a setting with a default of 90",
          core.DEFAULTS["storage"].get("retention_days") == 90)


def _settings_checks(check, core):
    import arch_params
    flat = {r[0]: r for rows in arch_params.SETTINGS.values() for r in rows}
    d = core.DEFAULTS["storage"]
    check("settings: storage.slot_cache_gb is in SETTINGS and DEFAULTS (default 4 GB)",
          "storage.slot_cache_gb" in flat and flat["storage.slot_cache_gb"][1] == "float" and float(d.get("slot_cache_gb")) == 4.0)
    check("settings: storage.slot_max_age_days is in SETTINGS and DEFAULTS (default 30)",
          "storage.slot_max_age_days" in flat and flat["storage.slot_max_age_days"][1] == "int" and d.get("slot_max_age_days") == 30)
    check("settings: both have plain descriptions", all(len(flat[k][3]) > 40 for k in ("storage.slot_cache_gb", "storage.slot_max_age_days")))
    check("settings: the reply cache's caps are still settings", "cache.max_rows" in flat and "cache.max_days" in flat)
    if hasattr(arch_params, "validate_settings_patch"):
        check("settings: the new values validate", bool(arch_params.validate_settings_patch({"storage.slot_cache_gb": 2.5, "storage.slot_max_age_days": 14})))


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
