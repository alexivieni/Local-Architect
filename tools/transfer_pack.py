"""Transfer pack: what the Claude lead taught this project, as local files another person or machine can apply. No network.

    py -3.12 tools/transfer_pack.py [--out DIR] [--selftest]

Writes ONE folder (default hub_data/transfer/<stamp>/):
  playbooks/, workflows/, agents/   arch_playbook.mine() output (re-run now) plus one local agent per Claude brief
  orchestration/summary.json        every workflow/agent the Claude lead ran: prompts (paths scrubbed), phases, tools, result summary, tokens, cache
  harness/HARNESS.md                the harness contract (docs/HARNESS.md)
  harness/settings.json             cache settings in effect, model choices (names only), playbook rules, and why
  TRANSFER.md                       how to apply it (arch_port --pack/--adopt for settings)
Everything Claude-written is tagged `source: claude-orchestration`, uses local tiers only, and is retrieval/playbook material, never training data.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import arch_playbook as pb  # noqa: E402

WHY = {"cloud_cache_ttl": "prompt-cache lifetime for the optional Claude step: 5m is cheapest for one-off calls, 1h pays when the same prefix is reused",
       "cloud_cache": "an identical request is answered from disk for $0", "keep_alive": "how long a model stays loaded; 0 keeps a small GPU free",
       "ctx": "context window; bounded by VRAM and KV cache type", "kv": "KV cache type; q8_0 halves KV memory for little quality loss"}
RULES = ["Audit before build when the change touches more than one file.", "Every build agent owns named files; two agents never own the same one.",
         "Verify with a panel (n>=2, majority) or recompute from raw data; a check that cannot run is not_sure, never pass.",
         "Briefs name: goal, owned files, rules, output schema, report format.", "Claude-written text is playbook material, never training data."]


def _scrub(t):
    return re.sub(r"[A-Za-z]:[\\/][^\s\"']+", "<path>", str(t or ""))


def _h(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:8]


def _agent_md(a, kind):
    tools = {"audit": "ls, grep, search, read, outline, view", "plan": "ls, grep, read, view", "verify": "ls, grep, read, view, run_tests",
             "build": "ls, grep, read, view, outline, edit_lines, apply"}.get(kind, "ls, grep, read, view")
    body = _scrub(a.get("prompt"))[:4000].replace("---", "- - -")
    t = ("---\nname: claude-brief-{h}\ndescription: \"Brief the Claude lead gave (" + kind + "). Local tiers only.\"\nmodel: worker\n"
         f"tools: {tools}\neffort: medium\nrole: {'generator' if kind == 'build' else 'reviewer'}\ncategory: harness\n"
         f"writes: {'true' if kind == 'build' else 'false'}\nsource: {pb.SOURCE}\ntraining: never\n"
         "task: |\n  Goal: {goal}\n  {prev}\n  {task}\n---\n<!-- source: " + pb.SOURCE + " -->\n" + body + "\n")
    h = _h(t)
    return h, t.replace("{h}", h)


def _tok(n):
    t = n.get("tokens") or {}
    return {k: int(t.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write")}


def _flatten(d, p=""):
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            yield from _flatten(v, p + k + ".")
        else:
            yield p + k, v


def settings_report(cfg):
    keys = {k: v for k, v in _flatten(cfg) if re.search(r"cache|ttl|keep_alive|kv", k, re.I)}
    why = {k: next((w for s, w in WHY.items() if s in k), "") for k in keys}
    models = {"tiers": {k: (v.get("model") if isinstance(v, dict) else v) for k, v in (cfg.get("tiers") or {}).items()},
              "roles": {k: v for k, v in (cfg.get("roles") or {}).items() if isinstance(v, str)}}
    return {"cache_settings": keys, "why": why, "model_choices": models, "playbook_rules": RULES,
            "note": "machine-specific; apply with arch_port --adopt, which keeps the new machine's own model choices"}


def build(out=None, snap=None, cfg=None):
    out = out or os.path.join(HERE, "hub_data", "transfer", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    if snap is None:
        import arch_orchestration
        snap = arch_orchestration.snapshot()
    mined = pb.mine(snap, root=out)                                   # (a) playbooks + learned workflows + brief agents
    wfs, summary, agents_n = pb.workflows_of(snap), [], 0
    for wf in wfs:                                                    # (b) every brief the lead used
        sig, _steps = pb.shape_of(wf)
        ags = []
        pb._walk(wf, "agent", ags)
        rows = []
        for a in ags:
            kind = pb.kind_of(a.get("label"))
            rows.append({"label": a.get("label"), "kind": kind, "model": a.get("model"), "tools": a.get("tool_calls") or {},
                         "tokens": _tok(a), "cache_hit_share": a.get("cache_hit_share"), "verdict": a.get("verdict"),
                         "prompt": _scrub(a.get("prompt"))[:4000], "result_summary": _scrub(a.get("result"))[:400]})
            if a.get("prompt"):
                h, t = _agent_md(a, kind)
                p = os.path.join(out, "agents", f"claude-brief-{h}.md")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(t)
                agents_n += 1
        summary.append({"workflow": wf.get("label"), "shape": sig, "phases": [ph.get("label") for ph in wf.get("children") or []],
                        "agents": rows, "total": wf.get("total")})
    os.makedirs(os.path.join(out, "orchestration"), exist_ok=True)
    with open(os.path.join(out, "orchestration", "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"source": pb.SOURCE, "training": "never", "workflows": summary}, f, indent=1)
    os.makedirs(os.path.join(out, "harness"), exist_ok=True)         # (c)
    src = os.path.join(HERE, "docs", "HARNESS.md")
    if os.path.isfile(src):
        shutil.copyfile(src, os.path.join(out, "harness", "HARNESS.md"))
    if cfg is None:
        try:
            with open(os.path.join(os.environ.get("ARCHITECT_HOME") or HERE, "architect_config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    with open(os.path.join(out, "harness", "settings.json"), "w", encoding="utf-8") as f:
        json.dump(settings_report(cfg), f, indent=1)
    guide = os.path.join(HERE, "docs", "TRANSFER.md")                 # (d)
    if os.path.isfile(guide):
        shutil.copyfile(guide, os.path.join(out, "TRANSFER.md"))
    return {"out": out, "shapes": mined["shapes"], "mined_files": len(mined["written"]) + len(mined["same"]), "briefs_converted": agents_n,
            "workflows_summarised": len(summary)}


def selftest():
    import arch_selftest_playbook as st
    ok, tmp = [], tempfile.mkdtemp(prefix="tpack_")
    try:
        r = build(os.path.join(tmp, "p"), snap=st.fixture(), cfg={"online": {"cloud_cache_ttl": "5m"}, "tiers": {"worker": {"model": "qwen3:8b"}}})
        o = r["out"]
        ok.append(("folder written", os.path.isfile(os.path.join(o, "orchestration", "summary.json"))))
        ok.append(("playbook + workflow + agent files", all(os.listdir(os.path.join(o, d)) for d in ("playbooks", "workflows", "agents"))))
        ok.append(("briefs converted", r["briefs_converted"] >= 1))
        txt = ""
        for b, _d, fs in os.walk(o):
            for fn in fs:
                if fn.endswith((".md", ".json", ".yaml")) and fn not in ("HARNESS.md", "TRANSFER.md"):
                    with open(os.path.join(b, fn), encoding="utf-8") as f:
                        txt += f.read()
        ok.append(("tagged, local-only, no machine paths", "claude-orchestration" in txt and "bob" not in txt and "model: sonnet" not in txt))
        with open(os.path.join(o, "harness", "settings.json"), encoding="utf-8") as f:
            ok.append(("cache settings explained", "cloud_cache_ttl" in f.read()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for w, c in ok:
        print(("[ok] " if c else "[x] ") + w)
    print(f"{sum(c for _, c in ok)}/{len(ok)} checks")
    return 0 if all(c for _, c in ok) else 1


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in a:
        return selftest()
    print(json.dumps(build(a[a.index("--out") + 1] if "--out" in a else None), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
