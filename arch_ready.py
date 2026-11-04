"""Get ready: one call that takes a fresh machine to a working, tuned, proven setup.

    python architect.py ready [--yes] [--root PATH]

Steps (each one reports; nothing is hidden and no download happens without a yes):
  1. doctor --fix        safe repairs only (pytest, old run folders)
  2. models              if no chat model is installed, suggests the best one that fits and (with your yes) downloads it
  3. tune                hardware -> context, timeouts, harness tier, team (arch_autotune)
  4. proof               the builder must call a tool correctly, fix small bugs and follow an output format; the result is stored in the scoreboard
  5. upgrades            catalog models that are a clear step up and fit this machine (listed, never downloaded unasked)
The result is written to <data>/ready.json so the hub can show it without re-running anything.
"""
import json
import os
import shutil
import time

import arch_core as core
import arch_autotune
import arch_catalog
import arch_doctor
import arch_hw

PROOF_CATS = ("toolcall", "backend_fix", "format")      # "format" is scored from how the replies to the coding tasks were formatted
PROOF_PASS = 60.0


def ready_path():
    return os.path.join(core.data_dir(), "ready.json")


def load_ready():
    return core.load_json(ready_path())


def _installed(mh, hw):
    rows = mh.model_list(hw, "f16", 192.0, 50.0)
    return [r["name"] for r in rows if not r["embed"]]


def _store_proof(mh, model, out):
    with mh.LOCK:
        res = mh.load_results()
        sc = res.setdefault(model, {}).setdefault("scores", {})
        for cat, r in out.items():
            sc.setdefault(cat, []).append({"score": r["score"], "n": r["n"], "t": time.time(), "detail": r.get("detail"), "think": False})
        mh.save_results(res)


