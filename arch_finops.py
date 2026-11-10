"""FinOps & Optimization Scorecard: Cloud API vs. Local-Architect cost model, 10-way optimization matrix, and ROI template.

    import arch_finops
    rep = arch_finops.report(days=30, team_devs=5, tasks_per_day=20)
    md  = arch_finops.markdown_report(rep)

Read-only and stdlib-only: reads real local/cloud token & call traces (`arch_usage`, `arch_costs`), cache ledger (`arch_cachestats`),
evals (`arch_costmodel`), hardware (`arch_hw`), and server plan (`arch_server`). Never modifies config or contacts the network.
"""
import glob
import os
import time

import arch_core as core

# Cloud API list prices per 1,000,000 tokens (USD), each with its source and date. The Claude rows come from arch_cloud.PRICES, the
# app's ONE Claude price list (this file kept its own copy, and it drifted: Opus 4.6 at $15/$75 and Haiku 4.5 at $0.80/$4 tripled
# "saved vs Opus"); a cache read is 0.1x the input price. The GPT-4o row is OpenAI's list price as entered by hand; Architect does
# not check it.
import arch_cloud as _cloud

RATES_AS_OF = _cloud.PRICES_AS_OF


def _claude(model_id, label, tier):
    pin, pout = _cloud.PRICES[model_id]
    return {"label": label, "vendor": "Anthropic", "tier": tier, "input": pin, "cached_input": round(pin * 0.1, 4), "output": pout,
            "source": "Anthropic first-party API price list (arch_cloud.PRICES)", "as_of": RATES_AS_OF}


CLOUD_RATES = {
    "claude-opus-4-6": _claude("claude-opus-4-6", "Claude Opus 4.6", "Frontier heavy"),
    "claude-sonnet-4-6": _claude("claude-sonnet-4-6", "Claude Sonnet 4.6", "Standard coding"),
    "gpt-4o": {"label": "GPT-4o", "vendor": "OpenAI", "tier": "Standard multimodal",
               "input": 2.50, "cached_input": 1.25, "output": 10.00,
               "source": "OpenAI list price, entered by hand (not checked by Architect)", "as_of": RATES_AS_OF},
    "claude-haiku-4-5": _claude("claude-haiku-4-5", "Claude Haiku 4.5", "Fast router / light"),
}

SEAT_PLANS = {
    "pro_seat": {"label": "Standard Cloud Coding Seat ($20/dev/mo)", "usd_per_dev_month": 20.0},
    "max_seat": {"label": "Power / Max Cloud Agent Seat ($200/dev/mo)", "usd_per_dev_month": 200.0},
}

# ASSUMED size of one multi-step coding task (plan + 3 tool/edit turns + test check + judge). Not measured: every projection that
# uses it says so, and the page shows these numbers next to it.
REFERENCE_TASK_TOKENS = {
    "calls_per_task": 6,
    "tokens_in_per_task": 18000,
    "tokens_cached_per_task": 11500,
    "tokens_out_per_task": 2400,
}


