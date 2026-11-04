"""Swap-aware agent chains: what a role change costs on one small GPU, and the team's memory fit sheet.

On a 6-8 GB card only one chat model is resident. Every time the chain hands a step to a role that uses a different model,
Ollama unloads one and loads the other: 3-20 s of pure waiting per swap (measured as `load_duration`), and the previous
model's prefill cache is gone with it. A team of specialists is still right when the specialist's acceptance rate pays for
the swap; the point here is to make that cost visible per run, in seconds, rather than let it hide in "the run felt slow".

Three things, all reversible, none of them reassigning a model:
  * the swap ledger: the Engine counts model changes and their measured load time; the run report gets a "Run economics"
    block with swaps, prefix reuse (arch_core.call) and seconds lost to loading;
  * judge batching (`behavior.swap_aware = on`): in the build flow the per-sprint judge only reports (nothing acts on its
    verdict mid-run), so when the judge is a different model on a small GPU, all sprints are judged in one go at the end, each from the
    evidence saved when it passed (one swap instead of two per sprint); nothing is judged if the build stopped at a failing sprint;
  * the fit sheet (`fit` command, Health page): for each model the team uses, weights + KV cache at its context window
    against this machine's VRAM, which ones coexist, and what a run's swaps cost from the ledger. Estimates are labelled.
"""
import collections
import re

SMALL_VRAM_GB = 10.0     # below this, two chat models never share the card: every role change is a swap
SWAP_MIN_S = 1.0         # a load_duration under this is a warm model, not a swap


# ----------------------------------------------------------------------------------------------- the ledger (per run)
class Swaps:
    def __init__(self):
        self.last = None
        self.n, self.secs, self.imputed, self.resident = 0, 0.0, 0, 0
        self.pairs = collections.Counter()
        self.models = collections.OrderedDict()

    def note(self, model, load_s, stats=None, resident=False):
        """Called after every model call. `model` None = the call ran on another machine (nothing changed on this GPU).
        `load_s` is the server's load_duration in seconds (0.0 = reported warm), or None when it reported nothing.
        `resident` = this model has its own always-loaded process (the llama-server path), so moving between tiers is not a swap and
        costs nothing; the call is still counted, but it never shows up as reload time.
        -> seconds this call spent loading, or 0."""
        if model is None:
            return 0.0
        self.models.setdefault(model, 0)
        self.models[model] += 1
        if resident:
            self.resident += 1
            self.last = model
            return 0.0
        cost = 0.0
        if self.last and model != self.last:
            self.n += 1
            self.pairs[(self.last, model)] += 1
            if load_s is None and stats:                          # nothing reported at all: fall back to the measured load time, and say so
                cost = float(((stats.get(model) or {}).get("load_s")) or 0.0)
                if cost:
                    self.imputed += 1
            else:
                cost = float(load_s or 0.0)                       # a warm swap on a card that holds both models costs what the server says: ~0
            self.secs += cost
        self.last = model
        return cost

    def lines(self, calls, reuse, run_secs=None):
        """Plain-language block for the run report."""
        n_calls = sum(self.models.values()) or calls
        out = ["## Run economics", f"- Model calls: {n_calls} across {len(self.models)} model(s): " + ", ".join(f"`{m}` x{n}" for m, n in self.models.items())]
        if self.n:
            share = f" ({round(100 * self.secs / run_secs)}% of the run)" if run_secs and self.secs else ""
            top = ", ".join(f"`{a}` -> `{b}` x{n}" for (a, b), n in self.pairs.most_common(3))
            src = "the server's reported load times" + (f"; {self.imputed} swap(s) had no report and use the last measured load time" if self.imputed else "")
            out.append(f"- Model swaps: {self.n}, about {round(self.secs)} s spent loading{share} ({src}; a swap also drops the cached prefill). Most frequent: {top}")
        elif self.resident:
            out.append(f"- Model swaps: none. {self.resident} of {n_calls} call(s) went to a model that stays loaded in its own server process, "
                       "so moving between tiers cost no loading time at all")
        else:
            out.append("- Model swaps: none (one resident model for the whole run)")
        if self.resident and self.n:
            out.append(f"- Of those calls, {self.resident} went to an always-loaded model and could not have caused a swap")
        if reuse and reuse.get("cached"):
            out.append(f"- Replies reused: {reuse['cached']} call(s) were answered from a reply this machine had already given to exactly the "
                       "same question, which costs no tokens and no waiting (Settings > Reply cache)")
        if reuse and reuse.get("calls"):
            out.append(f"- Prompt reuse: {reuse['calls']} call(s) read far fewer prompt tokens than the prompt holds, about {reuse['tokens']} tokens in all "
                       "(an estimate: the prompt's size from chars-per-token, minus the server's prompt_eval_count; consistent with a cached prefill)")
        else:
            out.append("- Prompt reuse: no call read noticeably fewer tokens than its prompt holds (first calls, a different model each time, or the server counts cached tokens too)")
        return out


