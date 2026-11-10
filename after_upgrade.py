"""After a hardware upgrade: what Architect now sees, how it would place the models, and how fast they really are.

    python after_upgrade.py          # the full check (a few minutes: it loads the heavy models one at a time)
    python after_upgrade.py --quick  # what it sees and its plan only, no model is loaded

Writes hub_data/after_upgrade_<date>.md and .json; a full run is compared with the previous full run, so the report says what the
upgrade changed (before -> after). Changes no setting: it measures, then the person decides (for example which model the planner,
architect and evaluator roles should use). Runs as one heavy job, below normal priority.
"""
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
import arch_core as core  # noqa: E402
import arch_heavy  # noqa: E402
import arch_hw  # noqa: E402
import arch_server as S  # noqa: E402

MOE = "eriktonon/qwen3.6-moe-uncensored:iq3"      # the mixture-of-experts model already installed here
GB = 1024 ** 3
PROMPTS = {"write": "Write a Python function parse_duration(text) that turns '1h30m', '45s', '2h' into seconds, with a docstring and "
                    "three doctests. /no_think",
           "edit": "Return this code unchanged except: rename the function fp to fingerprint everywhere.\n\n"
                   + open(os.path.join(HERE, "arch_progress.py"), encoding="utf-8").read()[:2400] + "\n/no_think"}
lines = []


def out(s=""):
    print(s, flush=True)
    lines.append(s)