def cloud_cost_usd(tokens_in, tokens_out, tokens_cached=0, rate_key="claude-sonnet-4-6"):
    """-> USD cost on a cloud API for (tokens_in, tokens_out, tokens_cached)."""
    r = CLOUD_RATES.get(rate_key) or CLOUD_RATES["claude-sonnet-4-6"]
    tin = max(0, int(tokens_in or 0))
    tcached = max(0, min(tin, int(tokens_cached or 0)))
    tuncached = max(0, tin - tcached)
    tout = max(0, int(tokens_out or 0))
    usd = (tuncached * r["input"] + tcached * r["cached_input"] + tout * r["output"]) / 1_000_000.0
    usd_no_cache = (tin * r["input"] + tout * r["output"]) / 1_000_000.0
    return {
        "model": rate_key,
        "label": r["label"],
        "vendor": r["vendor"],
        "tier": r["tier"],
        "usd": round(usd, 4),
        "usd_without_cache": round(usd_no_cache, 4),
        "cache_savings_usd": round(max(0.0, usd_no_cache - usd), 4),
    }


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _optimization_layers(cache_ledger, hw, cfg, cost_summary, evals):
    """The 10 optimization layers. `status` is READ from this install, never assumed:
      on         - a setting turns it on here and it is on
      off        - its setting is off here
      built in   - part of the code path on every run (no setting)
      not set up - it needs something this machine does not have yet (llama-server, another machine)
    `gain` is what the layer is expected to give, with `gain_basis` saying where that number comes from (measured on the reference
    laptop, by arithmetic, or an estimate); `measured` is what was read on THIS machine."""
    layers_by_id = {l.get("id"): l for l in (cache_ledger.get("layers") or []) if isinstance(l, dict)}
    route_l = layers_by_id.get("route") or {}
    reply_l = layers_by_id.get("reply") or {}
    prefix_l = layers_by_id.get("prefix") or {}
    slots_l = layers_by_id.get("slots") or {}
    cfg = cfg or {}
    server = cfg.get("server") or {}
    cache_cfg = cfg.get("cache") or {}
    squad = cfg.get("squad") or {}
    hosts = cfg.get("hosts") or {}
    tiers = cfg.get("tiers") or {}
    gpu_name = (hw or {}).get("gpu") or "CPU / integrated"
    vram_gb = float((hw or {}).get("vram_gb") or 0.0)
    ram_gb = float((hw or {}).get("ram_gb") or 0.0)
    try:
        import arch_server
        llama = bool(arch_server.resolve_binary(cfg))
    except Exception:
        llama = False
    rho = prefix_l.get("rho")
    reply_entries = (route_l.get("entries") or 0) + (reply_l.get("entries") or 0)
    reply_hits = (route_l.get("hits") or 0) + (reply_l.get("hits") or 0)
    best_eval = None
    for e in (evals or []):
        if e.get("C") is not None and (best_eval is None or e["C"] < best_eval["C"]):
            best_eval = e
    seed = (cfg.get("behavior") or {}).get("seed")
    perf = (hw or {}).get("cpu_perf_cores")
    kv = str(server.get("kv") or "f16")
    return [
        {"num": 1, "id": "exact_query_cache", "category": "Cache",
         "title": "Exact reply cache and router memo (responses.db, route_cache.json)",
         "status": "on" if cache_cfg.get("responses", True) else "off",
         "before": "Every identical prompt or routing question ran the model again.",
         "after": "A temperature-0 or seeded reply, and a routing decision the router model made, is answered again from disk"
                  + ("" if seed is not None else "; with no seed set, only temperature-0 calls are kept") + ".",
         "gain": "a repeated call costs no model time", "gain_basis": "by design (a hit skips the model)",
         "measured": f"{reply_entries} cached entries, {reply_hits} recorded hits",
         "code_ref": "arch_cache.py, arch_cachestats.py"},
        {"num": 2, "id": "kv_prefix_reuse", "category": "Cache",
         "title": "Prompt prefix ordering and KV slot reuse",
         "status": ("on" if server.get("slots", True) else "off") if llama else "not set up",
         "before": "Changing text near the top of a prompt invalidated the server's KV cache on every turn.",
         "after": "Stable instructions and project context come first, so llama-server reuses the cached prefix (cache_prompt) and saves "
                  "slots to disk" + ("" if llama else " (needs llama-server; Ollama keeps its own prefix cache)") + ".",
         "gain": "less prompt to re-read on a retry", "gain_basis": "estimate",
         "measured": (f"{round(rho * 100)}% of prompt tokens served from the prefix cache over {prefix_l.get('calls', 0)} calls"
                      if rho is not None else "not measured yet (no traced llama-server calls)")
                     + f"; {slots_l.get('entries', 0)} saved slot files",
         "code_ref": "arch_chain.py, arch_server.py, arch_transport.py"},
        {"num": 3, "id": "tool_tree_cache", "category": "Cache",
         "title": "Read-only tool cache and memoized tree fingerprint",
         "status": "built in",
         "before": "Repeated ls/grep/search/outline calls walked the project again.",
         "after": "The results of ls, grep, search and outline are kept per run and dropped when Architect writes a file; the token "
                  "count of an unchanged context section is reused.",
         "gain": "no repeated directory walks inside one run", "gain_basis": "by design",
         "measured": "not counted (the cache lives inside one run; 4 tools use it: ls, grep, search, outline)",
         "code_ref": "arch_tools.py (CACHED, _fingerprint), arch_core.py (_tok_section_cache)"},
        {"num": 4, "id": "q8_kv_flash_attn", "category": "Memory",
         "title": "Quantized KV cache and flash attention",
         "status": "on" if kv in ("q8_0", "q4_0") and server.get("flash_attn", True) else "off",
         "before": "An f16 KV cache needed twice the graphics memory for the same context.",
         "after": f"llama-server starts with a {kv} KV cache and flash attention when its build supports them (f16 otherwise); Ollama "
                  "gets OLLAMA_KV_CACHE_TYPE and OLLAMA_FLASH_ATTENTION only when Architect starts Ollama itself.",
         "gain": "half the KV memory at q8_0", "gain_basis": "by arithmetic (q8_0 stores half the bytes of f16)",
         "measured": f"setting server.kv = {kv}; {gpu_name}, {vram_gb:.1f} GB",
         "code_ref": "arch_server.py (plan), arch_ollama.py"},
        {"num": 5, "id": "gpu_residency_scoring", "category": "Hardware",
         "title": "Prefer models that fit on the graphics card",
         "status": "built in",
         "before": "Models were ranked by size alone, so a model half in system memory could win.",
         "after": "Role scoring takes points off a model that would run partly in system memory, and the server plan subtracts what "
                  "the card already holds.",
         "gain": "55 tok/s fully on the card vs about 8 tok/s split (qwen3:8b)", "gain_basis": "measured once on the reference laptop (RTX 4050 6 GB, 2026-11-05)",
         "measured": f"worker: {tiers.get('worker') or 'not chosen yet'}, router: {tiers.get('router') or 'not chosen yet'}",
         "code_ref": "arch_roles.py, arch_port.py, arch_server.py"},
        {"num": 6, "id": "role_ctx_trimming", "category": "Memory",
         "title": "Prompts fitted to the served window",
         "status": "built in",
         "before": "A prompt larger than the model's window failed or was silently cut by the server.",
         "after": "Each call is fitted to the smaller of the setting and the server's own window: the format example goes first, "
                  "then the longest project context is cut at a section boundary.",
         "gain": "no overflowing prompts", "gain_basis": "by design",
         "measured": "not counted (a prompt that had to be cut is noted in that run's trace)",
         "code_ref": "arch_core.py (Engine._call), arch_tune.py (cut_context)"},
        {"num": 7, "id": "squad_shared_prefix", "category": "Cache",
         "title": "Crew shared prefix and step cache",
         "status": "on" if int(squad.get("step_cache_days", 30) or 0) > 0 else "off",
         "before": "Crew agents re-read files from scratch and re-ran unchanged steps.",
         "after": "The crew's shared header carries outlines of the target files, and a step whose inputs, files and dependencies "
                  "are unchanged is replayed from the step cache.",
         "gain": "an unchanged step costs no model time", "gain_basis": "by design",
         "measured": f"step cache kept {int(squad.get('step_cache_days', 30) or 0)} days",
         "code_ref": "arch_squad.py (shared_header, step_key)"},
        {"num": 8, "id": "hybrid_cpu_power", "category": "Hardware",
         "title": "Performance-core threads and a cached battery check",
         "status": "built in",
         "before": "Model threads could land on efficiency cores, and every battery check started PowerShell.",
         "after": "Threads are sized to the performance cores, and the battery state is cached for 30 seconds.",
         "gain": "faster CPU work on hybrid processors", "gain_basis": "estimate",
         "measured": (f"{perf} performance cores read on this processor" if perf else "performance cores could not be read here"),
         "code_ref": "arch_hw.py (perf_cores), arch_power.py (battery, _BAT_CACHE)"},
        {"num": 9, "id": "distributed_lan_mesh", "category": "Distributed",
         "title": "Other machines on your network, with local fallback",
         "status": "on" if hosts else "not set up",
         "before": "Every model call ran on this machine.",
         "after": "Roles can run on other machines you add; hosts are ranked by speed / (1 + calls in flight), and a call falls back "
                  "to this machine when a host stops answering.",
         "gain": "more machines, more parallel calls", "gain_basis": "estimate",
         "measured": f"{len(hosts)} other machine(s) added" if hosts else "none added (Machine > Computers)",
         "code_ref": "arch_hosts.py (rank_hosts, mark_down), arch_core.py"},
        {"num": 10, "id": "verified_loop_cost", "category": "Verification",
         "title": "Code-checked results and a cost per accepted change",
         "status": "built in",
         "before": "A model's own claim that it was done was taken at face value.",
         "after": "Tests, AST checks and the fake-code gate decide every step; the cost model (C = t / p) ranks models per kind of task.",
         "gain": "fewer wasted retries", "gain_basis": "estimate",
         "measured": (f"best measured C = {best_eval['C']} s per accepted change ({best_eval.get('model', '')})" if best_eval
                      else "no benchmark run yet (architect.py eval --set representative)"),
         "code_ref": "arch_checks.py, arch_fakecode.py, arch_costmodel.py"},
    ]


