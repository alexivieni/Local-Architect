"""Hardware detection for tuning: GPU (NVIDIA, AMD, Apple unified memory), RAM, CPU cores, and free disk where models
live, plus a fingerprint so the Architect can tell when the machine changed and a re-tune is due.

Every probe is a real command or syscall with a short timeout. `detect(run=...)` accepts an injectable command
runner so the parsing can be tested without the hardware. Nothing here is guessed silently: what could not be read
is listed in `notes`.
"""
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time

GB = 1024 ** 3


def _run(cmd, timeout=8):
    try:
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if os.name == "nt" else {}
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _nvidia(run):
    rc, out = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"])
    if rc != 0 or not out.strip():
        return None
    gpus = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3:
            try:
                gpus.append({"name": parts[0], "vram_gb": float(parts[1]) / 1024, "free_gb": float(parts[2]) / 1024, "vendor": "nvidia"})
            except ValueError:
                continue
    return gpus or None


def _win_gpu(run):
    """Windows WMI fallback for discrete AMD Radeon / Intel Arc GPUs when nvidia-smi and rocm-smi are not present."""
    rc, out = run(["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"])
    if rc != 0 or not out.strip():
        return None
    import json
    try:
        items = json.loads(out)
        items = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
    except ValueError:
        return None
    gpus = []
    for d in items:
        name = str(d.get("Name") or "").strip()
        ram_b = int(d.get("AdapterRAM") or 0)
        if ram_b < 2 * GB or re.search(r"microsoft basic|parsec|virtual|remote|uhd graphics|iris", name, re.I):
            continue
        v = round(ram_b / GB, 2)
        vendor = "amd" if re.search(r"amd|radeon", name, re.I) else ("intel" if re.search(r"intel|arc", name, re.I) else "other")
        gpus.append({"name": name, "vram_gb": v, "free_gb": round(v * 0.85, 2), "vendor": vendor})
    return gpus or None


def _amd(run):
    rc, out = run(["rocm-smi", "--showmeminfo", "vram", "--showproductname", "--json"])
    if rc != 0 or not out.strip():
        return None
    import json
    try:
        j = json.loads(out)
    except ValueError:
        return None
    gpus = []
    for card, d in j.items():
        if not isinstance(d, dict):
            continue
        tot = next((v for k, v in d.items() if "Total Memory" in k and "VRAM" in k), None)
        used = next((v for k, v in d.items() if "Total Used Memory" in k and "VRAM" in k), 0)
        name = d.get("Card Series") or d.get("Card series") or d.get("Card model") or card
        try:
            t, u = float(tot) / GB, float(used) / GB
        except (TypeError, ValueError):
            continue
        gpus.append({"name": str(name), "vram_gb": t, "free_gb": max(t - u, 0), "vendor": "amd"})
    return gpus or None


def _ram_gb():
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tot", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("x", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.tot / GB, m.avail / GB
        if os.path.exists("/proc/meminfo"):
            mi = {}
            for ln in open("/proc/meminfo"):
                k, v = ln.split(":", 1)
                mi[k] = int(v.split()[0]) * 1024
            return mi["MemTotal"] / GB, mi.get("MemAvailable", mi["MemFree"]) / GB
        rc, out = _run(["sysctl", "-n", "hw.memsize"])
        if rc == 0 and out.strip():
            return int(out.strip()) / GB, None
    except (OSError, ValueError, KeyError, IndexError):          # an unreadable /proc/meminfo or sysctl: report "unknown", never guess
        return 0.0, None
    return 0.0, None


