"""IOC matching: an indicator list YOU supply against evidence YOU supply (log lines, file hashes), decided by CODE. A match is exact (same IP or an IP inside a
listed CIDR, the same domain or a subdomain of it, the same file hash, the same URL text) or it is not a match; no model decides it and nothing is looked up
online. The `ioc` flow then has the analyst explain each match that code found, citing it by id; a cited id that does not exist, or an indicator the evidence
never held, is refused (the `ioc_cited` gate), so the write-up can only say things the files support.

Inputs (the first found; or named in the goal):
  indicators   iocs.txt / indicators.txt (one per line, `#` comments) or iocs.json / indicators.json (a list of strings, or {"indicators": [...]}).
               Defanged forms are read as written normally (hxxp://evil[.]com -> http://evil.com).
  evidence     files named in the goal, else every file under evidence/, else the *.log *.txt *.csv *.json *.jsonl *.md *.conf files of the project
               (never the indicator list or docs/ioc_report.md). Text files are read line by line; every evidence file is also hashed (md5, sha1, sha256).
Nothing here opens a connection."""
import hashlib
import ipaddress
import json
import os
import re

import arch_core as core
from arch_gates import GATES, R

IOC_NAMES = ("iocs.txt", "indicators.txt", "iocs.json", "indicators.json")
EVIDENCE_EXT = (".log", ".txt", ".csv", ".json", ".jsonl", ".md", ".conf")
MAX_FILES, MAX_BYTES, MAX_LINES, MAX_HITS = 30, 2_000_000, 50000, 200
_IP = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_HASH = re.compile(r"^[0-9a-f]{32}$|^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")


def refang(s):
    """hxxp://evil[.]com -> http://evil.com (analysts defang indicators so they are not clicked)."""
    s = (s or "").strip().strip("`'\"")
    s = re.sub(r"^hxxp", "http", s, flags=re.I)
    return s.replace("[.]", ".").replace("(.)", ".").replace("[:]", ":").replace("[://]", "://")


def classify(value):
    """-> {"value", "kind", "norm"} or None. kind: ip | cidr | hash | url | domain."""
    v = refang(value)
    if not v or v.startswith("#"):
        return None
    low = v.lower()
    if _HASH.match(low):
        return {"value": value.strip(), "kind": "hash", "norm": low}
    if re.match(r"^https?://\S+$", low):
        return {"value": value.strip(), "kind": "url", "norm": low}
    try:
        if "/" in v:
            return {"value": value.strip(), "kind": "cidr", "norm": str(ipaddress.ip_network(v, strict=False))}
        return {"value": value.strip(), "kind": "ip", "norm": str(ipaddress.ip_address(v))}
    except ValueError:
        pass
    if _DOMAIN.match(low):
        return {"value": value.strip(), "kind": "domain", "norm": low}
    return None


def load_iocs(root, names=None):
    """-> (indicators, source file, error). The first indicator file that exists (named, or one of IOC_NAMES)."""
    for rel in list(names or []) + list(IOC_NAMES):
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        text = core.read_text(full, MAX_BYTES)[0]
        try:
            raw = json.loads(text) if rel.endswith(".json") else text.splitlines()
        except ValueError as ex:
            return [], rel, f"{rel} is not valid JSON: {ex}"
        if isinstance(raw, dict):
            raw = raw.get("indicators") or raw.get("iocs") or []
        if not isinstance(raw, list):
            return [], rel, f"{rel} must be a list of indicators (or {{\"indicators\": [...]}})"
        seen, out = set(), []
        for item in raw:
            c = classify(str(item)) if not str(item).strip().startswith("#") else None
            if c and c["norm"] not in seen:
                seen.add(c["norm"])
                out.append(c)
        if not out:
            return [], rel, f"{rel} holds no indicator this check understands (IP, CIDR, domain, URL, or an md5/sha1/sha256 hash)"
        return out, rel, None
    return [], None, "no indicator list: write iocs.txt (one indicator per line) or iocs.json, then run again"


def evidence_files(root, goal=""):
    """Evidence paths relative to root: named in the goal, else evidence/, else the usual log-like files of the project."""
    named = [m.replace("\\", "/") for m in re.findall(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}\b", goal or "") if os.path.isfile(os.path.join(root, m))]
    named = [n for n in dict.fromkeys(named) if os.path.basename(n) not in IOC_NAMES]
    if named:
        return named[:MAX_FILES]
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in (".git", ".arch", "node_modules", "__pycache__", "docs"))
        for f in sorted(fn):
            rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            if f in IOC_NAMES or rel == "docs/ioc_report.md":
                continue
            if rel.startswith("evidence/") or f.lower().endswith(EVIDENCE_EXT):
                out.append(rel)
    ev = [f for f in out if f.startswith("evidence/")]
    return (ev or out)[:MAX_FILES]