def report(days=30, roots=None, team_devs=5, tasks_per_day=20):
    """-> Full FinOps & Optimization report dict for `/api/arch/finops` and `markdown_report()`."""
    days = max(1, min(365, int(days or 30)))
    team_devs = max(1, min(500, int(team_devs or 5)))
    tasks_per_day = max(1, min(1000, int(tasks_per_day or 20)))

    import arch_cachestats
    import arch_costmodel as cm
    import arch_costs
    import arch_hw
    import arch_usage

    cfg = _safe_call(core.load_config, {})
    hw = _safe_call(arch_hw.detect, {})
    cache_ledger = _safe_call(arch_cachestats.ledger, {"layers": [], "findings": []})
    window = "day" if days <= 1 else "week" if days <= 7 else "month"
    usage = _safe_call(lambda: arch_usage.usage(window, roots=roots, fresh=True), {})      # (it called arch_usage.summary, which does not exist: always 0)
    costs = _safe_call(lambda: arch_costs.summary(days=days, roots=roots), {})
    eval_files = sorted(glob.glob(os.path.join(core.data_dir(), "evals", "*.json")))
    evals = _safe_call(lambda: cm.from_evals(eval_files), [])

    loc = (usage or {}).get("local") or {}
    cl = (usage or {}).get("claude") or {}
    cache_u = (usage or {}).get("cache") or {}

    measured_calls = int(loc.get("calls") or 0)
    measured_in = int(loc.get("tokens_in") or 0)
    measured_out = int(loc.get("tokens_out") or 0)
    measured_runs = int((costs or {}).get("runs") or (usage or {}).get("runs") or 0)
    measured_passed = int((costs or {}).get("passed") or (usage or {}).get("passed") or 0)

    # The share of prompt tokens the local server served from its prefix cache: measured, or None. (It fell back to a fixed 0.64.)
    # usage's prefix_hit_rate is a share of CALLS, not of tokens, so it is not used for this.
    prefix_layer = next((l for l in (cache_ledger.get("layers") or []) if l.get("id") == "prefix"), {})
    rho = prefix_layer.get("rho")
    rho_val = float(rho) if rho is not None else None
    measured_cached_in = int(round(measured_in * rho_val)) if rho_val is not None else 0

    # Cloud equivalent of actual measured local tokens
    measured_equivalents = {
        k: cloud_cost_usd(measured_in, measured_out, measured_cached_in, k)
        for k in CLOUD_RATES
    }
    actual_claude_usd = round(float(cl.get("usd") or (costs or {}).get("cost_usd") or 0.0), 4)
    local_share = (costs or {}).get("local_share") if (costs or {}).get("runs") else None     # measured, or None (it invented 0.85 / 1.0)

    # Team projection / ROI simulator (monthly = 22 working days, 1-year = 260 working days, 3-year = 780 working days)
    monthly_tasks = team_devs * tasks_per_day * 22
    proj_in = monthly_tasks * REFERENCE_TASK_TOKENS["tokens_in_per_task"]
    proj_cached = monthly_tasks * REFERENCE_TASK_TOKENS["tokens_cached_per_task"]
    proj_out = monthly_tasks * REFERENCE_TASK_TOKENS["tokens_out_per_task"]

    projected_models = {}
    for k in CLOUD_RATES:
        mc = cloud_cost_usd(proj_in, proj_out, proj_cached, k)
        m_usd = mc["usd"]
        m_uncached = mc["usd_without_cache"]
        projected_models[k] = {
            "model": k,
            "label": mc["label"],
            "vendor": mc["vendor"],
            "tier": mc["tier"],
            "per_task_usd": round(m_usd / max(1, monthly_tasks), 4),
            "monthly_api_usd": round(m_usd, 2),
            "monthly_uncached_usd": round(m_uncached, 2),
            "yearly_api_usd": round(m_usd * 12, 2),
            "three_year_api_usd": round(m_usd * 36, 2),
        }

    pro_seats_mo = round(team_devs * SEAT_PLANS["pro_seat"]["usd_per_dev_month"], 2)
    max_seats_mo = round(team_devs * SEAT_PLANS["max_seat"]["usd_per_dev_month"], 2)
    sonnet_mo = projected_models["claude-sonnet-4-6"]["monthly_api_usd"]
    opus_mo = projected_models["claude-opus-4-6"]["monthly_api_usd"]

    optimizations = _optimization_layers(cache_ledger, hw, cfg, costs, evals)
    status_counts = {}
    for o in optimizations:
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1

    rep = {
        "available": True,
        "generated_at": time.time(),
        "days": days,
        "hardware": {
            "gpu": (hw or {}).get("gpu") or "CPU / integrated",
            "vram_gb": float((hw or {}).get("vram_gb") or 0.0),
            "ram_gb": float((hw or {}).get("ram_gb") or 0.0),
            "worker": ((cfg or {}).get("tiers") or {}).get("worker") or "",
            "router": ((cfg or {}).get("tiers") or {}).get("router") or "",
            "heavy": ((cfg or {}).get("tiers") or {}).get("heavy") or "",
        },
        "measured": {
            "basis": "measured" if measured_calls > 0 else "none_yet",
            "window": window,
            "runs": measured_runs,
            "passed": measured_passed,
            "local_calls": measured_calls,
            "tokens_in": measured_in,
            "tokens_cached_est": measured_cached_in,
            "tokens_out": measured_out,
            "total_tokens": measured_in + measured_out,
            "prefix_reuse_rho": round(rho_val, 3) if rho_val is not None else None,
            "cache_priced": rho_val is not None,     # False: priced without any prompt caching on the cloud side (an upper bound)
            "exact_hit_rate": cache_u.get("exact_hit_rate"),
            "local_cost_usd": 0.0,
            "actual_cloud_usd": actual_claude_usd,
            "local_share": round(float(local_share), 3) if local_share is not None else None,
            "cloud_equivalents": measured_equivalents,
            # what the measured local tokens would have cost on each API at list prices (not money saved: hardware and power are not counted)
            "would_cost_opus_usd": measured_equivalents["claude-opus-4-6"]["usd"],
            "would_cost_sonnet_usd": measured_equivalents["claude-sonnet-4-6"]["usd"],
            "would_cost_gpt4o_usd": measured_equivalents["gpt-4o"]["usd"],
        },
        "projection": {
            "basis": "projection",
            "assumption": (f"Assumes {REFERENCE_TASK_TOKENS['tokens_in_per_task']:,} input tokens ({REFERENCE_TASK_TOKENS['tokens_cached_per_task']:,} "
                           f"of them cached) and {REFERENCE_TASK_TOKENS['tokens_out_per_task']:,} output tokens per task and 22 working days a "
                           "month; not measured. Local hardware and power are not counted."),
            "team_devs": team_devs,
            "tasks_per_day": tasks_per_day,
            "monthly_tasks": monthly_tasks,
            "reference_task": dict(REFERENCE_TASK_TOKENS),
            "local_monthly_usd": 0.0,
            "pro_seats_monthly_usd": pro_seats_mo,
            "max_seats_monthly_usd": max_seats_mo,
            "sonnet_monthly_api_usd": sonnet_mo,
            "sonnet_yearly_api_usd": round(sonnet_mo * 12, 2),
            "sonnet_three_year_api_usd": round(sonnet_mo * 36, 2),
            "opus_monthly_api_usd": opus_mo,
            "opus_yearly_api_usd": round(opus_mo * 12, 2),
            "opus_three_year_api_usd": round(opus_mo * 36, 2),
            "models": projected_models,
        },
        "cloud_rates": CLOUD_RATES,
        "rates_as_of": RATES_AS_OF,
        "optimizations": optimizations,
        "optimization_count": len(optimizations),
        "status_counts": status_counts,
        "active_optimizations": status_counts.get("on", 0) + status_counts.get("built in", 0),
    }
    rep["markdown"] = markdown_report(rep)
    return rep


