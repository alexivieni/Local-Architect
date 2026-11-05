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
    ok = arch_server.slot_io("save", cfg, proj)
    saved = os.path.join(w, hyg.slot_name(proj))
    check("slot_io save by full path writes the path-hashed name", ok and os.path.isfile(saved) and FakeSlots.state["calls"][-1] == ("save", hyg.slot_name(proj)),
          FakeSlots.state["calls"])
    left = sorted(os.path.basename(p) for p in olds if os.path.isfile(p))
    check("slot_io save applies the cap: the two oldest others evicted (5000 -> 3000 bytes), the newer ones and the just-saved one kept",
          left == ["old2.bin", "old3.bin"] and os.path.isfile(saved), left)
    legacy = _mk(os.path.join(w, "legacyproj.bin"), 10, 7200)
    ok = arch_server.slot_io("restore", cfg, os.path.join(tmp, "x", "legacyproj"))
    check("slot_io restore falls back to an old base-name file", ok and FakeSlots.state["calls"][-1] == ("restore", "legacyproj.bin"))
    check("a restore marks the file as used (least recently USED)", time.time() - os.path.getmtime(legacy) < 60)
    check("slot_io restore of a project with no file asks nothing", arch_server.slot_io("restore", cfg, os.path.join(tmp, "nope")) is False
          and FakeSlots.state["calls"][-1][1] != "nope.bin")
    check("a bare name (older callers) still saves under the old name",
          arch_server.slot_io("save", {"storage": {"slot_cache_gb": 0}}, "plain") and os.path.isfile(os.path.join(w, "plain.bin")))
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