def ecore_mask():
    """The logical processors of the efficiency cores on a hybrid CPU (Intel 12th gen and later), as an affinity bit mask; 0 when the
    CPU is not hybrid or it cannot be read. Tests, linters and scanners run there, so the performance cores stay free for the model
    servers that generate code at the same time."""
    try:
        if os.name == "nt":
            import ctypes
            import struct
            k = ctypes.windll.kernel32
            n = ctypes.c_ulong(0)
            k.GetLogicalProcessorInformationEx(0, None, ctypes.byref(n))
            buf = (ctypes.c_byte * n.value)()
            if not n.value or not k.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(n)):
                return 0
            raw, off, recs = bytes(buf), 0, []
            while off + 40 <= n.value:
                rel, size = struct.unpack_from("<II", raw, off)
                if size <= 0:
                    break
                if rel == 0:
                    group = struct.unpack_from("<H", raw, off + 40)[0]
                    if group == 0:                                           # group 0 only: plenty for any laptop or desktop
                        recs.append((raw[off + 9], struct.unpack_from("<Q", raw, off + 32)[0]))
                off += size
            classes = {c for c, _ in recs}
            if len(classes) < 2:
                return 0
            top = max(classes)
            mask = 0
            for c, m in recs:
                if c < top:
                    mask |= m
            return mask
        txt = open("/sys/devices/cpu_atom/cpus").read().strip()                 # Linux, Intel hybrid
        mask = 0
        for part in txt.split(","):
            lo, _, hi = part.partition("-")
            for i in range(int(lo), int(hi or lo) + 1):
                mask |= 1 << i
        return mask
    except (OSError, ValueError, AttributeError):
        return 0


def pin_to_ecores(pid_or_handle, mask=None):
    """Move a started process onto the efficiency cores. -> True when done. Never raises."""
    mask = ecore_mask() if mask is None else mask
    if not mask:
        return False
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.kernel32.SetProcessAffinityMask(int(pid_or_handle), ctypes.c_size_t(mask)))
        os.sched_setaffinity(int(pid_or_handle), {i for i in range(mask.bit_length()) if mask >> i & 1})
        return True
    except (OSError, AttributeError, ValueError):
        return False


def perf_cores():
    """Physical performance cores (P-cores on a hybrid Intel chip; all physical cores otherwise), or None when it cannot be read.
    Generating tokens on the CPU is limited by memory bandwidth and its threads wait for each other at every layer, so the slow
    efficiency cores and the second hyperthread of a core set the pace: about one thread per performance core is fastest.
    Seen live: an i5-13420H (4 P-cores with hyperthreads + 4 E-cores = 12 threads) had every model server asking for 11."""
    try:
        if os.name == "nt":
            import ctypes
            import struct
            k = ctypes.windll.kernel32
            n = ctypes.c_ulong(0)
            k.GetLogicalProcessorInformationEx(0, None, ctypes.byref(n))          # 0 = RelationProcessorCore: one record per core
            buf = (ctypes.c_byte * n.value)()
            if not n.value or not k.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(n)):
                return None
            raw, off, classes = bytes(buf), 0, []
            while off + 10 <= n.value:
                rel, size = struct.unpack_from("<II", raw, off)
                if size <= 0:
                    break
                if rel == 0:
                    classes.append(raw[off + 9])                                  # PROCESSOR_RELATIONSHIP.EfficiencyClass
                off += size
            if not classes:
                return None
            top = max(classes)
            return sum(1 for c in classes if c == top)
        if os.path.isdir("/sys/devices/system/cpu"):
            allowed = os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None
            pcpus = None
            try:                                                                  # Intel hybrid on Linux: the P-core CPU list
                txt = open("/sys/devices/cpu_core/cpus").read().strip()
                pcpus = set()
                for part in txt.split(","):
                    lo, _, hi = part.partition("-")
                    pcpus.update(range(int(lo), int(hi or lo) + 1))
            except (OSError, ValueError):
                pcpus = None
            caps, cores = {}, set()
            for d in os.listdir("/sys/devices/system/cpu"):
                if not re.fullmatch(r"cpu\d+", d):
                    continue
                cpu_n = int(d[3:])
                if (allowed is not None and cpu_n not in allowed) or (pcpus is not None and cpu_n not in pcpus):
                    continue
                base = os.path.join("/sys/devices/system/cpu", d)
                try:
                    core = open(os.path.join(base, "topology", "core_id")).read().strip()
                    pkg = open(os.path.join(base, "topology", "physical_package_id")).read().strip()
                except OSError:
                    continue
                cap = None
                try:
                    cap = int(open(os.path.join(base, "cpu_capacity")).read().strip())
                except (OSError, ValueError):
                    pass
                cores.add((pkg, core))
                caps[(pkg, core)] = max(caps.get((pkg, core)) or 0, cap or 0)
            if cores:
                top = max(caps.values()) if any(caps.values()) else 0
                return sum(1 for c in cores if not top or caps.get(c) == top)
        if sys.platform == "darwin":
            rc, out = _run(["sysctl", "-n", "hw.perflevel0.physicalcpu"], timeout=3)
            if rc == 0 and out.strip().isdigit():
                return int(out.strip())
    except Exception:
        return None
    return None


