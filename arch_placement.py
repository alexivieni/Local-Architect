"""Placement: one place that decides where every workload and every kind of data goes on THIS machine, from measured facts.

Compute: the graphics card writes code (the worker; the fast lane), the performance cores answer routing questions and run the tests
when there are no efficiency cores, the efficiency cores take the background chores (indexing, logbook ingest, overseer scans, dataset
export, compression), and RAM holds the experts of a mixture-of-experts heavy tier and the caches.

Storage: hot data (models, KV slot caches, the n-gram lookup cache, the reply cache, live databases) stays on the fastest fixed internal
drive, the one holding the models. Cold data (run archives, logbook backups, overseer timeline rollovers, dataset exports, batch results,
old self-build copies) may go to a slow or removable drive, such as a memory card (a fixed slower disk, when there is one, is preferred
over a drive that can be unplugged); datasets, archives and backups are COPIED there, only old self-build copies are MOVED. A drive that drive_roles flags (fake capacity, failing
speed test, too slow) holds nothing.

Everything is a pure function of a `facts` dict, so it is tested with made-up machines (arch_selftest_placement). `facts()` reads the
real one. Nothing is written or moved unless `apply_storage(..., dry_run=False)` is called; a copy is checked byte for byte before a
source is removed, and every applied change leaves an undo record (`undo_storage`).

Evidence labels on every line: "measured" (on this machine), "estimate" (arithmetic or a default), "rule" (a policy of this app).
"""
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GB = 1024 ** 3
FAT32_MAX = 4 * GB - 1
EST_GPU_BW, EST_RAM_BW = 192.0, 50.0          # the same defaults arch_hw.load_bandwidth's callers use when nothing was measured
FAST_READ_MB_S = 300.0                        # a fixed drive reading at least this is fast enough for model loads and slot caches
KEEP_SELF_BUILDS = 2                          # the newest self-build copies stay where self-improve and self-rollback look for them
COLD_DIR = "architect_cold"                   # <cold drive>/architect_cold/<kind>/...; run archives keep arch_archive's own folder

HOT_DATA = (
    ("models", "Model files (GGUF, Ollama blobs)", "loading reads several GB each time a server starts; only a fast fixed drive is acceptable"),
    ("slot_cache", "KV slot caches (server/slots)", "saved prefill read back on every repeated prompt start; a slow read costs more than recomputing"),
    ("lookup_cache", "N-gram lookup cache (server/lookup)", "read by the worker for every guessed token"),
    ("reply_cache", "Reply cache (arch_cache)", "read on every repeatable call"),
    ("databases", "Logbook, ledger, jobs.db, index.db", "written during runs; a drive unplugged mid-write corrupts SQLite and JSON"),
)
COLD_DATA = (
    ("run_archives", "Run archives (storage.archive)", "copy"),
    ("logbook_backups", "Logbook backups", "copy"),
    ("timeline_rollovers", "Overseer timeline and recorder rollovers", "copy"),
    ("dataset_exports", "Dataset exports (SFT, preference pairs, trajectories)", "copy"),
    ("batch_results", "Batch and eval results", "copy"),
    ("self_builds", "Old self-build copies (.arch_self/candidates)", "move"),
)
BACKGROUND = ("indexing", "logbook ingest", "overseer scans", "dataset export", "compression")


# ----------------------------------------------------------------------------------------------------------------------------- facts
def _bits(mask):
    return [i for i in range(int(mask or 0).bit_length()) if mask >> i & 1]