# ----------------------------------------------------------------------------------------------- judge batching
def swap_aware(cfg):
    return str((cfg.get("behavior") or {}).get("swap_aware", "on")).lower() == "on"


def defer_judge(e):
    """Should the build flow judge every sprint at the END instead of after each one? Only when the verdict is report-only
    (it is: the sprint loop never branches on it), the judge runs on a different model than the builder, and the machine
    cannot hold both. -> (True/False, why)."""
    if not swap_aware(e.cfg) or e.cfg["behavior"].get("evaluator") != "each":
        return False, ""
    try:
        judge, builder = e.model_for("evaluator"), e.model_for("generator")
    except Exception:
        return False, ""
    if judge == builder:
        return False, ""
    vram = _vram_gb()
    if vram is None or vram >= SMALL_VRAM_GB:                   # unknown machine: do not reorder anything
        return False, ""
    return True, (f"the judge ({judge}) and the builder ({builder}) are different models and this GPU"
                  f" ({round(vram)} GB) holds one at a time: sprints are judged together at the end from the evidence saved at each sprint, one swap instead of two per sprint")


def _vram_gb():
    try:
        import arch_core
        prof = arch_core.load_json(__import__("os").path.join(arch_core.data_dir(), "profile.json")) or {}
        v = (prof.get("hw") or {}).get("vram_gb")
        return float(v) if v is not None else None
    except Exception:
        return None


