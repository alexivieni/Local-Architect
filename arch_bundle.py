"""Crash / run bundle: one zip with everything needed to understand a run, safe to send to someone.

bundle(run_dir) -> <data>/bundles/<run id>.zip holding the run's trace, report, postmortem, plan/state, the telemetry events of that run,
the server state, the tail of the server logs, versions and the effective settings. Every text is passed through arch_redact.scrub
(known secret shapes) and, for JSON, arch_telemetry's key-based scrub; user paths (C:\\Users\\name, /home/name) become ~ and the
user name is replaced everywhere. Never raises from `auto()`; `bundle()` raises only for a run folder that does not exist.
`auto(run_dir, verdict)` makes one when a run ends in fail/not_sure (hooked after the verdict in arch_flows.run_flow).
CLI: python arch_bundle.py <run_dir> [--out DIR]
"""
import getpass
import json
import os
import platform
import re
import sys
import time
import zipfile

LOG_TAIL_BYTES = 64 * 1024
MAX_TEXT = 4 * 1024 * 1024             # a trace bigger than this keeps its head and tail
RUN_FILES = ("trace.jsonl", "report.md", "postmortem.md", "run.json", "plan.json", "plan_override.json", "created.txt")
_USERS = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)([^\\/\"'\s]+)|(/(?:home|Users)/)([^/\"'\s]+)")


def _data_dir():
    try:
        import arch_core
        return arch_core.data_dir()
    except Exception:
        return os.environ.get("ARCHITECT_DATA_DIR") or "hub_data"


def _names():
    names = set()
    for n in (os.environ.get("USERNAME"), os.environ.get("USER")):
        if n:
            names.add(n)
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    return {n for n in names if len(n) >= 3}


def anonymise(text, names=None):
    """C:\\Users\\alexi\\x -> C:\\Users\\~\\x, /home/bob -> /home/~, and the bare user name -> ~user. Home folder written as ~ stays."""
    def sub(m):
        return (m.group(1) + "~") if m.group(1) else (m.group(3) + "~")
    text = _USERS.sub(sub, text)
    for n in (names if names is not None else _names()):
        text = re.sub(r"(?i)(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", "~user", text)
    return text


def redact_text(text):
    try:
        import arch_redact
        text = arch_redact.scrub(text)
    except Exception:
        pass
    return anonymise(text)


def redact_json(obj):
    """Key-based scrub (api_key, token, ...) then the text pass over the serialised result."""
    try:
        import arch_telemetry
        obj = arch_telemetry._scrub(obj)
    except Exception:
        pass
    return redact_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def _read(path, tail=None):
    try:
        with open(path, "rb") as f:
            if tail:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - tail))
            data = f.read()
    except OSError:
        return None
    txt = data.decode("utf-8", errors="replace")
    if not tail and len(txt) > MAX_TEXT:
        h = MAX_TEXT // 2
        txt = txt[:h] + "\n...[trimmed %d bytes]...\n" % (len(txt) - MAX_TEXT) + txt[-h:]
    return txt


def _redact_jsonl(txt):
    """Per line, so one odd line never spoils the file."""
    out = []
    for line in txt.splitlines():
        try:
            out.append(redact_json_compact(json.loads(line)))
        except ValueError:
            out.append(redact_text(line))
    return "\n".join(out) + "\n"


def redact_json_compact(obj):
    try:
        import arch_telemetry
        obj = arch_telemetry._scrub(obj)
    except Exception:
        pass
    return redact_text(json.dumps(obj, ensure_ascii=False, default=str))


def versions():
    v = {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(), "bundle_made": time.time()}
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "MANIFEST.sha256"), "rb") as f:
            import hashlib
            v["manifest_sha256"] = hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        pass
    try:
        import arch_server
        v["server_binary"] = os.path.basename(str(arch_server.load_state().get("binary") or ""))
    except Exception:
        pass
    return v


def _effective(root):
    try:
        import arch_core
        cfg = arch_core.load_config(root)
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}
    return cfg


def _server_state():
    try:
        import arch_server
        return arch_server.load_state()
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}


def _events(run_id):
    try:
        import arch_telemetry
        return arch_telemetry.read(run=run_id, limit=0)
    except Exception:
        return []


def _verdict_of(run_dir):
    v = ""
    try:
        with open(os.path.join(run_dir, "trace.jsonl"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"verdict"' in line:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if j.get("type") == "verdict":
                        v = j.get("verdict") or v
    except OSError:
        pass
    return v


def bundle(run_dir, out_dir=None):
    """Write <out_dir or <data>/bundles>/<run id>.zip. -> its path."""
    run_dir = os.path.abspath(run_dir)
    if not os.path.isdir(run_dir):
        raise FileNotFoundError(run_dir)
    run_id = os.path.basename(run_dir)
    root = os.path.dirname(os.path.dirname(os.path.dirname(run_dir)))          # <root>/.arch/runs/<id>
    out_dir = out_dir or os.path.join(_data_dir(), "bundles")
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, re.sub(r"[^\w.\-]", "_", run_id) + ".zip")
    tmp = dest + ".tmp"
    included = []
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name, text):
            z.writestr(name, text)
            included.append(name)
        for fn in RUN_FILES:
            txt = _read(os.path.join(run_dir, fn))
            if txt is None:
                continue
            if fn.endswith(".jsonl"):
                put("run/" + fn, _redact_jsonl(txt))
            elif fn.endswith(".json"):
                try:
                    put("run/" + fn, redact_json(json.loads(txt)))
                except ValueError:
                    put("run/" + fn, redact_text(txt))
            else:
                put("run/" + fn, redact_text(txt))
        evs = _events(run_id)
        if evs:
            put("telemetry.jsonl", "\n".join(redact_json_compact(e) for e in evs) + "\n")
        put("server_state.json", redact_json(_server_state()))
        logs = os.path.join(_data_dir(), "server")
        try:
            for fn in sorted(os.listdir(logs)):
                if fn.endswith(".log"):
                    txt = _read(os.path.join(logs, fn), tail=LOG_TAIL_BYTES)
                    if txt is not None:
                        put("logs/" + fn + ".tail", redact_text(txt))
        except OSError:
            pass
        put("versions.json", redact_json(versions()))
        put("settings.json", redact_json(_effective(root)))
        put("README.txt", redact_text(
            f"Architect run bundle for {run_id}\nverdict: {_verdict_of(run_dir) or 'unknown'}\nmade: {time.ctime()}\n"
            "Secrets were redacted (arch_redact + key-based) and user paths anonymised, but read it before you share it.\n"
            "Files: " + ", ".join(included)))
    os.replace(tmp, dest)
    return dest


def auto(run_dir, verdict, out_dir=None):
    """The after-verdict hook: a bundle for fail/not_sure, nothing for a pass. Never raises. -> path or None."""
    if verdict not in ("fail", "not_sure", "error"):
        return None
    try:
        return bundle(run_dir, out_dir)
    except Exception as ex:
        try:
            import arch_telemetry
            arch_telemetry.swallow("bundle.auto", ex)
        except Exception:
            pass
        return None


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    out = None
    if "--out" in a:
        i = a.index("--out")
        out = a[i + 1] if i + 1 < len(a) else None
        del a[i:i + 2]
    if not a:
        print("usage: arch_bundle.py <run_dir> [--out DIR]")
        return 2
    try:
        print(bundle(a[0], out))
    except FileNotFoundError as ex:
        print(f"no such run folder: {ex}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