def ready(root=None, yes=False, say=print, probe=True, proof=True, ask=None, hw=None, pull=None, smoke=True, ask_model=None):
    """-> summary dict (also saved). `ask(question) -> bool` is used for downloads when `yes` is False; with neither, downloads are only suggested.
    `pull` is injectable for tests (default arch_catalog.pull)."""
    arch_autotune._wire()
    import model_hub as mh
    pull = pull or arch_catalog.pull
    root = os.path.abspath(root or os.getcwd())
    summary = {"ts": time.time(), "steps": [], "ok": False, "needs": []}

    def step(name, ok, detail="", advisory=False):
        summary["steps"].append({"name": name, "ok": bool(ok), "detail": detail, "advisory": advisory})
        say(f"  {'OK  ' if ok else 'NEXT'}  {name}" + (f": {detail}" if detail else ""))

    say("[1/5] checking the machine and repairing what is safe to repair")
    fx = arch_doctor.fix(root)
    blocking = [c for c in fx["after"]["checks"] if not c["ok"] and not c["advisory"]]
    for a in fx["actions"]:
        say(f"      fixed: {a['what']} ({a['detail']})")
    step("environment", not blocking, "; ".join(f"{c['name']}: {c['detail']}" for c in blocking))
    if any(c["name"].startswith("Ollama") or "ollama" in c["name"].lower() for c in blocking):
        summary["needs"].append("Start Ollama (https://ollama.com), then run this again.")
        return _finish(summary, say)

    hw = hw or arch_hw.detect()
    say(f"[2/5] models  ({hw.get('gpu') or 'no GPU'}, {hw['vram_gb']:.0f} GB VRAM, {hw['ram_gb']:.0f} GB RAM, {(hw.get('disk_free_gb') or 0):.0f} GB free)")
    try:
        have = _installed(mh, hw)
    except mh.OllamaError as e:
        step("models", False, e.msg)
        summary["needs"].append("Ollama did not answer: " + e.msg)
        return _finish(summary, say)
    if not have:
        sug = arch_catalog.recommend(hw, [], max_per_slot=1)["suggestions"]
        first = next((s for s in sug if s["slot"] == "builder"), sug[0] if sug else None)
        if not first:
            step("models", False, "no chat model installed and nothing in the catalog fits this machine")
            summary["needs"].append("Install a small chat model by hand (for example `ollama pull qwen2.5-coder:7b`).")
            return _finish(summary, say)
        q = f"Download {first['pull']} (about {first['size_gb']} GB) as your first model?"
        go = yes or (ask(q) if ask else False)
        if not go:
            step("models", False, f"no chat model installed. Suggested: {first['pull']} ({first['size_gb']} GB). Run `architect.py pull {first['pull']}` or `ready --yes`.")
            summary["needs"].append(f"Download a model: architect.py pull {first['pull']}")
            return _finish(summary, say)
        if not pull(first["pull"], confirm=True, say=say):
            step("models", False, f"the download of {first['pull']} did not finish")
            return _finish(summary, say)
        have = _installed(mh, hw)
    step("models", bool(have), f"{len(have)} chat model(s): {', '.join(have[:4])}")
    if not have:
        return _finish(summary, say)

    say("[3/5] tuning")
    try:
        prof = arch_autotune.tune(probe=probe, apply=True, say=lambda m: say("      " + m.strip()), hw=hw)
    except arch_autotune.TuneError as e:
        step("tune", False, str(e))
        return _finish(summary, say)
    step("tune", True, f"{prof['tier_label']}, {prof['crew']} crew, builder {prof['builder']}")
    try:
        import arch_modelopt
        wrote = arch_modelopt.apply(mh.model_list(hw, "f16", 192.0, 50.0), hw, say=lambda m: None)
        step("optimize", True, f"{len(wrote)} sampling value(s) set from each model family's guidance (empty ones only; `optimize --undo` reverts)" if wrote else "every model already has its family settings")
    except (arch_autotune.TuneError, mh.OllamaError) as e:
        step("optimize", False, getattr(e, "msg", None) or str(e))
    summary["profile"] = {k: prof.get(k) for k in ("tier", "tier_label", "crew", "builder", "warnings", "changes")}

    say("[4/5] proof: can the builder call tools and follow a format?")
    if proof:
        out = mh.run_tests(prof["builder"], list(PROOF_CATS), progress=lambda m: None)
        _store_proof(mh, prof["builder"], out)
        for cat in PROOF_CATS:
            sc = (out.get(cat) or {}).get("score", 0.0)
            ok = sc >= PROOF_PASS
            step(f"{cat} ({prof['builder']})", ok, f"{sc:.0f}/100")
            if not ok:
                summary["needs"].append(f"{prof['builder']} scored {sc:.0f} on {cat}: the harness will still work, but a larger or newer model would be more reliable.")
        summary["proof"] = {c: (out.get(c) or {}).get("score") for c in PROOF_CATS}
        _edit_format(prof, mh, summary, step, say, ask_model)
        if smoke:
            _smoke(prof, summary, step, say)
    else:
        step("proof", True, "skipped")

    say("[5/5] upgrades worth considering (nothing is downloaded without your yes)")
    try:
        sug = arch_autotune.suggest(hw, mh.model_list(hw, "f16", 192.0, 50.0), mh.read_config(), prof)["suggestions"]
    except Exception as e:                                # advisory only: a failing catalog must not undo a good setup
        sug = []
        say(f"      could not build suggestions: {e}")
    summary["upgrades"] = [{"slot": s["slot"], "pull": s["pull"], "size_gb": s["size_gb"], "verified": s["verified"], "why": s["why"][:2]} for s in sug]
    for s in sug:
        say(f"      [{s['slot']}] {s['pull']}  {s['size_gb']} GB" + ("" if s["verified"] else "  (unverified tag)"))
    if not sug:
        say("      none: nothing in the catalog is a clear step up that fits this machine")
    return _finish(summary, say, hw, prof)


SMOKE_GOAL = "Write slugify(text) that lowercases text, strips punctuation and joins the words with hyphens, with tests for normal text, punctuation and empty input."


def _edit_format(prof, mh, summary, step, say, ask_model):
    """Does the builder really edit files the way the harness needs? Judged by code; sets the edit style that works for it (reversible with `tune --undo`)."""
    import arch_probe
    builder = prof["builder"]
    say(f"      checking how {builder} edits files")
    try:
        m = arch_probe.modes(ask_model or arch_probe.live_ask(builder), say=lambda t: say("   " + t))
    except core.LLMError as e:
        step("edit format", False, f"the model did not answer: {str(e)[:120]}", advisory=True)
        return
    summary["modes"] = m
    pf = core.load_json(arch_autotune.profile_path()) or {}
    pf.setdefault("models", {}).setdefault(builder, {})["modes"] = m
    rec = m.get("recommend_edit_style")
    note = ""
    if rec:
        cfg = mh.read_config()
        cur = (cfg.get("behavior") or {}).get("edit_style")
        if cur != rec and not (cur is None and rec == "patch"):
            u = ((pf.get("undo") or {}).setdefault("settings", {}))
            u.setdefault("behavior.edit_style", {"had": "edit_style" in (cfg.get("behavior") or {}), "value": cur})
            cfg.setdefault("behavior", {})["edit_style"] = rec
            mh.write_config(cfg)
            note = f"; edit style set to '{rec}' for the harness"
    try:
        with open(arch_autotune.profile_path(), "w", encoding="utf-8") as f:
            json.dump(pf, f, indent=1)
    except OSError:
        pass
    step("edit format", bool(rec), (f"{builder} edits correctly with {rec} blocks" + note) if rec else "; ".join(m["notes"]) or "no edit style worked", advisory=False)
    for n in m["notes"]:
        summary["needs"].append(f"{builder}: {n}")