# ----------------------------------------------------------------------------------------------- the fit sheet
def fit_sheet(cfg, hw, rows, stats=None, ledger=None):
    """-> {"vram_gb","ram_gb","models":[...],"resident","swap","tips"} for the team in cfg. `rows` are model_hub.model_list rows
    (name, size_gb, arch, fit, rec, embed). Everything here is an estimate from the model's own metadata, labelled as such."""
    import model_hub as mh
    stats = stats or {}
    roles = {r: m for r, m in (cfg.get("roles") or {}).items() if m}
    default = cfg.get("default_model") or ""
    by_model = collections.OrderedDict()
    for r, m in roles.items():
        by_model.setdefault(m, []).append(r)
    if default and default not in by_model:
        by_model[default] = ["(default for unassigned roles)"]
    em = None                                                      # retrieval is SQLite FTS5 now: no embedding model shares the card
    rowmap = {r["name"]: r for r in rows}
    vram = float(hw.get("vram_gb") or 0)
    lim = cfg.get("limits") or {}
    out = {"vram_gb": vram, "ram_gb": float(hw.get("ram_gb") or 0), "models": [], "tips": []}
    for name, rs in by_model.items():
        row = rowmap.get(name) or rowmap.get(name + ":latest") or next((r for r in rows if r["name"].split(":")[0] == name.split(":")[0]), None)
        mo = (cfg.get("model_overrides") or {}).get(name) or {}
        ctx = int(mo.get("num_ctx") or lim.get("num_ctx") or 8192)
        ent = {"model": name, "roles": rs, "installed": bool(row), "ctx": ctx, "embed": bool(row and row.get("embed"))}
        if row:
            arch = row.get("arch") or {}
            kv16, kv8 = mh.kv_bytes_per_token(arch, "f16"), mh.kv_bytes_per_token(arch, "q8_0")
            ent.update(weights_gb=row.get("size_gb"), quant=row.get("quant"), params=row.get("params"),
                       kv_gb=round(kv16 * ctx / mh.GB, 2) if kv16 else None, kv_q8_gb=round(kv8 * ctx / mh.GB, 2) if kv8 else None)
            total = (row.get("size_gb") or 0) + (ent["kv_gb"] or 0)
            ent["total_gb"] = round(total, 2)
            frows = sorted((row.get("fit") or {}).get("rows") or [], key=lambda r: -r["ctx"])
            at = next((r for r in sorted(frows, key=lambda r: r["ctx"]) if r["ctx"] >= ctx), None)   # the fitter's row at (or just above) this window; None = beyond what it modelled
            layers = int(arch.get("layers") or 0)
            full_ctx = next((r["ctx"] for r in frows if r["gpu_ratio"] >= 1.0), None)
            most_ctx = next((r["ctx"] for r in frows if r["gpu_ratio"] >= 0.9), None)
            q8 = f"; with a q8_0 KV cache about {round((row.get('size_gb') or 0) + (ent['kv_q8_gb'] or 0), 1)} GB" if ent.get("kv_q8_gb") else ""
            ent["gpu_ratio"] = at["gpu_ratio"] if at else None
            if ent["embed"]:
                ent["verdict"] = "tiny; loaded for a batch and released (retrieval.keep_alive = 0)"
            elif not vram:
                ent["verdict"] = "CPU only (no dedicated GPU)"
            elif at is None:
                over = f" (above the model's {arch.get('ctx_max')}-token maximum)" if arch.get("ctx_max") and ctx > arch["ctx_max"] else ""
                ent["verdict"] = (f"by total size, {'fits' if total <= vram - 0.7 else 'does not fit'} on the GPU at {ctx} tokens{over}; no per-layer estimate at a window this large" +
                                  (f"; fully on the GPU at {full_ctx} tokens" if full_ctx else "") + q8)
                ent["gpu_ratio"] = 1.0 if total <= vram - 0.7 else 0.0
            elif total <= vram - 0.7 and at["gpu_ratio"] >= 1.0:
                ent["verdict"] = f"fits on the GPU at {ctx} tokens with about {round(vram - 0.7 - total, 1)} GB to spare"
            elif at and at["gpu_ratio"] >= 0.9:
                off = max(1, layers - int(round(layers * at["gpu_ratio"]))) if layers else None
                ent["verdict"] = (f"mostly on the GPU at {ctx} tokens ({round(100 * at['gpu_ratio'])}%" + (f", about {off} of {layers} layers on the CPU" if off else "") + "): works, but slower" +
                                  (f"; fully on the GPU at {full_ctx} tokens" if full_ctx else "") + q8)
            else:
                ent["verdict"] = (f"spills to the CPU at {ctx} tokens (needs {round(total, 1)} GB of {round(vram, 1)}" + (f", {round(100 * at['gpu_ratio'])}% on the GPU" if at else "") + "); " +
                                  (f"fully on the GPU at {full_ctx} tokens" if full_ctx else f"mostly on the GPU at {most_ctx} tokens" if most_ctx else "does not fit at any window") + q8)
            ms = stats.get(name) or {}
            ent["load_s"] = ms.get("load_s")
            ent["gen_tok_s"] = ms.get("gen_tok_s")
        else:
            ent["verdict"] = "not installed here"
        out["models"].append(ent)
    chat = [m for m in out["models"] if not m["embed"] and m["installed"]]
    big = [m for m in chat if m.get("gpu_ratio") is not None and m["gpu_ratio"] < 1.0] if vram else []      # no GPU: nothing "spills", it all runs from RAM
    if vram and vram < SMALL_VRAM_GB:
        out["resident"] = "one chat model at a time" + (" plus the embedding model while it embeds (released right after)" if em else "")
    elif vram:
        pairs = sum(m.get("total_gb") or 0 for m in chat)
        out["resident"] = (f"the whole team ({round(pairs, 1)} GB) fits together" if pairs <= vram - 0.7 else "one chat model at a time") + (" plus the embedding model" if em else "")
    else:
        out["resident"] = "CPU only: one model at a time, from RAM"
    n_chat = len({m["model"] for m in chat})
    loads = [m.get("load_s") for m in chat if m.get("load_s")]
    if n_chat > 1:
        est = f"about {round(sum(loads) / len(loads))} s each (measured)" if loads else "a few seconds each (not measured yet: run `tune`)"
        out["swap"] = f"{n_chat} chat models: every role change is a swap, {est}"
        if ledger and ledger.get("swaps_per_run"):
            out["swap"] += f"; your runs averaged {ledger['swaps_per_run']} swaps and {ledger['swap_secs_per_run']} s of loading"
    else:
        out["swap"] = "one chat model: no swaps"
    for m in big:
        out["tips"].append(f"{m['model']}: {m['verdict']}. A model partly on the CPU is the biggest silent slowdown on a small card: lower its context (Models tab), "
                           "set OLLAMA_KV_CACHE_TYPE=q8_0 with flash attention (see Settings that suit your models), or use a smaller quant.")
    if n_chat > 1 and vram and vram < SMALL_VRAM_GB and loads and sum(loads) / len(loads) > 8:
        out["tips"].append(f"Swaps cost about {round(sum(loads) / len(loads))} s each on this card. One model for every role (Team > Roles) removes them; the Scoreboard and Run history "
                           "show what a specialist gains per accepted reply, so you can weigh one against the other. The build flow batches the judge either way (behavior.swap_aware).")
    out["tips"].append("Function-level retrieval runs in SQLite (FTS5) inside the project: nothing to download and nothing next to the chat model on the card.")
    return out


