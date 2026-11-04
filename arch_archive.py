"""Write-behind archive: a copy of each finished run's records on a second drive (Settings > Storage > Archive folder).

The archive drive can be slow (a memory card at ~9 MB/s writes) and can be unplugged at any time, so it is never on the critical path:
the copy is made by a background thread after the run is called done, a missing drive is skipped and remembered, and the next run
retries what was left. Only copies go there (run.json, trace.jsonl, report.md); models and the only copy of anything never do. Names
are kept FAT32-safe and nothing over 4 GB is copied.
"""
import json
import os
import re
import shutil
import threading
import time

import arch_core as core

FILES = ("run.json", "trace.jsonl", "report.md")
MAX_BYTES = 4 * 1024 ** 3 - 1          # FAT32's file size limit


def _pending_path():
    return os.path.join(core.data_dir(), "archive_pending.json")


def _load_pending():
    try:
        with open(_pending_path(), encoding="utf-8") as f:
            return [p for p in json.load(f) if isinstance(p, dict)]
    except (OSError, ValueError):
        return []


def _save_pending(items):
    try:
        with open(_pending_path(), "w", encoding="utf-8") as f:
            json.dump(items[-500:], f)
    except OSError:
        pass


def _safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name))[:80] or "_"


def copy_run(archive, run_dir, project, run_id):
    """Copy one run's records into <archive>/architect_archive/<project>/<run_id>/. -> True when copied, False when the archive is not
    there (unplugged) or the copy failed."""
    if not archive or not os.path.isdir(archive) or not os.path.isdir(run_dir):
        return False
    dest = os.path.join(archive, "architect_archive", _safe(project), _safe(run_id))
    try:
        os.makedirs(dest, exist_ok=True)
        for fn in FILES:
            src = os.path.join(run_dir, fn)
            if os.path.isfile(src) and os.path.getsize(src) <= MAX_BYTES:
                shutil.copyfile(src, os.path.join(dest, fn))
        return True
    except OSError:
        return False


def archive_run(cfg, run_dir, project, run_id, wait=False):
    """After a run is done: copy it (and anything left pending) to the archive drive in the background. Never raises."""
    archive = ((cfg or {}).get("storage") or {}).get("archive") or ""
    if not archive:
        return None

    def work():
        todo = _load_pending() + [{"run_dir": run_dir, "project": project, "run_id": run_id, "t": time.time()}]
        left = [p for p in todo if not copy_run(archive, p.get("run_dir"), p.get("project"), p.get("run_id"))
                and os.path.isdir(p.get("run_dir") or "")]
        _save_pending(left)

    th = threading.Thread(target=work, daemon=False)      # not a daemon: the process waits the second it takes before exiting
    th.start()
    if wait:
        th.join(60)
    return th