def _smoke(prof, summary, step, say):
    """The honest 'it works' signal: a tiny real task through the real harness, passed or failed by its own tests."""
    import arch_flows
    import tempfile
    t0 = time.time()
    proj = tempfile.mkdtemp(prefix="smoke_", dir=core.data_dir() if os.path.isdir(core.data_dir()) else None)
    say("      smoke test: building a tiny function with tests through the whole harness")
    try:
        cfg = core.load_config(proj)
        cfg["limits"]["max_calls"] = 14
        cfg["limits"]["max_revisions"] = 2
        cfg["behavior"]["evaluator"] = "off"
        cfg["behavior"]["postmortem"] = "off"
        e = core.Engine(proj, "build", SMOKE_GOAL, cfg, review="auto", say=lambda m: None, persist=False)
        out = arch_flows.run_flow(e, "build", {"domains": ["api"]})
        ok = bool(out.get("ok"))
        step("smoke test", ok, f"{round(time.time() - t0)}s, {e.calls} model calls" + ("" if ok else ": the harness could not finish a tiny task; see the Scoreboard and try a larger model"))
        summary["smoke"] = {"ok": ok, "secs": round(time.time() - t0), "calls": e.calls}
        if not ok:
            summary["needs"].append("The smoke test (a tiny build with tests) did not pass with this team. Run `architect.py run --flow build` on a small task to see where it stops.")
    except (core.LLMError, core.BudgetExceeded, core.LoopDetected) as ex:
        step("smoke test", False, str(ex)[:160])
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def machine_card(summary, prof, hw):
    """A plain-text record of this machine's setup, saved next to the other measured data and shown on Health > Tune."""
    L = ["# This machine", "", f"- GPU: {hw.get('gpu') or 'none'}  ({hw.get('vram_gb', 0):.1f} GB VRAM), RAM {hw.get('ram_gb', 0):.0f} GB, {hw.get('cpu_cores')} CPU threads",
         f"- Harness: {prof.get('tier_label')} / {prof.get('crew')} crew, builder {prof.get('builder')}"]
    for name, m in (prof.get("models") or {}).items():
        me = m.get("measured") or {}
        L.append(f"- {name}: window {m.get('ctx')}" + (f", {me.get('gen_tok_s')} tok/s" if me.get("gen_tok_s") else "") + (f", {me.get('gpu_pct')}% on GPU" if me.get("gpu_pct") is not None else ""))
    L += ["", "## Checks", ""] + [f"- {'OK  ' if s['ok'] else 'NEXT'} {s['name']}: {s['detail']}" for s in summary["steps"]]
    if summary.get("needs"):
        L += ["", "## Next", ""] + [f"- {n}" for n in summary["needs"]]
    try:
        import arch_power
        pw = hw.get("power") or {}
        if pw.get("present"):
            L.insert(3, f"- Power: laptop, {'on battery' if pw.get('on_battery') else 'plugged in'}" + (f" ({pw['pct']}%)" if pw.get("pct") is not None else ""))
        L += ["", "## For this hardware", ""] + [f"- {t}" for t in arch_power.tips(hw, pw)]
    except Exception:
        pass
    return "\n".join(L) + "\n"


def _finish(summary, say, hw=None, prof=None):
    summary["ok"] = bool(summary["steps"]) and all(s["ok"] or s.get("advisory") for s in summary["steps"])
    if hw and prof:
        try:
            with open(os.path.join(core.data_dir(), "machine.md"), "w", encoding="utf-8") as f:
                f.write(machine_card(summary, prof, hw))
            summary["card"] = os.path.join(core.data_dir(), "machine.md")
        except OSError:
            pass
    try:
        os.makedirs(core.data_dir(), exist_ok=True)
        with open(ready_path(), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
    except OSError:
        pass
    say("")
    say("READY: yes" if summary["ok"] else "READY: not yet")
    for n in summary["needs"]:
        say(f"  next: {n}")
    return summary
