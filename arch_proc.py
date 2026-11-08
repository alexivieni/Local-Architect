"""Process identity, one place. A pid alone is not an identity: Windows recycles pids quickly and a state file outlives a reboot, so a
saved pid can name a different program the next day. Here a process is (pid, creation time); `alive()` never signals the process it
looks at (on Windows `os.kill(pid, 0)` is TerminateProcess), and `same()` says whether a recorded identity is still that process.
Stdlib only: ctypes on Windows, /proc on Linux, `ps` elsewhere."""
import os
import subprocess
import sys

_WIN = os.name == "nt"
_STILL_ACTIVE = 259
_QUERY = 0x1000            # PROCESS_QUERY_LIMITED_INFORMATION


def _k32():
    import ctypes
    return ctypes.windll.kernel32


def alive(pid):
    """True / False, or None when it cannot be told (access refused, an unusual platform). Never stops anything."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _WIN:
        try:
            import ctypes
            k = _k32()
            h = k.OpenProcess(_QUERY, False, pid)
            if not h:
                err = k.GetLastError()
                return False if err == 87 else (True if err == 5 else None)     # 87 no such process; 5 exists but is not ours to open
            try:
                code = ctypes.c_ulong()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return None
                return code.value == _STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        except Exception:
            return None
    try:
        os.kill(pid, 0)           # POSIX: signal 0 only checks
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True               # it exists; it is someone else's
    except OSError:
        return None


def start_time(pid):
    """When the process started, as a float (seconds; only comparable with another start_time() of the same machine), or None."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if _WIN:
        try:
            import ctypes
            k = _k32()
            h = k.OpenProcess(_QUERY, False, pid)
            if not h:
                return None
            try:
                ft = [ctypes.c_ulonglong() for _ in range(4)]
                if not k.GetProcessTimes(h, *[ctypes.byref(x) for x in ft]):
                    return None
                return ft[0].value / 1e7          # 100 ns ticks since 1601
            finally:
                k.CloseHandle(h)
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            rest = f.read().rsplit(b")", 1)[1].split()
        return float(rest[19])                     # field 22 (starttime, clock ticks since boot); fields after the ")" start at 3
    except (OSError, IndexError, ValueError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        return float(abs(hash(out))) if out else None
    except Exception:
        return None


def identity(pid):
    """{"pid", "start"} to keep beside a pid in any state file."""
    return {"pid": int(pid or 0), "start": start_time(pid)}


def same(pid, start, tolerance=2.0):
    """Is `pid` still the process that had creation time `start`? True / False / None (cannot be told). A record without a start
    (written by an older version) falls back to plain liveness."""
    a = alive(pid)
    if not a:
        return a
    if start is None:
        return a
    now = start_time(pid)
    if now is None:
        return None
    return abs(float(now) - float(start)) <= tolerance