def _file_hashes(full):
    h = {"md5": hashlib.md5(), "sha1": hashlib.sha1(), "sha256": hashlib.sha256()}
    with open(full, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            for x in h.values():
                x.update(chunk)
    return {k: v.hexdigest() for k, v in h.items()}


def _line_hit(ioc, line, low):
    k, n = ioc["kind"], ioc["norm"]
    if k == "ip":
        return n in _IP.findall(line)
    if k == "cidr":
        net = ipaddress.ip_network(n, strict=False)
        for cand in _IP.findall(line):
            try:
                if ipaddress.ip_address(cand) in net:
                    return True
            except ValueError:
                continue
        return False
    if k == "domain":
        # the indicator or a subdomain of it; not "notevil.x" (a longer label before) and not "evil.x.example.com" (another domain after)
        return re.search(r"(?<![a-z0-9-])(?:[a-z0-9-]+\.)*" + re.escape(n) + r"(?![a-z0-9-])(?!\.[a-z0-9])", low) is not None
    if k == "hash":
        return re.search(r"(?<![0-9a-f])" + n + r"(?![0-9a-f])", low) is not None
    return n in low


def match(root, iocs, files):
    """-> (hits, notes). A hit: {"id": "H1", "ioc", "kind", "file", "line", "text"}; line 0 = the whole file's hash. notes: what could not be read."""
    hits, notes = [], []
    for rel in files:
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > 50_000_000:
                notes.append(f"{rel}: over 50 MB, not read")
                continue
            hashes = _file_hashes(full)
            for ioc in iocs:
                if ioc["kind"] == "hash" and ioc["norm"] in hashes.values() and len(hits) < MAX_HITS:
                    hits.append({"ioc": ioc["value"], "kind": "hash", "file": rel, "line": 0, "text": f"the file's own {[k for k, v in hashes.items() if v == ioc['norm']][0]} hash"})
        except OSError as ex:
            notes.append(f"{rel}: {ex.strerror or ex}")
            continue
        if os.path.getsize(full) > MAX_BYTES:
            notes.append(f"{rel}: over {MAX_BYTES // 1_000_000} MB, only its hash was checked")
            continue
        text = core.read_text(full, MAX_BYTES)[0]
        for n, line in enumerate(text.splitlines()[:MAX_LINES], 1):
            low = line.lower()
            for ioc in iocs:
                if len(hits) >= MAX_HITS:
                    notes.append(f"stopped at {MAX_HITS} matches")
                    break
                if _line_hit(ioc, line, low):
                    hits.append({"ioc": ioc["value"], "kind": ioc["kind"], "file": rel, "line": n, "text": line.strip()[:160]})
            else:
                continue
            break
    for i, h in enumerate(hits, 1):
        h["id"] = f"H{i}"
    return hits, notes


def hits_text(hits):
    return "\n".join(f"- [{h['id']}] {h['kind']} `{h['ioc']}` in {h['file']}" + (f":{h['line']}" if h["line"] else "") + f": {h['text']}" for h in hits)


# ------------------------------------------------------------------------------------------------------------------ the gates
def g_ioc_match(e, ctx, step, changed):
    """ioc: read the indicator list, find the evidence, match them. Zero matches is an answer (the report says so), not a failure."""
    iocs, src, err = load_iocs(e.root, (ctx.get("params") or {}).get("iocs_file") and [ctx["params"]["iocs_file"]])
    if err:
        return R(False, err)
    files = evidence_files(e.root, ctx.get("goal", ""))
    if not files:
        return R(False, "no evidence to look through: name the files in the request, or put them under evidence/")
    hits, notes = match(e.root, iocs, files)
    ctx.update(iocs=iocs, ioc_file=src, ioc_hits=hits, ioc_files=", ".join(files), hits_text=hits_text(hits) or "(no indicator matched)",
               reply="", ioc_notes="; ".join(dict.fromkeys(notes)))
    e.say(f"[ioc] {len(iocs)} indicator(s) from {src}, {len(files)} evidence file(s), {len(hits)} match(es)" + (f" ({'; '.join(notes[:2])})" if notes else ""))
    e.report(f"- Indicators: {len(iocs)} ({src}); evidence files: {len(files)}; matches found by code: {len(hits)}", *[("  " + x) for x in hits_text(hits).splitlines()[:12]], "")
    return R(True, "", {"indicators": len(iocs), "files": len(files), "hits": len(hits)}, ctx["hits_text"])


def g_ioc_cited(e, ctx, step, changed):
    """ioc: the analyst's notes cite every match by its id, cite no id that does not exist, and name no indicator the matches do not hold."""
    reply = ctx.get("reply") or ""
    hits = ctx.get("ioc_hits") or []
    ids = {h["id"] for h in hits}
    cited = set(re.findall(r"\[(H\d+)\]", reply))
    problems = []
    if cited - ids:
        problems.append("cites " + ", ".join(sorted(cited - ids)) + ", which is not a match code found")
    if ids - cited:
        problems.append("does not mention " + ", ".join(sorted(ids - cited, key=lambda x: int(x[1:]))) + " (every match needs a note)")
    allowed = {h["ioc"].lower() for h in hits} | {refang(h["ioc"]).lower() for h in hits} | {i["norm"] for i in ctx.get("iocs") or []}
    seen_text = " ".join(h["text"].lower() for h in hits)
    for tok in set(_IP.findall(reply)) | set(re.findall(r"\b[0-9a-f]{32,64}\b", reply.lower())) | set(re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ru|cn|info|biz|xyz|top|cc|tk|ws|su)\b", reply.lower())):
        if tok.lower() not in allowed and tok.lower() not in seen_text and not any(tok.lower() in a or a in tok.lower() for a in allowed if len(a) > 3):
            problems.append(f"names {tok}, which is neither in the indicator list nor in a matching line")
    ctx["ioc_cited"] = sorted(cited)
    return R(not problems, "; ".join(problems[:5]), {"cited": len(cited & ids), "problems": len(problems)})


GATES["ioc_match"] = g_ioc_match
GATES["ioc_cited"] = g_ioc_cited