def wsl_limit_gb(ram_gb=None, path=None):
    """The memory limit of the WSL2 VM (which Docker Desktop also runs in): `memory=` under [wsl2] in %USERPROFILE%\\.wslconfig, else
    WSL's own default of half the RAM. -> GB, or None off Windows."""
    if os.name != "nt" and not path:
        return None
    path = path or os.path.join(os.path.expanduser("~"), ".wslconfig")
    try:
        section = ""
        for ln in open(path, encoding="utf-8", errors="replace"):
            ln = ln.split("#")[0].strip()
            if ln.startswith("[") and ln.endswith("]"):
                section = ln[1:-1].strip().lower()
            elif section == "wsl2" and ln.lower().startswith("memory"):
                m = re.match(r"memory\s*=\s*([\d.]+)\s*([KMGT]?)B?", ln, re.I)
                if m:
                    mult = {"": 1 / 1024 ** 3, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}[m.group(2).upper()]
                    return round(float(m.group(1)) * mult, 1)
    except OSError:
        pass
    return round(ram_gb / 2, 1) if ram_gb else None


def vm_memory(run=_run, ram_gb=None):
    """RAM held right now by the WSL2 / Docker Desktop VM (the vmmem / vmmemWSL process), and how far it may still grow.
    -> {"running": bool, "vm_gb": float|None, "limit_gb": float|None}. Docker and WSL are the person's dev environment: Architect
    plans around them instead of asking anyone to change them."""
    out = {"running": False, "vm_gb": None, "limit_gb": None}
    if os.name != "nt":
        return out
    rc, txt = run(["tasklist", "/FI", "IMAGENAME eq vmmem*", "/FO", "CSV", "/NH"], timeout=5)
    total_kb = 0
    for ln in (txt or "").splitlines():
        cells = [c.strip().strip('"') for c in ln.split('","')]
        if len(cells) >= 5 and cells[0].lower().startswith("vmmem"):
            digits = re.sub(r"[^\d]", "", cells[-1])
            if digits:
                total_kb += int(digits)
    if total_kb:
        out.update(running=True, vm_gb=round(total_kb / 1024 ** 2, 2))
    out["limit_gb"] = wsl_limit_gb(ram_gb or _ram_gb()[0])
    return out


def _power_now():
    """{"on_ac", "battery_pct"} without starting a process (Windows GetSystemPowerStatus; Linux sysfs); None values when unknown."""
    try:
        if os.name == "nt":
            import ctypes

            class SPS(ctypes.Structure):
                _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte), ("saver", ctypes.c_ubyte),
                            ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
            s = SPS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
                return {"on_ac": {0: False, 1: True}.get(s.ac), "battery_pct": None if s.pct == 255 else s.pct}
        base = "/sys/class/power_supply"
        if os.path.isdir(base):
            ac = [d for d in os.listdir(base) if d.upper().startswith(("AC", "ADP"))]
            if ac:
                return {"on_ac": open(os.path.join(base, ac[0], "online")).read().strip() == "1", "battery_pct": None}
    except Exception:
        pass
    return {"on_ac": None, "battery_pct": None}