def facts(hw=None, drs=None, speeds=None, bw=None, ecores=None, models=None, data_dir=None, app_dir=None, heavy_ram_gb=None, measure=False):
    """What placement is decided from. Every argument may be given (tests do); what is missing is read from this machine. `measure`
    runs arch_hw.measure_drive on each mounted drive (writes and deletes one 64 MB file per drive); otherwise only speeds already given
    are used."""
    import arch_hw
    hw = hw if hw is not None else arch_hw.detect()
    drs = drs if drs is not None else arch_hw.drives()
    speeds = dict(speeds or {})
    if measure:
        for d in drs:
            if d["mount"] not in speeds and not d.get("suspect"):
                speeds[d["mount"]] = arch_hw.measure_drive(d["mount"])
    if bw is None:
        try:
            bw = arch_hw.load_bandwidth()
        except Exception:
            bw = {}
    if ecores is None:
        ecores = arch_hw.ecore_mask()
    if data_dir is None:
        try:
            import arch_core as core
            data_dir = core.data_dir()
        except Exception:
            data_dir = os.path.join(HERE, "hub_data")
    models = models or hw.get("models_dir") or arch_hw.models_dir()
    arch_hw.drive_roles(drs, models=models, speeds=speeds)
    return {"hw": hw, "drives": drs, "speeds": speeds, "bw": bw or {}, "ecores": int(ecores or 0), "models": models,
            "data_dir": data_dir, "app_dir": app_dir or HERE, "heavy_ram_gb": heavy_ram_gb}


