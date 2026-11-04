"""Optional, opt-in model facts from Hugging Face. Read-only: one metadata request per `hf.co/...` model you already have, never a download.

Why it exists: for a model pulled from Hugging Face, the repo's own page says whether it is gated, how recently it was updated and what licence it carries.
That is "vendor card" evidence, below anything measured on this machine. Nothing here changes a setting. Off unless you pass `optimize --online`.
"""
import json
import re
import urllib.error
import urllib.request

API = "https://huggingface.co/api/models/"
_HF = re.compile(r"^(?:hf\.co|huggingface\.co)/([^/:\s]+/[^/:\s]+)(?::([^/\s]+))?$", re.I)


def hf_repo(name):
    """'hf.co/user/repo:Q4_K_M' -> ('user/repo', 'Q4_K_M'); anything else -> None."""
    m = _HF.match((name or "").strip())
    return (m.group(1), m.group(2)) if m else None


def _fetch(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "architect-local/1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000).decode("utf-8", "replace"))


def facts(name, fetch=None):
    """-> {"repo","quant","last_modified","downloads","likes","gated","license","pipeline"} or {"error": ...}. Never raises."""
    hit = hf_repo(name)
    if not hit:
        return {"error": "not a Hugging Face model name"}
    repo, quant = hit
    try:
        d = (fetch or _fetch)(API + repo)
    except urllib.error.HTTPError as e:
        return {"repo": repo, "error": "not found on Hugging Face (it may be private or renamed)" if e.code == 404 else f"Hugging Face answered {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"repo": repo, "error": f"could not reach Hugging Face: {str(e)[:80]}"}
    if not isinstance(d, dict):
        return {"repo": repo, "error": "unexpected answer"}
    tags = d.get("tags") or []
    lic = next((t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith("license:")), None)
    return {"repo": repo, "quant": quant, "last_modified": (d.get("lastModified") or d.get("last_modified") or "")[:10] or None,
            "downloads": d.get("downloads"), "likes": d.get("likes"), "gated": bool(d.get("gated")), "license": lic, "pipeline": d.get("pipeline_tag")}


def notes_for(f):
    """One facts dict -> short plain notes, each with its evidence tier."""
    if not f or f.get("error"):
        return [("unavailable", (f or {}).get("error", "no data"))]
    out = []
    bits = [f"repo {f['repo']}"]
    if f.get("last_modified"):
        bits.append(f"last updated {f['last_modified']}")
    if f.get("downloads") is not None:
        bits.append(f"{f['downloads']:,} downloads")
    if f.get("license"):
        bits.append(f"licence {f['license']}")
    out.append(("vendor card", ", ".join(bits) + ". Popularity is not quality: your Scoreboard is the real test."))
    if f.get("gated"):
        out.append(("vendor card", "The repo is gated: updating it needs a Hugging Face login."))
    return out


def lookup(names, fetch=None):
    """-> {model name: facts} for every Hugging Face model in `names` (others are skipped)."""
    return {n: facts(n, fetch) for n in names if hf_repo(n)}