def record_once(path, run=None, max_bytes=2_000_000, extra=None):
    """Append one sample of what the machine is doing to a rolling log, written through to the disk at once, so even a hard freeze
    leaves the minutes before it behind. Seen live: a freeze under load left nothing but "the previous shutdown was unexpected"; the
    charger had dropped out at about 89 W. The log keeps two files of about 2 MB (live.log and live.log.1). -> the sample written."""
    import json
    row = dict(t=round(time.time(), 1), **live(run or _run), **_power_now(), **(extra or {}))
    try:
        if os.path.isfile(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    return row


def start_recorder(path, period=5.0, extra=None):
    """Sample every `period` seconds on a background thread for as long as this process runs. `extra()` adds fields (the running
    model servers). Returns the thread; it never stops the program it runs in."""
    import threading

    def loop():
        while True:
            try:
                record_once(path, extra=extra() if extra else None)
            except Exception:
                pass
            time.sleep(period)
    t = threading.Thread(target=loop, name="live-recorder", daemon=True)
    t.start()
    return t


def live(run=_run):
    """What the machine is doing right now, cheap enough to poll every few seconds: how busy the graphics card is and how much of its
    memory and of RAM is in use. -> {"gpu_pct", "vram_used_gb", "vram_gb", "ram_gb", "ram_free_gb"}; None where it cannot be read
    (no NVIDIA card, no nvidia-smi). Windows' own Task Manager shows the "3D" engine by default, so model work on CUDA looks idle there."""
    out = {"gpu_pct": None, "vram_used_gb": None, "vram_gb": None, "gpu_temp_c": None, "gpu_power_w": None}
    rc, txt = run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                   "--format=csv,noheader,nounits"], timeout=4)
    if rc == 0 and txt.strip():
        def num(x):                                       # one "[N/A]" column must not blank the others
            try:
                return float(x)
            except ValueError:
                return None
        rows = [[num(x.strip()) for x in ln.split(",")] + [None] * 5 for ln in txt.strip().splitlines() if ln.strip()]
        col = lambda i: [r[i] for r in rows if r[i] is not None]
        if col(0):
            out["gpu_pct"] = round(max(col(0)))
        if col(1) and col(2):
            out["vram_used_gb"], out["vram_gb"] = round(sum(col(1)) / 1024, 2), round(sum(col(2)) / 1024, 2)
        if col(3):
            out["gpu_temp_c"] = round(max(col(3)))
        if col(4):
            out["gpu_power_w"] = round(sum(col(4)), 1)
    ram, free = _ram_gb()
    out.update(ram_gb=round(ram, 1) if ram else None, ram_free_gb=round(free, 1) if free is not None else None)
    vm = vm_memory(run, ram_gb=ram)
    out.update(vm_gb=vm["vm_gb"], vm_limit_gb=vm["limit_gb"])          # the WSL2 / Docker VM, when it runs
    return out


def _cpu_name():
    if sys.platform.startswith("win"):
        try:                                               # the marketing name ("13th Gen Intel Core i5-13420H"), not "Intel64 Family 6 ..."
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
            if name:
                return name
        except OSError:
            pass
    if os.environ.get("PROCESSOR_IDENTIFIER"):
        return os.environ["PROCESSOR_IDENTIFIER"]
    try:
        if os.path.exists("/proc/cpuinfo"):
            for ln in open("/proc/cpuinfo"):
                if ln.lower().startswith("model name"):
                    return ln.split(":", 1)[1].strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        rc, out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if rc == 0 and out.strip():
            return out.strip()
    return platform.processor() or None


def models_dir():
    return os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")


def disk_free_gb(path=None):
    p = path or models_dir()
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(p).free / GB
    except OSError:
        return None


_PS_DRIVES = ("Get-Volume | Where-Object DriveLetter | ForEach-Object { $v=$_; $p=Get-Partition -DriveLetter $v.DriveLetter "
              "-ErrorAction SilentlyContinue | Select-Object -First 1; $d=$null; if ($p) { $d=Get-Disk -Number $p.DiskNumber "
              "-ErrorAction SilentlyContinue }; [pscustomobject]@{letter=[string]$v.DriveLetter; label=[string]$v.FileSystemLabel; "
              "fs=[string]$v.FileSystem; total=[int64]$v.Size; free=[int64]$v.SizeRemaining; bus=[string]$d.BusType; "
              "name=[string]$d.FriendlyName; disk_size=[int64]$d.Size; removable=([string]$v.DriveType -eq 'Removable')} } | "
              "ConvertTo-Json -Compress")


