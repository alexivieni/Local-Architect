"""Work that can wait, sent to Claude as one overnight batch at half the price.

submit(items) stores the batch id with the items in data_dir()/batches.json; poll(id) says where it stands and, once it ended,
returns every reply keyed by the item's id (results arrive in any order). Same gate as every Claude call: arch_cloud.allowed().
The SDK is imported only here, only when a real call is made; tests pass `client=`.
"""
import json
import os
import re
import sys
import time

DEFAULT_MODEL = "claude-sonnet-5-5"
HALF = 0.5                                   # the batch discount
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _file():
    import arch_core
    return os.path.join(arch_core.data_dir(), "batches.json")


def _load():
    try:
        with open(_file(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    p = _file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, p)


def _cfg(cfg):
    if cfg is not None:
        return cfg
    try:
        import arch_core
        return arch_core.load_json(arch_core.CONFIG_FILE)
    except Exception:
        return {}


def _client(client, cfg):
    import arch_cloud
    ok, why = arch_cloud.allowed(_cfg(cfg))
    if not ok:
        raise RuntimeError(why)
    if client is not None:
        return client
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("Batches need the Anthropic SDK: run  pip install anthropic")
    return anthropic.Anthropic()


def submit(items, client=None, cfg=None):
    """items: [{"id","system","prompt","model"}] -> batch id."""
    items = list(items or [])
    if not items:
        raise ValueError("nothing to submit")
    seen = set()
    for it in items:
        i = str(it.get("id") or "")
        if not ID_RE.match(i) or i in seen:
            raise ValueError(f"item id {i!r} must be unique, 1-64 letters, digits, - or _")
        seen.add(i)
    c = _client(client, cfg)
    import arch_cloud
    reqs = []
    for it in items:
        p = {"model": arch_cloud.api_model(it.get("model") or DEFAULT_MODEL), "max_tokens": int(it.get("max_tokens") or 4096),
             "messages": [{"role": "user", "content": str(it.get("prompt") or "")}]}
        if it.get("system"):
            p["system"] = str(it["system"])
        reqs.append({"custom_id": str(it["id"]), "params": p})
    b = c.messages.batches.create(requests=reqs)
    bid = getattr(b, "id", None) or (b.get("id") if isinstance(b, dict) else None)
    d = _load()
    d[bid] = {"created": time.time(), "status": "in_progress", "items": items}
    _save(d)
    return bid


def _text(message):
    parts = []
    for blk in (getattr(message, "content", None) or []):
        if getattr(blk, "type", None) == "text":
            parts.append(getattr(blk, "text", "") or "")
    return "".join(parts)


def poll(batch_id, client=None, cfg=None):
    """-> {"id", "status": "in_progress"|"ended"|..., "results": {item id: {"ok", "text", "usd", "error"}}}.
    Results are fetched and stored once, the first time the batch is seen ended."""
    d = _load()
    rec = d.get(batch_id)
    if rec and rec.get("results") is not None:
        return {"id": batch_id, "status": "ended", "results": rec["results"], "usd": rec.get("usd", 0.0)}
    c = _client(client, cfg)
    b = c.messages.batches.retrieve(batch_id)
    st = getattr(b, "processing_status", None) or "in_progress"
    if st != "ended":
        if rec:
            rec["status"] = st
            _save(d)
        return {"id": batch_id, "status": st, "results": {}}
    import arch_cloud
    model_of = {str(i["id"]): i.get("model") or DEFAULT_MODEL for i in (rec or {}).get("items", [])}
    res, total = {}, 0.0
    for r in c.messages.batches.results(batch_id):
        cid = getattr(r, "custom_id", None)
        out = getattr(r, "result", None)
        if getattr(out, "type", None) == "succeeded":
            msg = out.message
            u = getattr(msg, "usage", None)
            usd = HALF * arch_cloud.cost(model_of.get(cid, DEFAULT_MODEL), getattr(u, "input_tokens", 0) or 0,
                                         getattr(u, "output_tokens", 0) or 0)
            total += usd
            res[cid] = {"ok": True, "text": _text(msg), "usd": round(usd, 6), "error": ""}
        else:
            res[cid] = {"ok": False, "text": "", "usd": 0.0, "error": str(getattr(out, "type", "unknown"))}
    d[batch_id] = dict(rec or {"created": time.time(), "items": []}, status="ended", results=res, usd=round(total, 6))
    _save(d)
    return {"id": batch_id, "status": "ended", "results": res, "usd": round(total, 6)}


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(a) == 2 and a[0] == "submit":
            with open(a[1], encoding="utf-8") as f:
                items = [json.loads(l) for l in f if l.strip()]
            print(submit(items))
            return 0
        if len(a) == 2 and a[0] == "poll":
            print(json.dumps(poll(a[1]), indent=2))
            return 0
    except (RuntimeError, ValueError, OSError) as e:
        print("batch:", e)
        return 1
    print("usage: python arch_batch.py submit file.jsonl | poll ID")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