# --------------------------------------------------------------------------------------------------------------------------- compute
def compute(f):
    """-> [{"work", "place", "threads", "why", "evidence"}]; place is gpu | cpu-p | cpu-e | cpu | ram."""
    hw, bw = f["hw"], f.get("bw") or {}
    logical = int(hw.get("cpu_cores") or 0)
    e_cpus = _bits(f.get("ecores"))
    p_cores = hw.get("cpu_perf_cores")
    p_logical = max(logical - len(e_cpus), 0) if logical else None
    gpu = bool(hw.get("vendor")) and (hw.get("vram_gb") or 0) > 0
    gbw, gev = (bw["gpu_bw"], "measured") if bw.get("gpu_bw") else (EST_GPU_BW, "estimate")
    rbw, rev = (bw["ram_bw"], "measured") if bw.get("ram_bw") else (EST_RAM_BW, "estimate")
    rows = []

    def row(work, place, why, evidence, threads=None):
        rows.append({"work": work, "place": place, "threads": threads, "why": why, "evidence": evidence})

    if gpu:
        kind = "unified memory" if hw.get("unified") else f"{(hw.get('vram_gb') or 0):.1f} GB card"
        row("worker (writes code; the fast lane)", "gpu",
            f"{hw.get('gpu') or 'the graphics card'} ({kind}) reads weights at {gbw:g} GB/s against RAM's {rbw:g} GB/s, "
            f"about {gbw / rbw:.0f}x faster per token", gev if gev == rev else "measured + estimate", 1)
    else:
        row("worker (writes code)", "cpu-p", "no usable graphics card: the performance cores are the fastest place left; one thread per "
            "performance core, since the slow cores set the pace of every layer", "rule", p_cores)
    router_threads = max(1, (p_cores or 2) // 2)
    row("router (picks the flow)", "cpu-p", f"small enough for the CPU, so it never takes graphics memory from the worker; "
        f"{router_threads} of {p_cores or '?'} performance cores (half), leaving the rest for the worker's own thread and the tests",
        "rule", router_threads)
    if e_cpus:
        row("test runs", "cpu-e", f"efficiency cores {_span(e_cpus)} (behavior.tests_on_ecores): the test runner competes with no "
            "model server there", "measured" if hw.get("cpu_perf_cores") else "rule", len(e_cpus))
        for job in BACKGROUND:
            row(job, "cpu-e", f"background chore: efficiency cores {_span(e_cpus)} at below-normal priority (run_low), so it never "
                "slows a reply", "rule", 1)
    else:
        row("test runs", "cpu-p", "no efficiency cores on this CPU: tests share the performance cores; the worker on the card "
            "is unaffected", "rule", p_logical or logical or None)
        for job in BACKGROUND:
            row(job, "cpu", "background chore: below-normal priority (run_low); no efficiency cores to keep it apart", "rule", 1)
    heavy = f.get("heavy_ram_gb")
    free = hw.get("ram_free_gb")
    why = (f"experts of the mixture-of-experts heavy tier: only a few are read per token, so RAM's {rbw:g} GB/s is enough "
           "and the card keeps the shared layers")
    if heavy:
        why += f"; it holds about {heavy:g} GB"
        if free is not None and free < 0.15 * (hw.get("ram_gb") or 0):
            why += (f" and only {free:.1f} GB of RAM is free: set server.heavy_beside = off to load it on demand "
                    "(a 15-20 s wait per escalation, measured)")
    row("heavy tier experts", "ram", why, rev)
    row("caches (prompt cache --cache-ram, reply cache)", "ram", "read on every repeated prompt; RAM answers in microseconds and "
        "the slot files on the fast drive cover a restart", "rule")
    return rows


def _span(cpus):
    return f"{cpus[0]}-{cpus[-1]}" if cpus and cpus == list(range(cpus[0], cpus[-1] + 1)) else ",".join(map(str, cpus))


# --------------------------------------------------------------------------------------------------------------------------- storage
def drive_tier(d, speeds=None):
    """-> ("hot" | "warm" | "cold" | "refused", plain reason). Uses the role arch_hw.drive_roles gave it and any measured speed."""
    m = (speeds or {}).get(d["mount"]) or {}
    role = d.get("role")
    if role == "none" or d.get("suspect"):
        return "refused", d.get("why") or d.get("suspect") or "flagged by the drive check"
    if d.get("removable") or role == "archive":
        sp = f", writes at {m['write_mb_s']} MB/s (measured)" if m.get("write_mb_s") else ""
        return "cold", "can be unplugged" + sp + ": copies of cold records only, never models or caches"
    read = m.get("read_mb_s")
    if read is not None:
        if read >= FAST_READ_MB_S:
            return "hot", f"fixed drive reading {read} MB/s (measured)"
        return "warm", f"fixed drive reading only {read} MB/s (measured): fine for cold data, too slow for models"
    if (d.get("bus") or "") in ("nvme", "sata", "raid", "sas"):
        return "hot" if d.get("bus") == "nvme" else "warm", f"fixed {d.get('bus').upper()} drive (speed not measured yet)"
    return "warm", "fixed drive of unknown speed"


def _holds(mount, path):
    norm = lambda s: str(s).replace("\\", "/").lower()
    return bool(path) and norm(path).startswith(norm(mount))


def storage(f):
    """-> {"drives": [{mount, tier, why, free_gb, fs}], "hot": mount, "cold": mount|None, "data": [{data, kind, temp, mount, why}]}"""
    speeds = f.get("speeds") or {}
    out = []
    for d in f["drives"]:
        tier, why = drive_tier(d, speeds)
        out.append({"mount": d["mount"], "tier": tier, "why": why, "free_gb": d.get("free_gb"), "fs": d.get("fs") or "",
                    "total_gb": d.get("total_gb"), "write_mb_s": (speeds.get(d["mount"]) or {}).get("write_mb_s")})
    hot = next((x for x in out if _holds(x["mount"], f.get("models")) and x["tier"] in ("hot", "warm")), None)
    if not hot:
        hot = max((x for x in out if x["tier"] == "hot"), key=lambda x: x["free_gb"] or 0, default=None)
    cands = [x for x in out if x["tier"] in ("cold", "warm") and x is not hot and (x["free_gb"] or 0) >= 1.0]
    # a fixed slower drive beats one that can be unplugged; then a measured write speed beats none; then the faster; then the roomier
    cold = max(cands, key=lambda x: (x["tier"] == "warm", x["write_mb_s"] is not None, x["write_mb_s"] or 0, x["free_gb"] or 0),
               default=None)
    data = []
    for kind, label, why in HOT_DATA:
        data.append({"data": label, "kind": kind, "temp": "hot", "mount": hot["mount"] if hot else None,
                     "why": why if hot else "no fast fixed drive was found: " + why})
    for kind, label, mode in COLD_DATA:
        if cold:
            why = ("a spare copy here; the original stays on the fast drive" if mode == "copy"
                   else f"moved here after a verified copy; the newest {KEEP_SELF_BUILDS} stay for self-rollback")
            if (cold["fs"] or "").upper() == "FAT32":
                why += "; FAT32: files over 4 GB are skipped"
        else:
            why = "no second drive that can be trusted: stays on the fast drive"
        data.append({"data": label, "kind": kind, "temp": "cold", "mode": mode,
                     "mount": cold["mount"] if cold else (hot["mount"] if hot else None), "why": why})
    return {"drives": out, "hot": hot["mount"] if hot else None, "cold": cold["mount"] if cold else None, "data": data}


# ------------------------------------------------------------------------------------------------------------------------------ plan
def plan(f=None, **kw):
    """-> {"compute", "storage", "notes", "text"}: the whole decision, with a table and plain words in "text"."""
    f = f or facts(**kw)
    p = {"compute": compute(f), "storage": storage(f), "notes": []}
    st = p["storage"]
    refused = [d for d in st["drives"] if d["tier"] == "refused"]
    for d in refused:
        p["notes"].append(f"{d['mount']} holds nothing: {d['why']}.")
    if not st["cold"]:
        p["notes"].append("No trustworthy second drive: cold data stays on the fast drive. A memory card or USB drive can take it.")
    if not (f.get("bw") or {}).get("gpu_bw"):
        p["notes"].append("Memory speeds are estimates until a model has been timed here (arch_hw.bandwidth_from).")
    if not f.get("speeds"):
        p["notes"].append("Drive speeds are not measured yet: tiers come from the bus type (measure_drive refines them).")
    p["text"] = text(p)
    return p


def text(p):
    lines = ["Where the work runs", f"  {'work':<44} {'place':<6} {'thr':>3}  why"]
    for r in p["compute"]:
        lines.append(f"  {r['work']:<44} {r['place']:<6} {str(r['threads'] or '-'):>3}  {r['why']} [{r['evidence']}]")
    st = p["storage"]
    lines += ["", "Drives"]
    for d in st["drives"]:
        lines.append(f"  {d['mount']:<6} {d['tier']:<8} {d['fs']:<6} {d['free_gb'] if d['free_gb'] is not None else '?':>7} GB free  {d['why']}")
    lines += ["", "Where the data lives"]
    for r in st["data"]:
        lines.append(f"  {r['data']:<52} {r['temp']:<4} {r['mount'] or '-':<6} {r['why']}")
    words = []
    if st["hot"]:
        words.append(f"Models, caches and live databases stay on {st['hot']}.")
    if st["cold"]:
        words.append(f"Cold records (archives, backups, rollovers, exports, old self-builds) can go to {st['cold']}.")
    lines += ["", "In plain words: " + " ".join(words + p["notes"])]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------------- apply / undo
def _cold_sources(f):
    """(kind, mode, source path, name under <cold>/architect_cold/<kind>/) for the cold data this install actually has."""
    dd, app = f.get("data_dir") or "", f.get("app_dir") or HERE
    found = []

    def add(kind, mode, path, name=None):
        if os.path.exists(path):
            found.append((kind, mode, path, name or os.path.basename(path.rstrip("\\/"))))
    add("dataset_exports", "copy", os.path.join(dd, "datasets"))
    add("batch_results", "copy", os.path.join(dd, "evals"))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        for fn in sorted(os.listdir(dd)):
            p = os.path.join(dd, fn)
            if not os.path.isfile(p):
                continue
            if re.search(r"(logbook|ledger).*\.(bak|old|\d+)$", fn, re.I):
                add("logbook_backups", "copy", p)
            elif re.search(r"\.(log|jsonl)\.\d+$", fn):
                add("timeline_rollovers", "copy", p, f"{stamp}_{fn}")
    except OSError:
        pass
    cdir = os.path.join(app, ".arch_self", "candidates")
    try:
        olds = sorted(n for n in os.listdir(cdir) if os.path.isdir(os.path.join(cdir, n)))[:-KEEP_SELF_BUILDS or None]
    except OSError:
        olds = []
    for n in olds:
        add("self_builds", "move", os.path.join(cdir, n))
    return found


def _files(path):
    if os.path.isfile(path):
        yield path, ""
        return
    for root, _, fns in os.walk(path):
        for fn in fns:
            full = os.path.join(root, fn)
            yield full, os.path.relpath(full, path)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _default_write_cfg(cfg):
    import arch_core as core
    import model_hub as mh
    mh.CONFIG_FILE = core.CONFIG_FILE
    mh.write_config(cfg)


def _undo_path(f):
    return os.path.join(f.get("data_dir") or HERE, "placement_undo.json")


def apply_storage(p=None, f=None, cfg=None, dry_run=True, sources=None, write_cfg=None):
    """What placing the data would change. -> {"settings": [{"set", "from", "to"}], "moves": [{kind, mode, src, dest, bytes, skip?}],
    "dry_run", "done", "undo"}. With dry_run (the default) nothing is written. Otherwise: storage.archive is set (the old value goes
    into the undo record), each source is copied, every file is compared by SHA-256, and only a "move" whose copy matched removes its
    source. A FAT32 target skips files over 4 GB; a target without room (1 GB kept free) skips the item."""
    f = f or facts()
    p = p or plan(f)
    cold = p["storage"]["cold"]
    res = {"dry_run": dry_run, "settings": [], "moves": [], "done": [], "undo": None, "cold": cold}
    if not cold:
        res["why"] = "no trustworthy second drive: nothing to place"
        return res
    if cfg is None:
        try:
            import arch_core as core
            cfg = core.load_config()
        except Exception:
            cfg = {}
    old = ((cfg.get("storage") or {}).get("archive")) or ""
    same = bool(old) and os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(cold))
    if not same:
        res["settings"].append({"set": "storage.archive", "from": old, "to": cold})
    drv = next(d for d in p["storage"]["drives"] if d["mount"] == cold)
    fat = (drv["fs"] or "").upper() == "FAT32"
    room = max(((drv["free_gb"] or 0) - 1.0) * 1e9, 0)
    for kind, mode, src, name in (sources if sources is not None else _cold_sources(f)):
        dest = os.path.join(cold, COLD_DIR, kind, name)
        files = list(_files(src))
        size = sum(os.path.getsize(x) for x, _ in files)
        mv = {"kind": kind, "mode": mode, "src": src, "dest": dest, "bytes": size, "files": len(files)}
        big = [rel or os.path.basename(x) for x, rel in files if fat and os.path.getsize(x) > FAT32_MAX]
        if big:
            mv["skip_files"] = big
        if size > room:
            mv["skip"] = f"needs {size / 1e9:.2f} GB, {room / 1e9:.2f} GB free after the 1 GB reserve"
        else:
            room -= size
        res["moves"].append(mv)
    if dry_run:
        return res
    undo = {"t": time.time(), "settings": res["settings"], "moves": []}
    if res["settings"]:
        cfg.setdefault("storage", {})["archive"] = cold
        (write_cfg or _default_write_cfg)(cfg)
    for mv in res["moves"]:
        if mv.get("skip"):
            continue
        skip = set(mv.get("skip_files") or ())
        ok, copied = True, []
        for full, rel in _files(mv["src"]):
            if (rel or os.path.basename(full)) in skip:
                continue
            out = os.path.join(mv["dest"], rel) if rel else mv["dest"]
            try:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.copyfile(full, out)
                if _sha(full) != _sha(out):
                    ok = False
                    break
                copied.append(out)
            except OSError:
                ok = False
                break
        removed = False
        if ok and mv["mode"] == "move" and not skip:
            try:
                shutil.rmtree(mv["src"]) if os.path.isdir(mv["src"]) else os.remove(mv["src"])
                removed = True
            except OSError:
                pass
        rec = dict(mv, ok=ok, removed=removed, copied=len(copied))
        res["done"].append(rec)
        undo["moves"].append(rec)
    try:
        with open(_undo_path(f), "w", encoding="utf-8") as fh:
            json.dump(undo, fh, indent=1)
        res["undo"] = _undo_path(f)
    except OSError:
        res["undo"] = undo
    return res