def _suspect(bus, name, disk_size):
    """A removable drive that is probably not what its label says: a capacity that is an exact round number in binary units (firmware
    set, as on fake-capacity sticks: 1,048,576,000,000 bytes = 1,000,000 MiB) or the placeholder product name those sticks carry.
    Real flash has an irregular size set by its chips. -> a plain reason, or ""."""
    if (bus or "").lower() not in ("usb", "sd", "mmc", "unknown", ""):
        return ""
    mib = int(disk_size or 0) / 2 ** 20
    if disk_size and mib == int(mib) and int(mib) % 1000 == 0 and mib >= 64000:
        return (f"it reports exactly {int(mib):,} MiB, a round number set by its firmware; fake-capacity drives look like this. "
                "Keep nothing important on it until its real size is checked")
    if "productcode" in (name or "").replace(" ", "").lower():
        return "its chip reports the placeholder name 'ProductCode', as fake-capacity sticks do"
    return ""


def drives(run=_run):
    """Every mounted drive: where it is, how big and how full, what bus it hangs off (NVMe, SATA, USB), whether it can be unplugged,
    and a warning when it looks like a fake-capacity drive. Reads only; nothing is measured or written here (measure_drive does that).
    -> [{"mount", "label", "fs", "total_gb", "free_gb", "bus", "removable", "name", "suspect"}]"""
    out = []
    if sys.platform.startswith("win"):
        import json
        rc, txt = run(["powershell", "-NoProfile", "-Command", _PS_DRIVES], timeout=30)
        try:
            rows = json.loads(txt) if rc == 0 and txt.strip() else []
        except ValueError:
            rows = []
        for r in rows if isinstance(rows, list) else [rows]:
            bus = (r.get("bus") or "").strip()
            out.append({"mount": f"{r.get('letter')}:\\", "label": r.get("label") or "", "fs": r.get("fs") or "",
                        "total_gb": round((r.get("total") or 0) / 1e9, 1), "free_gb": round((r.get("free") or 0) / 1e9, 1),
                        "bus": bus.lower() or "unknown", "removable": bool(r.get("removable")) or bus.lower() == "usb",
                        "name": r.get("name") or "", "suspect": _suspect(bus, r.get("name"), r.get("disk_size"))})
    else:
        import json
        rc, txt = run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TRAN,RM,MOUNTPOINT,FSTYPE,MODEL"], timeout=10)
        try:
            devs = json.loads(txt).get("blockdevices") or [] if rc == 0 else []
        except ValueError:
            devs = []

        def walk(d, parent):
            for c in d.get("children") or []:
                walk(c, d)
            mp = d.get("mountpoint")
            if not mp:
                return
            try:
                du = shutil.disk_usage(mp)
            except OSError:
                return
            top = parent or d
            tran = (top.get("tran") or ("nvme" if str(top.get("name", "")).startswith("nvme") else "")) or "unknown"
            out.append({"mount": mp, "label": "", "fs": d.get("fstype") or "", "total_gb": round(du.total / 1e9, 1),
                        "free_gb": round(du.free / 1e9, 1), "bus": tran.lower(), "removable": bool(int(top.get("rm") or 0)),
                        "name": (top.get("model") or "").strip(), "suspect": _suspect(tran, top.get("model"), top.get("size"))})
        for d in devs:
            walk(d, None)
    return out


SLOWEST_USEFUL_MB_S = 2.0      # below this a drive cannot even keep up with copying run records in the background