def markdown_report(rep):
    """-> The FinOps report as Markdown. Every figure says whether it was measured here, is a projection, or an estimate."""
    hw = rep.get("hardware") or {}
    m = rep.get("measured") or {}
    p = rep.get("projection") or {}
    opts = rep.get("optimizations") or []
    models = p.get("models") or {}
    sc = rep.get("status_counts") or {}
    share = m.get("local_share")
    rho = m.get("prefix_reuse_rho")
    lines = [
        "# Local-Architect: cost and optimization report",
        "",
        f"- **Machine:** {hw.get('gpu', 'Local PC')} ({hw.get('vram_gb', 0):.1f} GB graphics memory, {hw.get('ram_gb', 0):.0f} GB RAM)",
        f"- **Local team:** worker `{hw.get('worker') or 'not chosen'}`, router `{hw.get('router') or 'not chosen'}`, heavy `{hw.get('heavy') or 'not chosen'}`",
        f"- **Share of model calls that ran locally (measured):** {str(round(share * 100)) + '%' if share is not None else 'no runs recorded yet'}",
        f"- **Optimization layers:** {sc.get('on', 0)} on, {sc.get('built in', 0)} built in, {sc.get('off', 0)} off, {sc.get('not set up', 0)} not set up (read from this install)",
        "",
        f"## 1. Measured local usage (last {m.get('window', 'month')})",
        "",
        f"- **Local model calls:** {m.get('local_calls', 0):,} ({m.get('tokens_in', 0):,} input tokens, {m.get('tokens_out', 0):,} output tokens)",
        f"- **Prompt tokens served from the prefix cache:** {str(round(rho * 100)) + '%' if rho is not None else 'not measured yet'}",
        f"- **API bill for this work:** $0.00 (hardware and power are not counted here)",
        f"- **The same tokens on Claude Opus 4.6 would cost:** ${m.get('would_cost_opus_usd', 0):.2f}",
        f"- **On Claude Sonnet 4.6:** ${m.get('would_cost_sonnet_usd', 0):.2f}",
        f"- **On GPT-4o:** ${m.get('would_cost_gpt4o_usd', 0):.2f}",
        f"- Prices: list prices as of {rep.get('rates_as_of', '?')}" + ("" if m.get("cache_priced") else "; priced without prompt caching because no cache share was measured (an upper bound)"),
        "",
        f"## 2. Projection for a team ({p.get('team_devs', 5)} developers x {p.get('tasks_per_day', 20)} tasks a day = {p.get('monthly_tasks', 2200):,} tasks a month)",
        "",
        f"{p.get('assumption', '')}",
        "",
        "| Option | Per task | Monthly | 1 year | 3 years |",
        "|---|---:|---:|---:|---:|",
        "| Local-Architect (this PC / your network): API bill | $0.00 | $0.00 | $0.00 | $0.00 |",
    ]
    for k in ("claude-opus-4-6", "claude-sonnet-4-6", "gpt-4o", "claude-haiku-4-5"):
        mk = models.get(k) or {}
        lines.append(f"| {mk.get('label', k)} API | ${mk.get('per_task_usd', 0):.4f} | ${mk.get('monthly_api_usd', 0):,.2f} | "
                     f"${mk.get('yearly_api_usd', 0):,.2f} | ${mk.get('three_year_api_usd', 0):,.2f} |")
    lines.extend([
        f"| Cloud coding seats ($20 per developer a month) | - | ${p.get('pro_seats_monthly_usd', 0):,.2f} | ${p.get('pro_seats_monthly_usd', 0) * 12:,.2f} | ${p.get('pro_seats_monthly_usd', 0) * 36:,.2f} |",
        f"| Cloud agent seats ($200 per developer a month) | - | ${p.get('max_seats_monthly_usd', 0):,.2f} | ${p.get('max_seats_monthly_usd', 0) * 12:,.2f} | ${p.get('max_seats_monthly_usd', 0) * 36:,.2f} |",
        "",
        "## 3. Optimization layers (status read from this install)",
        "",
        "| # | Layer | Status | What it does now | Expected gain | Basis | Measured here |",
        "|---:|---|---|---|---|---|---|",
    ])
    for o in opts:
        lines.append(f"| {o['num']} | {o['title']} | {o['status']} | {o['after']} | {o['gain']} | {o['gain_basis']} | {o['measured']} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# One-Click Live Visual Frontend Demo for Calls & Stakeholder Demos
# ---------------------------------------------------------------------------------------------
_DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Local-Architect v12 · Live Visual Frontend Demo</title>
<style>
:root{
  --bg:#0b1020;--panel:#131b31;--panel2:#192340;--line:#263459;
  --ink:#f1f5f9;--mute:#94a3b8;--acc:#38bdf8;--acc2:#818cf8;
  --good:#22c55e;--warn:#f59e0b;--bad:#ef4444;
  --font:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:"Cascadia Code",Consolas,monospace;
}
body.light{
  --bg:#f8fafc;--panel:#ffffff;--panel2:#f1f5f9;--line:#e2e8f0;
  --ink:#0f172a;--mute:#64748b;--acc:#0284c7;--acc2:#4f46e5;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font);transition:background .2s,color .2s}
.wrap{max-width:1180px;margin:0 auto;padding:24px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-bottom:18px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,var(--acc),var(--acc2));display:grid;place-items:center;color:#fff;font-weight:800;font-size:1.2rem;box-shadow:0 6px 20px rgba(56,189,248,.25)}
h1{margin:0;font-size:1.35rem;letter-spacing:-.02em}
.sub{color:var(--mute);font-size:.88rem;margin:2px 0 0}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:.78rem;font-weight:600;background:var(--panel2);border:1px solid var(--line)}
.chip.good{color:var(--good);border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.1)}
.chip.acc{color:var(--acc);border-color:rgba(56,189,248,.4);background:rgba(56,189,248,.1)}
.btn{cursor:pointer;border:1px solid var(--line);background:var(--panel2);color:var(--ink);padding:8px 14px;border-radius:8px;font:600 .86rem var(--font);transition:transform .1s,border-color .15s}
.btn:hover{border-color:var(--acc)}
.btn.p{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;border:none}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:20px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 8px 24px rgba(0,0,0,.14)}
.kpi-val{font:800 1.75rem/1.1 var(--mono);color:var(--ink);margin:6px 0 4px}
.kpi-lbl{color:var(--mute);font-size:.82rem}
.grid2{display:grid;grid-template-columns:1.35fr 1fr;gap:18px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
h2{margin:0 0 10px;font-size:1.05rem}
.task-list{display:flex;flex-direction:column;gap:8px;margin-top:12px}
.task{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:10px;background:var(--panel2);border:1px solid var(--line)}
.task.done b{text-decoration:line-through;color:var(--mute)}
input[type=text],select{background:var(--panel2);border:1px solid var(--line);color:var(--ink);padding:8px 12px;border-radius:8px;font:inherit}
.bar-row{display:grid;grid-template-columns:150px 1fr 80px;align-items:center;gap:10px;margin:8px 0;font-size:.85rem}
.bar-track{height:10px;border-radius:999px;background:var(--panel2);overflow:hidden}
.bar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--acc),var(--good))}
footer{margin-top:24px;padding-top:14px;border-top:1px solid var(--line);display:flex;justify-content:space-between;color:var(--mute);font-size:.8rem;flex-wrap:wrap;gap:8px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">A</div>
      <div>
        <h1>Local-Architect v12 · Live Visual Frontend Showcase</h1>
        <p class="sub">Written from a ready-made template, checked by its own pytest tests, served on this computer</p>
      </div>
    </div>
    <div class="row">
      <span class="chip good">● Live Python HTTP + JSON API</span>
      <span class="chip acc" id="testBadge">{{TEST_BADGE}}</span>
      <button class="btn" id="themeBtn" type="button">Toggle Light / Dark</button>
    </div>
  </header>

  <section class="kpis" id="kpiGrid">
    <div class="card"><div class="kpi-lbl">API bill for local runs</div><div class="kpi-val" style="color:var(--good)">$0.00</div><div class="kpi-lbl">hardware and power not counted</div></div>
    <div class="card"><div class="kpi-lbl">Same work on Claude Sonnet 4.6, 5 devs, 1 year</div><div class="kpi-val" id="kpiSaved">{{PROJ_SONNET_YEAR}}</div><div class="kpi-lbl">projection: assumed task size, list prices {{RATES_AS_OF}}</div></div>
    <div class="card"><div class="kpi-lbl">Prompt tokens from the prefix cache</div><div class="kpi-val" id="kpiRho">{{RHO}}</div><div class="kpi-lbl">measured on this computer</div></div>
    <div class="card"><div class="kpi-lbl">Active Feature Sprint Tasks</div><div class="kpi-val" id="kpiTasks">0</div><div class="kpi-lbl">Live state via /api/tasks</div></div>
  </section>

  <div class="grid2">
    <section class="card">
      <div class="row" style="justify-content:space-between">
        <h2>Interactive Feature Board (Live REST API)</h2>
        <span class="chip" id="apiLatency">API: <1 ms</span>
      </div>
      <p class="sub">Add or toggle items below: every action goes to the local Python backend (<code>GET/POST /api/tasks</code>).</p>
      <form id="addForm" class="row" style="margin-top:12px">
        <input type="text" id="titleIn" placeholder="Add a live feature or demo item..." style="flex:1" required/>
        <select id="tagIn">
          <option value="Frontend">Frontend</option>
          <option value="Cache">Cache</option>
          <option value="FinOps">FinOps</option>
          <option value="LAN Mesh">LAN Mesh</option>
        </select>
        <button class="btn p" type="submit">Add Item</button>
      </form>
      <div class="task-list" id="taskList"></div>
    </section>

    <section class="card">
      <h2>Speed figures from the reference laptop</h2>
      <p class="sub">Measured once on an RTX 4050 6 GB laptop (2026-11), not on this computer; KV memory by arithmetic:</p>
      <div style="margin-top:14px">
        <!-- bars are to scale: the two speeds share one scale (306 tok/s = full width), the two shares are out of 100% -->
        <div class="bar-row"><span>Fully on the card</span><div class="bar-track"><div class="bar-fill" style="width:18%"></div></div><b style="text-align:right">55 tok/s</b></div>
        <div class="bar-row"><span>+ n-gram speculation (edits)</span><div class="bar-track"><div class="bar-fill" style="width:100%"></div></div><b style="text-align:right">306 tok/s</b></div>
        <div class="bar-row"><span>KV memory saved by q8_0</span><div class="bar-track"><div class="bar-fill" style="width:50%"></div></div><b style="text-align:right">50%</b></div>
        <div class="bar-row"><span>Prefix cache (here)</span><div class="bar-track"><div class="bar-fill" style="width:{{RHO_W}}%"></div></div><b style="text-align:right">{{RHO}}</b></div>
      </div>
      <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--line)">
        <h2>Generation speed: split across card and RAM vs. fully on the card (reference laptop)</h2>
        <!-- two measured figures, drawn to one scale (55 tok/s = 330 units wide, so 8 tok/s = 48); there is no time series to plot -->
        <svg viewBox="0 0 420 92" width="100%" style="display:block;margin-top:6px" role="img"
             aria-label="Generation speed on the reference laptop: about 8 tokens per second split across card and RAM, 55 fully on the card">
          <text x="0" y="14" fill="var(--mute)" font-size="11">Split across card and RAM</text>
          <rect x="0" y="20" width="48" height="16" rx="4" fill="var(--mute)"/>
          <text x="56" y="33" fill="var(--ink)" font-size="11">about 8 tok/s</text>
          <text x="0" y="60" fill="var(--mute)" font-size="11">Fully on the card</text>
          <rect x="0" y="66" width="330" height="16" rx="4" fill="var(--acc)"/>
          <text x="338" y="79" fill="var(--ink)" font-size="11" font-weight="bold">55 tok/s</text>
        </svg>
      </div>
    </section>
  </div>

  <footer>
    <span>Served by <code>app/server.py</code> · Ready for live editing in Local-Architect Hub</span>
    <span id="clockSpan"></span>
  </footer>
