"""Laptop awareness: is there a battery, is it in use, and what should the harness do about it.

Read-only detection (files under /sys on Linux, `pmset` on macOS, a PowerShell query on Windows), all injectable for tests.
The battery saver only trims the run's in-memory settings (it never edits your saved settings file) and says so once.
"""
import glob
import json
import os
import re
import subprocess
import sys


def _run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def battery(run=_run, sys_root="/sys/class/power_supply", platform=None):
    """-> {"present": bool, "on_battery": bool|None, "pct": int|None, "source": str}. present=False on desktops and when unknown."""
    plat = platform or sys.platform
    out = {"present": False, "on_battery": None, "pct": None, "source": "none"}
    if plat.startswith("linux"):
        bats = sorted(glob.glob(os.path.join(sys_root, "BAT*")))
        if not bats:
            return out
        try:
            status = open(os.path.join(bats[0], "status")).read().strip().lower()
            cap = open(os.path.join(bats[0], "capacity")).read().strip()
        except OSError:
            return out
        out.update(present=True, on_battery=(status == "discharging"), pct=int(cap) if cap.isdigit() else None, source="sysfs")
    elif plat == "darwin":
        rc, txt = run(["pmset", "-g", "batt"])
        if rc == 0 and "InternalBattery" in txt:
            m = re.search(r"(\d+)%", txt)
            out.update(present=True, on_battery=("discharging" in txt.lower() and "ac power" not in txt.lower().split("\n")[0]),
                       pct=int(m.group(1)) if m else None, source="pmset")
    elif plat.startswith("win"):
        rc, txt = run(["powershell", "-NoProfile", "-Command",
                       "Get-CimInstance Win32_Battery | Select-Object BatteryStatus,EstimatedChargeRemaining | ConvertTo-Json -Compress"])
        try:
            d = json.loads(txt) if rc == 0 and txt.strip() else None
            d = d[0] if isinstance(d, list) and d else d
        except ValueError:
            d = None
        if isinstance(d, dict):
            out.update(present=True, on_battery=(d.get("BatteryStatus") == 1), pct=d.get("EstimatedChargeRemaining"), source="powershell")
    return out


def tips(hw, power):
    """Plain advice for laptops and small machines, from the detected hardware. -> list of strings."""
    out = []
    vram, ram = hw.get("vram_gb") or 0, hw.get("ram_gb") or 0
    integrated = not vram or (hw.get("unified") and not hw.get("gpus"))
    if power.get("present"):
        out.append("Laptop detected. Tune and the speeds it records depend on the power state: run `tune` plugged in to know the speed you get on AC. Many laptops lower the GPU and CPU clocks on battery.")
        if power.get("on_battery"):
            out.append("On battery now: the battery saver (Settings > behavior.battery_saver = auto) shortens the context and keep-alive for this run and turns off sampled retries.")
    if not vram:
        out.append("No dedicated GPU memory was found: models run on the CPU. Prefer a 3B-8B model at Q4, keep the context at 4096-8192, and expect a few tokens per second.")
    elif vram < 8 and not hw.get("unified"):
        out.append(f"{vram:.0f} GB of GPU memory is tight: a 4B-8B Q4 model fits, larger ones will spill to the CPU. A mixture-of-experts model needs all its weights in memory even though few are active.")
    if ram and ram <= 16:
        out.append(f"{ram:.0f} GB of RAM: close other heavy apps while a model is loaded; one loaded model at a time (OLLAMA_MAX_LOADED_MODELS=1).")
    if integrated and ram and ram >= 24:
        out.append("Plenty of system RAM: a larger MoE model can run from RAM at usable speed because only a few experts are computed per token.")
    if (hw.get("cpu_cores") or 0) >= 4 and integrated:
        out.append("CPU-bound: `tune --deep` also measures thread counts and keeps one only when it is at least 10% faster.")
    out.append("Offload: if you have a stronger machine on your network running Ollama, add it with `python architect.py hosts --add name=http://ip:11434` and route heavy roles to it.")
    return out


def saver(cfg, power):
    """Trim THIS run's settings when on battery. Mutates the in-memory cfg only. -> list of what changed (empty = nothing to do)."""
    b = cfg.get("behavior") or {}
    if b.get("battery_saver", "auto") != "auto" or not (power or {}).get("on_battery"):
        return []
    lim = cfg.setdefault("limits", {})
    done = []
    if int(lim.get("num_ctx", 0) or 0) > 8192:
        lim["num_ctx"] = 8192
        done.append("context 8192")
    if lim.get("keep_alive") not in ("2m", "1m", "0"):
        lim["keep_alive"] = "2m"
        done.append("keep-alive 2m")
    if int(b.get("best_of_n", 1) or 1) > 1:
        cfg["behavior"]["best_of_n"] = 1
        done.append("no sampled retries")
    if b.get("evaluator") == "each":
        cfg["behavior"]["evaluator"] = "final"
        done.append("judge at the end only")
    return done
