"""Limits for the code a run executes (tests, the app being built): a memory cap, and the whole process tree stopped when it ends.

Windows: a Job Object with a per-process memory limit and kill-on-close (closing the job stops every process in it, including servers
a test started and forgot). POSIX: an address-space limit set in the child before it starts. Network blocking is the separate,
existing guard (arch_core.guard_env). Settings > Behaviour > Sandbox memory (MB); 0 = no limit.
"""
import ctypes
import os
import sys

_JOB_LIMIT_PROCESS_MEMORY = 0x100
_JOB_LIMIT_KILL_ON_CLOSE = 0x2000
_EXTENDED_LIMIT_INFO = 9


class _IO(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]


class _BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]


class _EXT(ctypes.Structure):
    _fields_ = [("Basic", _BASIC), ("Io", _IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def attach(proc, mem_mb):
    """Put a started process into a job with a memory cap (Windows). -> the job handle to close() after it ends, or None."""
    if os.name != "nt" or not mem_mb:
        return None
    try:
        k = ctypes.windll.kernel32
        k.CreateJobObjectW.restype = ctypes.c_void_p
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXT()
        info.Basic.LimitFlags = _JOB_LIMIT_PROCESS_MEMORY | _JOB_LIMIT_KILL_ON_CLOSE
        info.ProcessMemoryLimit = int(mem_mb) * 1024 * 1024
        k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if not k.SetInformationJobObject(job, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)) or \
                not k.AssignProcessToJobObject(job, int(proc._handle)):
            k.CloseHandle(ctypes.c_void_p(job))
            return None
        return job
    except (OSError, AttributeError):
        return None


def close(job):
    """Close the job: anything still running in it is stopped."""
    if job:
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
        except (OSError, AttributeError):
            pass


def posix_preexec(mem_mb):
    """A preexec_fn for POSIX children: cap their address space. None when not applicable."""
    if os.name == "nt" or not mem_mb:
        return None

    def limit():
        try:
            import resource
            b = int(mem_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (b, b))
        except (ImportError, ValueError, OSError):
            pass
    return limit


def explain(rc, text, mem_mb):
    """A plain sentence when a child died of the memory cap, else ""."""
    if not mem_mb or rc in (0, None):
        return ""
    if "MemoryError" in (text or "") or (os.name == "nt" and rc in (3221225495, -1073741801)):   # STATUS_NO_MEMORY / commit limit
        return (f"the program ran out of the {mem_mb} MB it is allowed (Settings > Behaviour > Sandbox memory); a test that needs more "
                "memory than that is usually a bug, such as an endless loop that keeps adding to a list")
    return ""