</div>
<script>
const $ = s => document.querySelector(s);
$("#themeBtn").onclick = () => document.body.classList.toggle("light");
async function loadTasks(){
  const t0 = performance.now();
  const r = await fetch("/api/tasks");
  const d = await r.json();
  $("#apiLatency").textContent = "API: " + Math.max(1, Math.round(performance.now() - t0)) + " ms";
  const items = d.tasks || [];
  $("#kpiTasks").textContent = items.filter(x => !x.done).length + " open / " + items.length + " total";
  const box = $("#taskList");
  box.innerHTML = "";
  for(const it of items){
    const row = document.createElement("div");
    row.className = "task" + (it.done ? " done" : "");
    row.innerHTML = `<div><span class="chip acc" style="margin-right:8px">${it.tag}</span><b></b></div>
      <div class="row"><button class="btn" data-id="${it.id}">${it.done ? "Reopen" : "Complete ✓"}</button></div>`;
    row.querySelector("b").textContent = it.title;
    row.querySelector("button").onclick = async () => {
      await fetch("/api/tasks/toggle", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:it.id})});
      loadTasks();
    };
    box.append(row);
  }
}
$("#addForm").onsubmit = async (e) => {
  e.preventDefault();
  const title = $("#titleIn").value.trim();
  const tag = $("#tagIn").value;
  if(!title) return;
  $("#titleIn").value = "";
  await fetch("/api/tasks", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title, tag})});
  loadTasks();
};
setInterval(() => { $("#clockSpan").textContent = "Local node active · " + new Date().toLocaleTimeString(); }, 1000);
loadTasks();
</script>
</body>
</html>
"""

_DEMO_SERVER_PY = '''"""Zero-dependency visual frontend demo server for Local-Architect live calls & stakeholder demos."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(os.path.dirname(HERE), "static", "index.html")