def undo_storage(f=None, cfg=None, write_cfg=None, record=None):
    """Put back what apply_storage changed: the old storage.archive, and every moved source copied back from the cold drive (the cold
    copies stay; they are only copies). -> {"settings": n, "restored": [src], "missing": [dest]}"""
    if f is None:
        import arch_core as core
        f = {"data_dir": core.data_dir()}
    if record is None:
        with open(_undo_path(f), encoding="utf-8") as fh:
            record = json.load(fh)
    out = {"settings": 0, "restored": [], "missing": []}
    for s in record.get("settings") or []:
        if s.get("set") == "storage.archive":
            if cfg is None:
                import arch_core as core
                cfg = core.load_config()
            cfg.setdefault("storage", {})["archive"] = s.get("from") or ""
            (write_cfg or _default_write_cfg)(cfg)
            out["settings"] += 1
    for mv in record.get("moves") or []:
        if not mv.get("removed"):
            continue
        if not os.path.exists(mv["dest"]):
            out["missing"].append(mv["dest"])
            continue
        if os.path.isdir(mv["dest"]):
            shutil.copytree(mv["dest"], mv["src"], dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(mv["src"]), exist_ok=True)
            shutil.copyfile(mv["dest"], mv["src"])
        out["restored"].append(mv["src"])
    return out