def memory_sticks():
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_PhysicalMemory | ForEach-Object { '{0}|{1}|{2}' -f [math]::Round($_.Capacity/1GB), $_.DeviceLocator, "
          "$_.ConfiguredClockSpeed }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
        return [ln.split("|") for ln in r.stdout.splitlines() if ln.count("|") == 2]
    except (OSError, subprocess.SubprocessError):
        return []


def disks():
    if os.name != "nt":
        return []
    ps = "Get-PhysicalDisk | ForEach-Object { '{0}|{1}|{2}' -f $_.FriendlyName, $_.BusType, [math]::Round($_.Size/1GB) }"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
        return [ln.split("|") for ln in r.stdout.splitlines() if ln.count("|") == 2]
    except (OSError, subprocess.SubprocessError):
        return []


def disk_read(path, gb=2.0, block=8 << 20):
    """Read the first `gb` of a model file twice. The first pass is mostly the drive (unless Windows still has the file in its RAM
    cache), the second is the RAM cache: what reloading a model costs when RAM is big enough to keep its file. -> (first, again) GB/s."""
    n = int(gb * GB)

    def once():
        t0, got = time.perf_counter(), 0
        with open(path, "rb", buffering=0) as f:
            while got < n:
                b = f.read(block)
                if not b:
                    break
                got += len(b)
        return round(got / GB / max(time.perf_counter() - t0, 1e-6), 2)
    return once(), once()


def previous(kind="json"):
    """The newest earlier full run's results, or None."""
    runs = sorted(glob.glob(os.path.join(core.data_dir(), "after_upgrade_*." + kind)))
    for p in reversed(runs):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("speed"):
                return d
        except (OSError, ValueError):
            continue
    return None


def speed(url, tier, prompt, n):
    body = {"model": tier, "stream": False, "max_tokens": n, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t = json.loads(urllib.request.urlopen(req, timeout=900).read()).get("timings") or {}
    return round(t.get("predicted_per_second") or 0, 1)


def measure(cfg, hw, tier, label):
    """Start one tier alone on the card, time a write and an edit, stop it. -> {"write", "edit"} tokens/s, or a reason."""
    S.stop(["worker", "heavy"], say=lambda m: None)
    t0 = time.time()
    st = S.start(cfg, hw, tiers=[tier], heavy=(tier == "heavy"), say=lambda m: None, watch=False)
    load_s = round(time.time() - t0, 1)
    rec = (st.get("processes") or {}).get(tier) or {}
    if not rec.get("healthy"):
        return {"error": "did not start (see hub_data/server/" + tier + ".log)"}
    try:
        r = {k: speed(rec["url"], tier, p, 400 if k == "edit" else 240) for k, p in PROMPTS.items()}
        r["load_s"] = load_s
        return r
    except Exception as e:                                   # a model that is too slow or failed mid-reply: say so, keep going
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        S.stop([tier], say=lambda m: None)


def main():
    quick = "--quick" in sys.argv
    cfg = core.load_config()
    hw = arch_hw.detect()
    out(f"# After the upgrade: {time.strftime('%Y-%m-%d %H:%M')}")
    out()
    out("## What Architect sees")
    sticks = memory_sticks()
    for gb, slot, mts in sticks:
        out(f"- RAM stick: {gb} GB in {slot} at {mts} MT/s")
    if sticks:
        both = len(sticks) >= 2 and len({s[0] for s in sticks}) == 1 and len({s[2] for s in sticks}) == 1
        out(f"- {'Dual channel: matched sticks, so RAM bandwidth roughly doubles' if both else 'Check: one stick, or sticks that do not match (single channel or a lower speed)'}")
    out(f"- RAM: {hw.get('ram_gb', 0):.1f} GB, {(format(hw['ram_free_gb'], '.1f') + ' GB') if hw.get('ram_free_gb') is not None else 'unknown amount'} free now; graphics card: {hw.get('gpu')} {hw.get('vram_gb', 0):.1f} GB; "
        f"performance cores: {hw.get('cpu_perf_cores')}")
    for name, bus, gb in disks():
        out(f"- Drive: {name} ({bus}, {gb} GB)")
    data = {"ts": time.time(), "sticks": sticks, "ram_gb": hw.get("ram_gb"), "disk": {}, "speed": {}}
    try:
        import shutil
        mdir = arch_hw.models_dir() if hasattr(arch_hw, "models_dir") else os.path.expanduser("~/.ollama/models")
        du = shutil.disk_usage(mdir)
        data["disk"]["models_free_gb"] = round(du.free / GB)
        out(f"- Models folder: {mdir} ({round(du.free / GB)} GB free of {round(du.total / GB)} GB)"
            + ("; a drive this full writes slower (QLC drives most of all), reading models is not affected" if du.free < 0.25 * du.total else ""))
    except Exception:
        pass
    out()
    out("## How Architect would place the models")
    for title, c in (("your current setup", cfg), (f"with {MOE} as the heavy model", core.deep_merge(cfg, {"tiers": {"heavy": MOE}}))):
        pl = S.plan(c, hw)
        out(f"**{title}**")
        for p in pl["processes"]:
            out(f"- {p['tier']}: {p['name']}: {p['why']}")
        for n in pl["notes"]:
            out(f"- note: {n}")
        out()
    if quick:
        out("(quick check: no model was loaded)")
        return 0
    out("## The drive and the RAM cache")
    big = next((p for p in ((S.resolve_model(m, cfg) or {}).get("gguf") for m in (MOE, (cfg.get("tiers") or {}).get("heavy"))) if p and os.path.isfile(p)), None)
    if big:
        first, again = disk_read(big)
        data["disk"].update(read_first_gb_s=first, read_again_gb_s=again)
        if again >= 3 and again > 1.8 * first:
            why = "the second read came from Windows' RAM cache: with more RAM, more model files stay there and models load faster"
        elif first >= 3:
            why = "both came from the RAM cache (the file was read recently), so neither number is the drive"
        else:
            why = "probably both from the drive; one simple read, not a full drive benchmark"
        out(f"- Reading 2 GB of a model file: {first} GB/s the first time, {again} GB/s again ({why})")
    out()
    out("## Measured speed (tokens per second; one model on the card at a time)")
    with arch_heavy.one_at_a_time("tune", cfg):
        was_up = [p["tier"] for p in S.status(cfg)["processes"] if p["up"]]
        rows = [("worker", cfg, "worker", "the worker"), ("heavy 14B", cfg, "heavy", "the 14B"),
                ("heavy MoE", core.deep_merge(cfg, {"tiers": {"heavy": MOE}}), "heavy", "the 35B mixture-of-experts")]
        res = {}
        for key, c, tier, label in rows:
            need = next((p.get("ram_gb", 0) for p in S.plan(c, hw)["processes"] if p["tier"] == tier), 0)
            free = arch_hw.live().get("ram_free_gb") or 0
            if need and need > free - 1.5:
                res[key] = {"error": f"skipped: needs about {need} GB of RAM, {free} GB free"}
            else:
                out(f"- measuring {label} ...")
                res[key] = measure(c, hw, tier, label)
            r = res[key]
            out(f"  - {label}: " + (r["error"] if "error" in r else f"new code {r['write']}, edit {r['edit']}, started in {r['load_s']} s"))
        if was_up:
            S.ensure(cfg, say=lambda m: None)                  # the resident tiers back as they were
        else:
            S.stop(say=lambda m: None)                         # nothing was running before the check: nothing is left running
    data["speed"] = res
    try:                                                     # the measured speeds give this machine's real memory bandwidth
        pw = next(p for p in S.plan(cfg, hw)["processes"] if p["tier"] == "worker")
        ph = next(p for p in S.plan(cfg, hw)["processes"] if p["tier"] == "heavy")
        w, h = res.get("worker") or {}, res.get("heavy 14B") or {}
        if pw.get("full_gpu") and w.get("write"):
            bw = arch_hw.bandwidth_from(w["write"], pw["size_gb"], h.get("write"), ph.get("est_gpu_gb"), ph.get("ram_gb"))
            if bw["gpu_bw"]:
                arch_hw.save_bandwidth(bw, f"after_upgrade {time.strftime('%Y-%m-%d %H:%M')}")
                out(f"- Memory speed worked out from these runs: graphics card {bw['gpu_bw']} GB/s, RAM {bw['ram_bw']} GB/s "
                    "(used from now on for every speed estimate)")
    except Exception:
        pass
    before = previous()
    if before:
        out()
        out(f"## Compared with the run of {time.strftime('%Y-%m-%d %H:%M', time.localtime(before['ts']))}")
        out(f"- RAM sticks: {len(before.get('sticks') or [])} -> {len(sticks)}; RAM {before.get('ram_gb', 0):.1f} -> {hw.get('ram_gb', 0):.1f} GB")
        for key in ("worker", "heavy 14B", "heavy MoE"):
            a, b = (before.get("speed") or {}).get(key) or {}, res.get(key) or {}
            if b and "error" not in b and (not a or "error" in a):
                out(f"- {key}: not measured before ({a.get('error') or 'no earlier run'}); now new code {b['write']}, edit {b['edit']} tokens/s, "
                    f"started in {b.get('load_s')} s")
                continue
            if "error" in a or "error" in b or not a or not b:
                out(f"- {key}: " + (b.get("error") or a.get("error") or "not measured both times"))
                continue
            out(f"- {key}: new code {a.get('write', '?')} -> {b.get('write', '?')} tokens/s, edit {a.get('edit') or '?'} -> "
                f"{b.get('edit') or '?'}, started in {a.get('load_s') or '?'} -> {b.get('load_s') or '?'} s"
                + (f" (before: {a['note']})" if a.get("note") else ""))
        da, db = before.get("disk") or {}, data["disk"]
        if da.get("read_again_gb_s") and db.get("read_again_gb_s"):
            out(f"- model file read: {da.get('read_first_gb_s')} -> {db.get('read_first_gb_s')} GB/s first, "
                f"{da['read_again_gb_s']} -> {db['read_again_gb_s']} GB/s again")
    try:
        with open(os.path.join(core.data_dir(), f"after_upgrade_{time.strftime('%Y%m%d_%H%M')}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass
    w14, wm = res.get("heavy 14B", {}), res.get("heavy MoE", {})
    if "error" not in wm and "error" not in w14:
        better = MOE if wm["write"] > w14["write"] else "the 14B"
        out()
        out(f"**Suggestion:** {better} is the faster heavy model here ({wm['write']} vs {w14['write']} tokens/s on new code). "
            "Tell Claude which model the planner, architect and evaluator should use; nothing was changed.")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        path = os.path.join(core.data_dir(), f"after_upgrade_{time.strftime('%Y%m%d_%H%M')}.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print(f"\nReport saved: {path}")
        except OSError:
            pass
    sys.exit(rc)