def ledger_swaps(runs_dir):
    """Average swaps and loading seconds per finished run, from the run reports' economics blocks. -> dict or {}."""
    import os
    try:
        ids = sorted(os.listdir(runs_dir))
    except OSError:
        return {}
    n, swaps, secs = 0, 0, 0.0
    for rid in ids[-30:]:
        p = os.path.join(runs_dir, rid, "report.md")
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = f.read()
        except OSError:
            continue
        m = re.search(r"Model swaps: (\d+), about (\d+) s", t)
        if m:
            n += 1
            swaps += int(m.group(1))
            secs += float(m.group(2))
        elif "Model swaps: none" in t:
            n += 1
    return {"runs": n, "swaps_per_run": round(swaps / n, 1), "swap_secs_per_run": round(secs / n)} if n else {}


def sheet_text(sheet):
    out = [f"Machine: {sheet['vram_gb']:.1f} GB VRAM, {sheet['ram_gb']:.1f} GB RAM.  Resident: {sheet.get('resident')}", f"Swaps: {sheet.get('swap')}", ""]
    for m in sheet["models"]:
        head = f"{m['model']}  ({', '.join(m['roles'])})"
        if m.get("installed") and not m.get("embed"):
            detail = (f"weights {m.get('weights_gb')} GB {m.get('quant') or ''} + KV {m.get('kv_gb')} GB at {m['ctx']} tokens"
                      + (f" (q8_0 KV: {m.get('kv_q8_gb')} GB)" if m.get("kv_q8_gb") else "") + f" = {m.get('total_gb')} GB")
            meas = ", ".join(x for x in (f"load {m['load_s']} s" if m.get("load_s") else "", f"{m['gen_tok_s']} tok/s" if m.get("gen_tok_s") else "") if x)
            out += [head, f"    {detail}" + (f"   [measured: {meas}]" if meas else ""), f"    -> {m['verdict']}"]
        else:
            out += [head, f"    -> {m['verdict']}"]
    if sheet["tips"]:
        out += ["", "Tips:"] + [f"  * {t}" for t in sheet["tips"]]
    out.append("")
    out.append("Weights from Ollama's own metadata; KV cache = 2 x layers x kv_heads x head_dim x bytes per token (estimate). Nothing here changes a setting.")
    return "\n".join(out)