# ------------------------------------------------------------------------------------------------------------- background helper
THREAD_PRIORITY_BELOW_NORMAL = -1
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000      # for a child process: subprocess.Popen(..., creationflags=low_creationflags())


def low_creationflags():
    return BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0


def _pin_this_thread(mask, api=None):
    """Put the calling thread on the cores in `mask` (0 = leave the cores alone) at below-normal priority. `api` stands in for the OS
    in tests: an object with affinity(mask) and lower(). -> {"affinity": bool, "priority": bool}"""
    done = {"affinity": False, "priority": False}
    if api is not None:
        if mask:
            done["affinity"] = bool(api.affinity(mask))
        done["priority"] = bool(api.lower())
        return done
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.GetCurrentThread.restype = ctypes.c_void_p
            k.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            k.SetThreadAffinityMask.restype = ctypes.c_size_t
            k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            th = k.GetCurrentThread()
            if mask:
                done["affinity"] = bool(k.SetThreadAffinityMask(th, mask))
            done["priority"] = bool(k.SetThreadPriority(th, THREAD_PRIORITY_BELOW_NORMAL))
        elif sys.platform.startswith("linux"):
            if mask:
                os.sched_setaffinity(0, set(_bits(mask)))          # 0 = the calling thread on Linux
                done["affinity"] = True
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)   # Linux nice values are per thread
            done["priority"] = True
    except (OSError, AttributeError, ValueError):
        pass
    return done


def run_low(fn, *args, wait=True, mask=None, api=None, **kwargs):
    """Run fn(*args, **kwargs) on its own thread pinned to the efficiency cores (arch_hw.ecore_mask; no pinning when the CPU has none)
    at below-normal priority, so indexing, logbook ingest, overseer scans, dataset export and compression never slow a reply. The
    calling thread keeps its own cores and priority. wait=True -> fn's result (its exception is raised here); wait=False -> the thread,
    with `.result` / `.error` / `.pinned` set when it ends."""
    if mask is None:
        try:
            import arch_hw
            mask = arch_hw.ecore_mask()
        except Exception:
            mask = 0
    box = {}

    def body():
        th = box["th"]
        th.pinned = _pin_this_thread(mask, api)
        try:
            th.result = fn(*args, **kwargs)
        except BaseException as e:            # handed to the caller, never lost on a background thread
            th.error = e
    th = box["th"] = threading.Thread(target=body, daemon=True, name="architect-low")
    th.result = th.error = None
    th.pinned = {}
    th.start()
    if not wait:
        return th
    th.join()
    if th.error is not None:
        raise th.error
    return th.result


if __name__ == "__main__":
    print(plan(facts(measure="--measure" in sys.argv))["text"])