def drive_roles(drs, models=None, speeds=None):
    """What each drive is good for in this app, in plain words. Measured speeds (measure_drive) refine this; the rules: models only on a
    fixed internal drive; a removable or slow drive is an archive at most; a suspect drive holds nothing that matters. `speeds` maps a
    mount to its measure_drive() result: a drive that read back different bytes than were written (`ok` False) is failing, and one that
    writes under SLOWEST_USEFUL_MB_S is too slow to hold anything; both get role "none"."""
    models = os.path.abspath(models or models_dir())
    speeds = speeds or {}
    for d in drs:
        here = models.lower().startswith(str(d["mount"]).lower())
        m = speeds.get(d["mount"]) or {}
        if m.get("ok") is False:
            d["role"], d["why"] = "none", "read back different data than was written in the speed test: the drive is failing or not its stated size"
        elif m.get("write_mb_s") is not None and m["write_mb_s"] < SLOWEST_USEFUL_MB_S:
            d["role"], d["why"] = "none", f"writes at {m['write_mb_s']} MB/s (measured), too slow to hold anything for this app"
        elif d.get("suspect"):
            d["role"], d["why"] = "none", "probably a fake-capacity drive: " + d["suspect"]
        elif d.get("removable"):
            d["role"], d["why"] = "archive", "can be unplugged at any time, so only copies of run records and backups go here, never models"
        else:
            d["role"] = "models" if here else "scratch"
            d["why"] = ("holds your models" if here else "fast internal drive: room for test copies and saved prompt caches")
    return drs


