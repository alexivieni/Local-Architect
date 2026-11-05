"""Self-test for arch_placement: where work and data go, decided from made-up machines. No hardware is read and nothing outside a temporary
folder is written."""
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _laptop_hw(ram_free=4.3):
    """The owner's laptop as the snapshot of 2026-11-04 saw it (docs/pc_snapshot_20261104.txt)."""
    return {"gpu": "NVIDIA GeForce RTX 4050 Laptop GPU", "vendor": "nvidia", "vram_gb": 6.0, "vram_free_gb": 0.63, "ram_gb": 31.6,
            "ram_free_gb": ram_free, "cpu_cores": 12, "cpu_perf_cores": 4, "unified": False, "models_dir": "C:\\Users\\a\\.ollama\\models"}


def _laptop_drives():
    return [
        {"mount": "D:\\", "fs": "FAT32", "total_gb": 32.0, "free_gb": 31.9, "bus": "usb", "removable": True, "name": "Generic", "suspect": ""},
        {"mount": "E:\\", "fs": "exFAT", "total_gb": 1048.5, "free_gb": 1048.5, "bus": "usb", "removable": True, "name": "YGS ProductCode",
         "suspect": "it reports exactly 1,000,000 MiB"},
        {"mount": "C:\\", "fs": "NTFS", "total_gb": 999.1, "free_gb": 239.7, "bus": "nvme", "removable": False, "name": "CT1000", "suspect": ""},
    ]


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_hw
    import arch_placement as P

    ECORES = 0xF00                                                   # logical processors 8-11, as on the i5-13420H
    BW = {"gpu_bw": 158.0, "ram_bw": 19.5}

    def lap(**kw):
        a = dict(hw=_laptop_hw(), drs=_laptop_drives(), speeds={}, bw=BW, ecores=ECORES, models="C:\\Users\\a\\.ollama\\models",
                 data_dir=tempfile.gettempdir(), app_dir=tempfile.gettempdir(), heavy_ram_gb=11.57)
        a.update(kw)
        return P.facts(**a)

    # ---- compute on the owner's laptop
    p = P.plan(lap())
    by = {r["work"].split(" (")[0]: r for r in p["compute"]}
    check("worker goes on the graphics card", by["worker"]["place"] == "gpu", by["worker"])
    check("worker's reason uses the measured 158 vs 19.5 GB/s", "158" in by["worker"]["why"] and "19.5" in by["worker"]["why"]
          and by["worker"]["evidence"] == "measured", by["worker"])
    check("router on the performance cores, half of 4 = 2 threads", by["router"]["place"] == "cpu-p" and by["router"]["threads"] == 2, by["router"])
    check("tests on efficiency cores 8-11", by["test runs"]["place"] == "cpu-e" and "8-11" in by["test runs"]["why"]
          and by["test runs"]["threads"] == 4, by["test runs"])
    check("every background chore goes on the efficiency cores", all(by[j]["place"] == "cpu-e" for j in P.BACKGROUND), [by[j] for j in P.BACKGROUND])
    check("heavy experts in RAM, with the heavy_beside advice when RAM is tight", by["heavy tier experts"]["place"] == "ram"
          and "heavy_beside = off" in by["heavy tier experts"]["why"], by["heavy tier experts"])
    p_free = P.plan(lap(hw=_laptop_hw(ram_free=20.0)))
    hv = next(r for r in p_free["compute"] if r["work"].startswith("heavy"))
    check("no heavy_beside advice when RAM is free", "heavy_beside" not in hv["why"], hv)
    check("caches in RAM", by["caches"]["place"] == "ram", by["caches"])

    # ---- storage on the owner's laptop
    st = p["storage"]
    tiers = {d["mount"]: d["tier"] for d in st["drives"]}
    check("C: is hot, D: cold, E: refused (fake capacity)", tiers == {"C:\\": "hot", "D:\\": "cold", "E:\\": "refused"}, tiers)
    check("all hot data on C:", all(r["mount"] == "C:\\" for r in st["data"] if r["temp"] == "hot"), st["data"])
    check("models never on a cold drive", next(r for r in st["data"] if r["kind"] == "models")["mount"] == "C:\\")
    check("all cold data on D:", all(r["mount"] == "D:\\" for r in st["data"] if r["temp"] == "cold"), st["data"])
    check("FAT32's 4 GB limit is said", all("FAT32" in r["why"] for r in st["data"] if r["temp"] == "cold"))
    check("nothing goes on E:", all(r["mount"] != "E:\\" for r in st["data"]) and any("E:" in n for n in p["notes"]), p["notes"])
    check("plain words + table", "Where the work runs" in p["text"] and "In plain words:" in p["text"] and "D:\\" in p["text"], p["text"][-300:])
    check("no measured drive speed is said", any("not measured" in n for n in p["notes"]), p["notes"])

    # ---- a drive that fails its speed test, or is too slow
    bad = P.plan(lap(speeds={"D:\\": {"write_mb_s": 9.0, "read_mb_s": 40.0, "ok": False}}))
    check("D: that reads back wrong bytes is refused", {d["mount"]: d["tier"] for d in bad["storage"]["drives"]}["D:\\"] == "refused")
    check("then cold data stays on C: and the plan says why", bad["storage"]["cold"] is None
          and all(r["mount"] == "C:\\" for r in bad["storage"]["data"]) and any("No trustworthy" in n for n in bad["notes"]), bad["notes"])
    slow = P.plan(lap(speeds={"D:\\": {"write_mb_s": 1.0, "read_mb_s": 20.0, "ok": True}}))
    check("D: writing at 1 MB/s is refused", slow["storage"]["cold"] is None)
    okd = P.plan(lap(speeds={"D:\\": {"write_mb_s": 9.0, "read_mb_s": 40.0, "ok": True}}))
    check("a healthy measured card stays the cold drive, speed named", okd["storage"]["cold"] == "D:\\"
          and "9.0 MB/s" in next(d["why"] for d in okd["storage"]["drives"] if d["mount"] == "D:\\"))

    # ---- drive_roles stays compatible without speeds
    plain = arch_hw.drive_roles([dict(d) for d in _laptop_drives()], models="C:\\Users\\a\\.ollama\\models")
    check("drive_roles without speeds: same roles as before", [d["role"] for d in plain] == ["archive", "none", plain[2]["role"]]
          and plain[2]["role"] in ("models", "scratch"), [d["role"] for d in plain])

    # ---- a fixed slow internal drive beats a removable one for cold data
    hdd = {"mount": "F:\\", "fs": "NTFS", "total_gb": 2000, "free_gb": 1500, "bus": "sata", "removable": False, "name": "HDD", "suspect": ""}
    w = P.plan(lap(drs=_laptop_drives() + [hdd], speeds={"F:\\": {"write_mb_s": 140, "read_mb_s": 150, "ok": True}}))
    check("a 150 MB/s internal disk is warm and takes the cold data", {d["mount"]: d["tier"] for d in w["storage"]["drives"]}["F:\\"] == "warm"
          and w["storage"]["cold"] == "F:\\", w["storage"]["cold"])
    check("hot data still on C: with a warm disk present", all(r["mount"] == "C:\\" for r in w["storage"]["data"] if r["temp"] == "hot"))

    # ---- a CPU-only desktop without efficiency cores, nothing measured
    cpu = P.plan(P.facts(hw={"vendor": None, "vram_gb": 0, "ram_gb": 16, "ram_free_gb": 10, "cpu_cores": 8, "cpu_perf_cores": 8},
                         drs=[{"mount": "/", "fs": "ext4", "total_gb": 500, "free_gb": 100, "bus": "nvme", "removable": False, "suspect": ""}],
                         speeds={}, bw={}, ecores=0, models="/home/a/.ollama/models", data_dir=tempfile.gettempdir()))
    cb = {r["work"].split(" (")[0]: r for r in cpu["compute"]}
    check("no card: worker on the performance cores", cb["worker"]["place"] == "cpu-p", cb["worker"])
    check("no efficiency cores: tests on the performance cores, chores low-priority anywhere",
          cb["test runs"]["place"] == "cpu-p" and all(cb[j]["place"] == "cpu" for j in P.BACKGROUND))
    check("unmeasured bandwidth is labelled an estimate", cb["heavy tier experts"]["evidence"] == "estimate"
          and any("estimates" in n for n in cpu["notes"]), cpu["notes"])
    check("one drive only: cold data stays on it", cpu["storage"]["cold"] is None and all(r["mount"] == "/" for r in cpu["storage"]["data"]))

    # ---- apply_storage on a temporary 'machine'
    tmp = tempfile.mkdtemp(prefix="arch_place_")
    try:
        cmount, dmount = os.path.join(tmp, "C") + os.sep, os.path.join(tmp, "D") + os.sep
        data, app = os.path.join(cmount, "hub"), os.path.join(cmount, "app")
        os.makedirs(os.path.join(data, "datasets"))
        os.makedirs(dmount)
        with open(os.path.join(data, "datasets", "sft.jsonl"), "w") as fh:
            fh.write('{"a": 1}\n' * 50)
        with open(os.path.join(data, "ledger.json.bak"), "w") as fh:
            fh.write("[]")
        with open(os.path.join(data, "live.log.1"), "w") as fh:
            fh.write("{}\n")
        cands = os.path.join(app, ".arch_self", "candidates")
        for n in ("20261101_000000", "20261102_000000", "20261103_000000"):
            os.makedirs(os.path.join(cands, n, "architect"))
            with open(os.path.join(cands, n, "architect", "x.py"), "w") as fh:
                fh.write(f"# {n}\n")
        drs = [{"mount": cmount, "fs": "NTFS", "total_gb": 999, "free_gb": 200, "bus": "nvme", "removable": False, "suspect": ""},
               {"mount": dmount, "fs": "FAT32", "total_gb": 32, "free_gb": 30, "bus": "usb", "removable": True, "suspect": ""}]
        f = P.facts(hw=_laptop_hw(), drs=drs, speeds={}, bw=BW, ecores=ECORES, models=os.path.join(cmount, "models"), data_dir=data, app_dir=app)
        writes = []
        cfg = {"storage": {"archive": ""}}
        dry = P.apply_storage(f=f, cfg=cfg, write_cfg=writes.append)
        kinds = sorted(m["kind"] for m in dry["moves"])
        check("dry run proposes storage.archive = the card", dry["settings"] == [{"set": "storage.archive", "from": "", "to": dmount}], dry["settings"])
        check("dry run finds exports, backups, rollovers and the one old self-build",
              kinds == ["dataset_exports", "logbook_backups", "self_builds", "timeline_rollovers"], kinds)
        check("only the oldest self-build is listed (newest 2 kept)",
              [os.path.basename(m["src"]) for m in dry["moves"] if m["kind"] == "self_builds"] == ["20261101_000000"])
        check("dry run writes nothing", not writes and not os.listdir(dmount) and cfg["storage"]["archive"] == ""
              and os.path.isdir(os.path.join(cands, "20261101_000000")) and not os.path.exists(os.path.join(data, "placement_undo.json")))
        same = P.apply_storage(f=f, cfg={"storage": {"archive": dmount}}, write_cfg=writes.append)
        check("archive already set: no setting change proposed", same["settings"] == [], same["settings"])

        done = P.apply_storage(f=f, cfg=cfg, dry_run=False, write_cfg=writes.append)
        check("applied: the setting is written once", len(writes) == 1 and writes[0]["storage"]["archive"] == dmount, writes)
        check("every item copied and verified", done["done"] and all(d["ok"] for d in done["done"]), done["done"])
        copied = os.path.join(dmount, P.COLD_DIR, "dataset_exports", "datasets", "sft.jsonl")
        check("an export is a copy: both files exist and match", os.path.isfile(copied) and os.path.isfile(os.path.join(data, "datasets", "sft.jsonl"))
              and open(copied).read() == open(os.path.join(data, "datasets", "sft.jsonl")).read())
        moved = os.path.join(dmount, P.COLD_DIR, "self_builds", "20261101_000000", "architect", "x.py")
        check("the old self-build moved: copy on the card, source gone, newest two untouched", os.path.isfile(moved)
              and not os.path.exists(os.path.join(cands, "20261101_000000"))
              and sorted(os.listdir(cands)) == ["20261102_000000", "20261103_000000"])
        check("an undo record is written", os.path.isfile(os.path.join(data, "placement_undo.json")), done.get("undo"))
        undo_writes = []
        u = P.undo_storage(f=f, cfg=cfg, write_cfg=undo_writes.append)
        check("undo restores the old setting", u["settings"] == 1 and undo_writes and undo_writes[-1]["storage"]["archive"] == "", u)
        check("undo puts the moved self-build back", os.path.isfile(os.path.join(cands, "20261101_000000", "architect", "x.py")), u)

        # FAT32 limit and room
        old_max = P.FAT32_MAX
        P.FAT32_MAX = 10                                       # stands in for 4 GB: every file of the self-build is 'too big'
        try:
            big = P.apply_storage(f=f, cfg=cfg, dry_run=False, write_cfg=lambda c: None,
                                  sources=[("self_builds", "move", os.path.join(cands, "20261102_000000"), "20261102_000000")])
        finally:
            P.FAT32_MAX = old_max
        check("FAT32: an oversized file is skipped and the source is NOT removed", big["moves"][0].get("skip_files")
              and not big["done"][0]["removed"] and os.path.isdir(os.path.join(cands, "20261102_000000")), big["moves"])
        small = [dict(drs[0]), dict(drs[1], free_gb=1.0)]
        f2 = P.facts(hw=_laptop_hw(), drs=small, speeds={}, bw=BW, ecores=ECORES, models=os.path.join(cmount, "models"), data_dir=data, app_dir=app)
        tight = P.apply_storage(f=f2, cfg=cfg, write_cfg=lambda c: None)
        check("no room after the 1 GB reserve: every item is skipped with a reason", tight["moves"] and all(m.get("skip") for m in tight["moves"]),
              tight["moves"])
        nod = P.apply_storage(f=P.facts(hw=_laptop_hw(), drs=[drs[0]], speeds={}, bw=BW, ecores=ECORES, models=os.path.join(cmount, "models"),
                                        data_dir=data, app_dir=app), cfg=cfg, dry_run=False, write_cfg=writes.append)
        check("no cold drive: nothing applied", nod["cold"] is None and not nod["moves"] and not nod["settings"] and len(writes) == 1, nod)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- run_low
    class FakeOS:
        def __init__(self):
            self.masks, self.lowered = [], 0

        def affinity(self, m):
            self.masks.append(m)
            return True

        def lower(self):
            self.lowered += 1
            return True

    fo = FakeOS()
    seen = {}
    r = P.run_low(lambda a, b=0: seen.setdefault("t", threading.get_ident()) and a + b, 2, b=3, mask=ECORES, api=fo)
    check("run_low returns fn's result", r == 5, r)
    check("run_low runs on its own thread", seen.get("t") != threading.get_ident())
    check("run_low pins to the efficiency mask and lowers priority", fo.masks == [ECORES] and fo.lowered == 1, (fo.masks, fo.lowered))
    fo2 = FakeOS()
    P.run_low(lambda: None, mask=0, api=fo2)
    check("no efficiency cores: no pinning, still lowered", fo2.masks == [] and fo2.lowered == 1)

    def boom():
        raise ValueError("x")
    try:
        P.run_low(boom, mask=0, api=FakeOS())
        raised = False
    except ValueError:
        raised = True
    check("run_low hands fn's exception to the caller", raised)
    th = P.run_low(lambda: 7, wait=False, mask=0, api=FakeOS())
    th.join(5)
    check("wait=False returns the thread with its result", th.result == 7 and th.pinned.get("priority"), (th.result, th.pinned))
    check("run_low on the real OS without pinning works", P.run_low(lambda: "ok", mask=0) == "ok")
    check("child-process priority flag", P.low_creationflags() == (0x4000 if os.name == "nt" else 0))

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' PLACEMENT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' PLACEMENT CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