TASKS = [
    {"id": 1, "title": "100% GPU-resident Qwen3 worker + split-penalty role scoring", "tag": "Hardware", "done": True},
    {"id": 2, "title": "q8_0 KV cache quantization + Flash Attention enabled", "tag": "Cache", "done": True},
    {"id": 3, "title": "Cloud API vs. Local $0 FinOps ROI calculator & executive report", "tag": "FinOps", "done": True},
    {"id": 4, "title": "Live visual frontend demo ready for stakeholder screen-share", "tag": "Frontend", "done": False},
]


def list_tasks():
    return list(TASKS)


def add_task(title, tag="Frontend"):
    title = str(title or "").strip()
    if not title:
        raise ValueError("title required")
    nid = max([t["id"] for t in TASKS] or [0]) + 1
    item = {"id": nid, "title": title, "tag": str(tag or "Frontend"), "done": False}
    TASKS.append(item)
    return item


def toggle_task(task_id):
    for t in TASKS:
        if t["id"] == int(task_id):
            t["done"] = not t["done"]
            return t
    raise KeyError(f"task {task_id} not found")


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            return self._send_json(200, {"ok": True, "service": "visual_demo", "tasks": len(TASKS)})
        if self.path.startswith("/api/tasks"):
            return self._send_json(200, {"ok": True, "tasks": list_tasks()})
        if self.path in ("/", "/index.html"):
            body = open(HTML_FILE, encoding="utf-8").read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n > 0 else "{}"
        data = json.loads(raw or "{}")
        if self.path == "/api/tasks":
            try:
                item = add_task(data.get("title"), data.get("tag") or "Frontend")
                return self._send_json(201, {"ok": True, "task": item})
            except ValueError as e:
                return self._send_json(400, {"ok": False, "error": str(e)})
        if self.path == "/api/tasks/toggle":
            try:
                item = toggle_task(data.get("id") or 0)
                return self._send_json(200, {"ok": True, "task": item})
            except (KeyError, ValueError) as e:
                return self._send_json(404, {"ok": False, "error": str(e)})
        self._send_json(404, {"ok": False, "error": "not found"})