def measure_drive(path, mb=64):
    """Sequential write then read speed of the drive holding `path`, in MB/s, bypassing Windows' file cache so a fresh write is not
    read back from RAM. Writes one temporary file and deletes it. -> {"write_mb_s", "read_mb_s", "ok"} or {"error"}."""
    mb = max(4, min(int(mb), 256))
    test = os.path.join(path, "_architect_speed_test.bin")
    chunk = 4 << 20
    n = max(1, mb * (1 << 20) // chunk)
    try:
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateFileW.restype = wintypes.HANDLE
            k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            k.VirtualAlloc.restype = ctypes.c_void_p
            k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
            k.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
            k.WriteFile.argtypes = k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            bad = wintypes.HANDLE(-1).value
            buf = k.VirtualAlloc(None, chunk, 0x1000, 0x04)
            pat = os.urandom(chunk)
            ctypes.memmove(buf, pat, chunk)
            done = wintypes.DWORD()
            try:
                h = k.CreateFileW(test, 0x40000000, 0, None, 2, 0x20000000 | 0x80000000, None)
                if h == bad:
                    return {"error": "cannot write a test file there"}
                t0 = time.perf_counter()
                for _ in range(n):
                    k.WriteFile(h, buf, chunk, ctypes.byref(done), None)
                k.CloseHandle(h)
                w = n * 4 / (time.perf_counter() - t0)
                h = k.CreateFileW(test, 0x80000000, 0, None, 3, 0x20000000, None)
                same = True
                t0 = time.perf_counter()
                for _ in range(n):
                    k.ReadFile(h, buf, chunk, ctypes.byref(done), None)
                    same = same and ctypes.string_at(buf, chunk) == pat
                k.CloseHandle(h)
                r = n * 4 / (time.perf_counter() - t0)
            finally:
                k.VirtualFree(buf, 0, 0x8000)
        else:
            pat = os.urandom(chunk)
            t0 = time.perf_counter()
            with open(test, "wb") as f:
                for _ in range(n):
                    f.write(pat)
                f.flush()
                os.fsync(f.fileno())
            w = n * 4 / (time.perf_counter() - t0)
            same = True
            t0 = time.perf_counter()
            with open(test, "rb") as f:
                for _ in range(n):
                    same = same and f.read(chunk) == pat
            r = n * 4 / (time.perf_counter() - t0)              # may come from the page cache on Linux/macOS: said by the caller
        return {"write_mb_s": round(w, 1), "read_mb_s": round(r, 1), "ok": bool(same), "mb": n * 4}
    except OSError as e:
        return {"error": str(e)[:120]}
    finally:
        try:
            os.remove(test)
        except OSError:
            pass


def bandwidth_from(full_tok_s, full_gb, split_tok_s=None, split_gpu_gb=None, split_ram_gb=None):
    """Effective memory bandwidth from real runs. Writing one token reads every weight once, so a model fully on the card that writes
    `full_tok_s` tokens a second while holding `full_gb` gives the card's bandwidth; a model split between card and RAM gives RAM's:
    1/tok_s = gpu_gb/gpu_bw + ram_gb/ram_bw. -> {"gpu_bw", "ram_bw"} in GB/s (None where it cannot be worked out)."""
    gpu = round(full_tok_s * full_gb, 1) if full_tok_s and full_gb else None
    ram = None
    if gpu and split_tok_s and split_ram_gb:
        left = 1.0 / split_tok_s - (split_gpu_gb or 0) / gpu
        if left > 0:
            ram = round(split_ram_gb / left, 1)
    return {"gpu_bw": gpu, "ram_bw": ram}


def _bw_path():
    import arch_core as core
    return os.path.join(core.data_dir(), "bandwidth.json")


def save_bandwidth(bw, source):
    import json
    try:
        with open(_bw_path(), "w", encoding="utf-8") as f:
            json.dump(dict(bw, source=source, ts=time.time()), f, indent=1)
    except OSError:
        pass


def load_bandwidth():
    """-> {"gpu_bw", "ram_bw", "source"} measured on this machine, or {} (then the estimates 192 / 50 GB/s are used, and said so)."""
    import json
    try:
        with open(_bw_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def fingerprint(hw):
    key = "|".join(str(x) for x in (hw.get("gpu"), round(hw.get("vram_gb") or 0), round(hw.get("ram_gb") or 0), hw.get("cpu_cores"), hw.get("os")))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def detect(run=_run):
    """-> dict compatible with model_hub.detect_hardware() (gpu, vram_gb, vram_free_gb, ram_gb, ram_free_gb, cpu, os, notes) plus
    gpus (all cards), vendor, unified (Apple/shared memory), cpu_cores, disk_free_gb, models_dir and a `fingerprint`."""
    hw = {"gpu": None, "vram_gb": 0.0, "vram_free_gb": 0.0, "ram_gb": 0.0, "ram_free_gb": 0.0, "cpu": None, "os": sys.platform,
          "notes": [], "gpus": [], "vendor": None, "unified": False}
    ram, ram_free = _ram_gb()
    hw["ram_gb"], hw["ram_free_gb"] = ram, (ram_free if ram_free is not None else ram * 0.6)
    gpus = _nvidia(run) or _amd(run) or (_win_gpu(run) if sys.platform.startswith("win") and run is _run else None)
    if gpus:
        hw["gpus"] = gpus
        hw["gpu"] = gpus[0]["name"] + (f" (+{len(gpus) - 1} more)" if len(gpus) > 1 else "")
        hw["vendor"] = gpus[0]["vendor"]
        hw["vram_gb"] = sum(g["vram_gb"] for g in gpus)       # layers can be split across cards; the planner treats it as one pool
        hw["vram_free_gb"] = sum(g["free_gb"] for g in gpus)
        if len(gpus) > 1:
            hw["notes"].append(f"{len(gpus)} GPUs: VRAM is summed, which assumes the backend splits layers across them.")
    elif sys.platform == "darwin" and re.search(r"apple", platform.processor() or "", re.I) or (sys.platform == "darwin" and platform.machine() == "arm64"):
        hw["vendor"], hw["unified"], hw["gpu"] = "apple", True, "Apple GPU (unified memory)"
        hw["vram_gb"] = ram * 0.70                            # macOS lets the GPU wire roughly 2/3-3/4 of unified memory by default
        hw["vram_free_gb"] = hw["vram_gb"]
        hw["notes"].append("Apple Silicon: GPU memory is shared with the system; ~70% of RAM is treated as usable by the GPU.")
    else:
        hw["notes"].append("No NVIDIA/AMD/Apple GPU found: planning for CPU-only unless you enter VRAM by hand.")
    hw["cpu"] = _cpu_name()
    hw["cpu_cores"] = os.cpu_count()
    hw["cpu_perf_cores"] = perf_cores()
    hw["vm"] = vm_memory(ram_gb=hw.get("ram_gb"))
    hw["models_dir"] = models_dir()
    hw["disk_free_gb"] = disk_free_gb()
    try:
        import arch_power
        hw["power"] = arch_power.battery()
    except Exception:
        hw["power"] = {"present": False}
    hw["fingerprint"] = fingerprint(hw)
    return hw