def main():
    port = int(os.environ.get("PORT") or "8090")
    for i, a in enumerate(sys.argv[1:]):
        if a == "--port" and i + 2 <= len(sys.argv[1:]):
            port = int(sys.argv[i + 2])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    srv = ThreadingHTTPServer(("127.0.0.1", port), DemoHandler)
    print(f"Visual demo listening on http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
'''

_DEMO_TEST_PY = '''"""Acceptance tests for the Local-Architect Visual Frontend Demo app."""
from app.server import add_task, list_tasks, toggle_task


def test_initial_tasks_present():
    items = list_tasks()
    assert len(items) >= 4
    assert any(t["tag"] == "FinOps" for t in items)


def test_add_and_toggle_task():
    item = add_task("Show live stakeholder demo", tag="Frontend")
    assert item["title"] == "Show live stakeholder demo"
    assert item["done"] is False
    toggled = toggle_task(item["id"])
    assert toggled["done"] is True
'''


def create_visual_demo(root=None, start_server=True):
    """Scaffold, test, and optionally start the interactive Visual Frontend Demo app.

    -> {"ok": True, "root": path, "url": "http://127.0.0.1:<port>", "port": int, "tests_ok": bool, "test_output": str}
    """
    import subprocess
    import sys
    import arch_runner as runner

    if not root:
        root = os.path.join(core.data_dir(), "visual_demo")
    root = os.path.abspath(root)
    os.makedirs(os.path.join(root, "app"), exist_ok=True)
    os.makedirs(os.path.join(root, "static"), exist_ok=True)
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    os.makedirs(os.path.join(root, ".arch"), exist_ok=True)

    with open(os.path.join(root, "app", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(root, "app", "server.py"), "w", encoding="utf-8") as f:
        f.write(_DEMO_SERVER_PY)
    with open(os.path.join(root, "static", "index.html"), "w", encoding="utf-8") as f:
        f.write(_DEMO_HTML)
    with open(os.path.join(root, "tests", "test_demo.py"), "w", encoding="utf-8") as f:
        f.write(_DEMO_TEST_PY)

    handoff = {
        "template": "visual_demo",
        "name": "visual_demo",
        "run": "python -m app.server --port 8090",
        "test": f'"{sys.executable}" -m pytest -q',
    }
    import json as _json
    with open(os.path.join(root, ".arch", "handoff.json"), "w", encoding="utf-8") as f:
        f.write(_json.dumps(handoff, indent=2))

    # Run the demo's own tests. Three outcomes: True passed, False failed, None could not run (pytest missing, or it timed out)
    import re as _re
    try:
        tr = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root, capture_output=True, text=True, timeout=60)
        test_out = ((tr.stdout or "") + (tr.stderr or "")).strip()
        counts = {k: int(n) for n, k in _re.findall(r"(\d+) (passed|failed|error|errors)", test_out)}
        n_pass, n_bad = counts.get("passed", 0), counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        tests_ok = True if tr.returncode == 0 and n_pass else (False if n_bad else None)
    except (subprocess.TimeoutExpired, OSError) as ex:
        test_out, n_pass, n_bad, tests_ok = f"the tests could not run: {type(ex).__name__}: {ex}", 0, 0, None
    # the page's figures are filled in now, from this install (they were fixed numbers in the template)
    badge = (f"pytest: {n_pass}/{n_pass + n_bad} passing" if tests_ok else
             f"pytest: {n_bad} of {n_pass + n_bad} failing" if tests_ok is False else "pytest: could not run")
    try:
        rr = report(days=30, team_devs=5, tasks_per_day=20)
    except Exception:
        rr = {}
    rho = ((rr.get("measured") or {}).get("prefix_reuse_rho"))
    yr = ((rr.get("projection") or {}).get("sonnet_yearly_api_usd"))
    page = (_DEMO_HTML.replace("{{TEST_BADGE}}", badge)
            .replace("{{PROJ_SONNET_YEAR}}", f"${yr:,.0f}" if isinstance(yr, (int, float)) else "n/a")
            .replace("{{RATES_AS_OF}}", RATES_AS_OF)
            .replace("{{RHO}}", f"{round(rho * 100)}%" if rho is not None else "not measured")
            .replace("{{RHO_W}}", str(round(rho * 100)) if rho is not None else "0"))
    with open(os.path.join(root, "static", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)

    srv = {"ok": False, "port": None, "url": None}
    if start_server:
        # Stop any previous instance in this root first, then start fresh
        try:
            runner.stop(root)
        except Exception:
            pass
        st = runner.start(root, "python -m app.server")
        for _ in range(20):
            h = runner.health(root, timeout=0.5)
            if h.get("http_ok"):
                break
            time.sleep(0.15)
        h = runner.health(root, timeout=1.0)
        port = h.get("port") or st.get("port")
        srv = {
            "ok": bool(st.get("ok") and h.get("running")),
            "port": port,
            "http_ok": h.get("http_ok"),
            "url": f"http://127.0.0.1:{port}" if port else None,
        }

    return {
        "ok": bool(tests_ok is True and (srv["ok"] if start_server else True)),
        "root": root,
        "tests_ok": tests_ok,
        "test_output": test_out,
        "server": srv,
        "port": srv.get("port"),
        "url": srv.get("url"),
    }

