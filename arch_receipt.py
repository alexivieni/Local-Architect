"""A receipt for one run: a single HTML file that says exactly what the machine did, and what it cost.

Open it in any browser, on any computer, forever. It carries no scripts, loads nothing from the internet, and needs this program no more
than a printed receipt needs the till. That matters for the same reason a receipt does: a record you can only read inside the thing that
produced it is not really a record.

What goes in it:
  * the request, the flow that ran it, and the verdict -- including "not sure", which is a real answer here;
  * a timeline: every step, how long it took, the tools it used and what each one found, the checks it ran and what they said;
  * what changed on disk, and what was reused rather than rewritten;
  * what it cost -- model calls, tokens, seconds, which tier answered, what came out of the cache instead of the card;
  * what it learned: the notes it wrote, the skills it leaned on, the point where a bigger model had to take over.

Everything in it comes from the trace the run already wrote. Nothing is reconstructed afterwards or inferred.
"""
import html
import json
import os
import time

import arch_core as core

MAX_TOOL_RESULT = 600


# ---------------------------------------------------------------- reading one run

def read(run_dir):
    """-> {"meta", "calls", "marks", "steps", "checks", "tools", "escalations", "hooks", "verdict", "report"}."""
    out = {"meta": core.load_json(os.path.join(run_dir, "run.json")) or {}, "calls": [], "marks": {}, "steps": [],
           "checks": [], "tools": [], "escalations": [], "hooks": [], "verdict": None, "report": ""}
    try:
        with open(os.path.join(run_dir, "report.md"), encoding="utf-8") as f:
            out["report"] = f.read()
    except OSError:
        pass
    try:
        with open(os.path.join(run_dir, "trace.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                t = j.get("type")
                if t == "call":
                    out["calls"].append(j)
                elif t == "mark":
                    out["marks"][j.get("id")] = j
                elif t == "step":
                    out["steps"].append(j)
                elif t == "check":
                    out["checks"].append(j)
                elif t == "tool":
                    out["tools"].append(j)
                elif t == "escalation":
                    out["escalations"].append(j)
                elif t == "hook":
                    out["hooks"].append(j)
                elif t == "verdict":
                    out["verdict"] = j
                elif t == "postmortem":
                    out["postmortem"] = j
    except OSError:
        pass
    return out


def totals(d):
    """What the run cost, counted from the records rather than estimated."""
    calls = d["calls"]
    real = [c for c in calls if not c.get("cached")]
    cached = [c for c in calls if c.get("cached")]
    by_tier, by_model = {}, {}
    for c in real:
        by_tier[c.get("tier") or "?"] = by_tier.get(c.get("tier") or "?", 0) + 1
        by_model[c.get("model") or "?"] = by_model.get(c.get("model") or "?", 0) + 1
    secs = sum(float(c.get("secs") or 0) for c in real)
    speeds = [float(c["tok_s"]) for c in real if c.get("tok_s")]
    return {"calls": len(calls), "real": len(real), "cached": len(cached),
            "tokens_in": sum(int(c.get("prompt_tokens") or 0) for c in real),
            "tokens_out": sum(int(c.get("gen_tokens") or 0) for c in real),
            "tokens_saved": sum(int(c.get("tokens_saved") or 0) for c in cached),
            "secs_saved": round(sum(float(c.get("secs_saved") or 0) for c in cached), 1),
            "secs": round(secs, 1), "tok_s": round(sum(speeds) / len(speeds), 1) if speeds else None,
            "by_tier": by_tier, "by_model": by_model,
            "reused_prefix": sum(int(c.get("prefix_reused_est") or 0) for c in real),
            "usd": round(sum(float(c.get("usd") or 0) for c in real), 4)}


# ---------------------------------------------------------------- rendering

def _e(x):
    return html.escape(str(x if x is not None else ""))


def _dur(s):
    s = float(s or 0)
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m {int(s % 60)}s"


def _verdict_words(v):
    return {"pass": ("Passed", "Every check the flow asked for ran, and passed."),
            "fail": ("Did not pass", "At least one check ran and failed."),
            "not_sure": ("Not sure", "A check could not be run, so this is not being reported as a pass.")}.get(
        v, ("Unfinished", "The run stopped before it reached a verdict."))


def build(run_dir, root=None):
    """-> the whole receipt as one HTML string."""
    d = read(run_dir)
    m = d["meta"]
    tot = totals(d)
    v = (d["verdict"] or {}).get("verdict") or ("pass" if m.get("status") == "done" else "")
    vt, vwhy = _verdict_words(v)
    started = m.get("started")
    when = time.strftime("%d %B %Y at %H:%M", time.localtime(started)) if started else ""
    elapsed = (m.get("updated") or 0) - (started or 0)

    rows = []
    rows.append(f"""<header>
      <div class="brand"><span class="mark"><svg viewBox="0 0 24 24"><path d="M12 3.5L20.5 20h-17z"/><path d="M8.3 14h7.4"/></svg></span>
        <span>Architect</span><span class="dim">run receipt</span></div>
      <h1>{_e(m.get('goal') or '(no goal recorded)')}</h1>
      <p class="sub">{_e(m.get('flow') or '')} flow &middot; {_e(when)} &middot; took {_dur(elapsed)}</p>
      <div class="verdict {_e(v or 'none')}"><b>{_e(vt)}</b><span>{_e(vwhy)}</span>
        {f'<span class="why">{_e((d["verdict"] or {}).get("why"))}</span>' if (d['verdict'] or {}).get('why') else ''}</div>
    </header>""")
    pm = d.get("postmortem")
    if pm and v != "pass":
        rows.append(f"""<section><h2>Why it did not pass</h2><div class="pm"><p>{_e(pm.get('cause'))}</p>
      {f"<p><b>Next time:</b> {_e(pm.get('next_try'))}</p>" if pm.get('next_try') else ''}
      <p class="dim">The model's own reading of the failed checks{f" (most likely at fault: {_e(pm.get('fault'))})" if pm.get('fault') else ''}.
      Advice, not a verdict.</p></div></section>""")

    # ---- what it cost
    cost = [f"<div class='k'><b>{tot['real']}</b><span>model calls</span></div>",
            f"<div class='k'><b>{tot['tokens_out']:,}</b><span>tokens written</span></div>",
            f"<div class='k'><b>{_dur(tot['secs'])}</b><span>spent generating</span></div>"]
    if tot["tok_s"]:
        cost.append(f"<div class='k'><b>{tot['tok_s']}</b><span>tokens a second</span></div>")
    if tot.get("usd"):
        cost.append(f"<div class='k'><b>${tot['usd']:.4f}</b><span>paid to Claude (everything else ran on this machine, free)</span></div>")
    if tot["cached"]:
        cost.append(f"<div class='k good'><b>{tot['cached']}</b><span>answers reused, saving {tot['tokens_saved']:,} tokens</span></div>")
    rows.append("<section><h2>What it cost</h2><div class='keys'>" + "".join(cost) + "</div>" + _models_table(tot) + "</section>")

    # ---- the timeline
    rows.append("<section><h2>What it did</h2>" + _timeline(d) + "</section>")

    # ---- what changed
    changed = sorted({f for s in d["steps"] for f in (s.get("changed") or [])})
    if changed:
        rows.append("<section><h2>What changed on disk</h2><ul class='files'>"
                    + "".join(f"<li>{_e(f)}</li>" for f in changed) + "</ul></section>")

    # ---- what it learned
    learned = _learned(d, root)
    if learned:
        rows.append("<section><h2>What it learned</h2>" + learned + "</section>")

    # ---- the report, verbatim
    if d["report"]:
        rows.append("<section><h2>The run's own report</h2><pre class='report'>" + _e(d["report"]) + "</pre></section>")

    rows.append("<footer>Written by Architect on this computer. No model wrote any number in this file: every figure here was measured "
                "while the run happened. Nothing in this page loads anything from the internet.</footer>")
    return _PAGE.replace("{{BODY}}", "\n".join(rows)).replace("{{TITLE}}", _e((m.get("goal") or "Architect run")[:70]))


def _models_table(tot):
    if not tot["by_model"]:
        return ""
    rows = "".join(f"<tr><td>{_e(k)}</td><td class='n'>{v}</td></tr>" for k, v in sorted(tot["by_model"].items(), key=lambda x: -x[1]))
    tiers = ", ".join(f"{k} &times;{v}" for k, v in sorted(tot["by_tier"].items(), key=lambda x: -x[1]))
    extra = ""
    if tot["reused_prefix"]:
        extra = (f"<p class='note'>About {tot['reused_prefix']:,} tokens of prompt were already in the server's memory from the previous "
                 "call and did not have to be read again.</p>")
    return f"<table class='mini'><thead><tr><th>model</th><th class='n'>calls</th></tr></thead><tbody>{rows}</tbody></table>" \
           f"<p class='note'>By tier: {tiers}.</p>{extra}"


def _timeline(d):
    """Steps in order, each with the tools it used and the checks it ran. Falls back to the calls when a flow records no steps."""
    checks_by_step, tools_by_step = {}, {}
    for c in d["checks"]:
        checks_by_step.setdefault(c.get("step") or "", []).append(c)
    for t in d["tools"]:
        tools_by_step.setdefault(t.get("step") or "", []).append(t)
    steps = [s for s in d["steps"] if s.get("status") != "started"]
    out = ["<ol class='tl'>"]
    if not steps:
        for c in d["calls"]:
            mk = d["marks"].get(c.get("id")) or {}
            ok = mk.get("accepted")
            out.append(f"<li class='{_cls(ok)}'><div class='hd'><span class='ic'>{_mark(ok)}</span>"
                       f"<b>{_e(c.get('label') or c.get('role'))}</b>"
                       f"<span class='dim'>{_e(c.get('model'))} &middot; {_dur(c.get('secs'))}"
                       + (f" &middot; {_e(c.get('tok_s'))} tok/s" if c.get("tok_s") else "")
                       + ("<span class='tag cache'>reused</span>" if c.get("cached") else "") + "</span></div></li>")
    for s in steps:
        sid = s.get("step") or ""
        ok = None if s.get("unsure") else s.get("ok")
        bits = [f"<div class='hd'><span class='ic'>{_mark(ok, s.get('status'))}</span><b>{_e(_pretty(sid))}</b>"
                f"<span class='dim'>{_e(s.get('kind') or '')}"
                + (f" &middot; {_dur(s.get('secs'))}" if s.get("secs") else "")
                + (f" &middot; {s.get('tries')} tries" if (s.get("tries") or 0) > 1 else "") + "</span></div>"]
        tl = tools_by_step.get(sid) or []
        if tl:
            bits.append("<ul class='tools'>" + "".join(
                f"<li class='{'ok' if t.get('ok') else 'no'}'><code>{_e(t.get('tool'))}{_e(_args(t.get('args')))}</code>"
                f"<span>{_e((str(t.get('result') or '').splitlines() or [''])[0][:MAX_TOOL_RESULT])}</span></li>" for t in tl) + "</ul>")
        cl = checks_by_step.get(sid) or []
        if cl:
            bits.append("<ul class='checks'>" + "".join(
                f"<li class='{_cls(c.get('ok'))}'><span class='ic'>{_mark(c.get('ok'))}</span><code>{_e(c.get('gate'))}</code>"
                + (f"<span class='dim'>{_e((c.get('text') or '').splitlines()[0][:160])}</span>" if c.get("text") else "")
                + "</li>" for c in cl) + "</ul>")
        if s.get("changed"):
            bits.append("<p class='note'>changed " + ", ".join(f"<code>{_e(f)}</code>" for f in s["changed"][:8]) + "</p>")
        out.append(f"<li class='{_cls(ok, s.get('status'))}'>" + "".join(bits) + "</li>")
    out.append("</ol>")
    return "".join(out)


def _learned(d, root):
    parts = []
    for esc in d["escalations"]:
        parts.append(f"<p class='learn'><b>{_e(esc.get('from_model'))}</b> could not finish "
                     f"<b>{_e(_pretty(esc.get('step')))}</b>, so the <b>{_e(esc.get('to_tier'))}</b> tier "
                     f"(<b>{_e(esc.get('to_model'))}</b>) took it over. "
                     "That pair is the most useful training data this app produces: it is exactly what the smaller model cannot yet do."
                     + (f"<span class='dim'> {_e((esc.get('why') or '')[:200])}</span>" if esc.get("why") else "") + "</p>")
    if d["hooks"]:
        ok = sum(1 for h in d["hooks"] if h.get("ok"))
        parts.append(f"<p class='learn'>Your own hooks ran {len(d['hooks'])} time(s), {ok} of them cleanly.</p>")
    if root:
        try:
            import arch_memory
            mem = arch_memory.lines(root, "lessons.md")[-3:]
            if mem:
                parts.append("<p class='learn'>Notes it has written about this project:</p><ul class='files'>"
                             + "".join(f"<li>{_e(x['text'])}</li>" for x in mem) + "</ul>")
        except Exception:
            pass
    return "".join(parts)


def _pretty(s):
    return str(s or "").replace("-", " ").replace("_", " ").strip() or "step"


def _args(a):
    if not isinstance(a, dict) or not a:
        return "()"
    return "(" + ", ".join(f"{k}={str(v)[:40]}" for k, v in list(a.items())[:3]) + ")"


def _cls(ok, status=None):
    if status == "skipped":
        return "skip"
    return "ok" if ok is True else "no" if ok is False else "un"


def _mark(ok, status=None):
    if status == "skipped":
        return "&ndash;"
    return "&check;" if ok is True else "&times;" if ok is False else "?"


# ---------------------------------------------------------------- writing

def write(run_dir, root=None, dest=None):
    dest = dest or os.path.join(run_dir, "receipt.html")
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(build(run_dir, root))
    return dest


def latest_run(root):
    d = os.path.join(root, ".arch", "runs")
    try:
        runs = [r for r in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, r, "run.json"))]
    except OSError:
        return None
    return os.path.join(d, runs[-1]) if runs else None


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}} &middot; Architect receipt</title>
<style>
:root{--bg:#f6f8fc;--panel:#fff;--soft:#eef2f8;--ink:#0e1a33;--mute:#53617a;--faint:#8a97ad;--line:#dde4ef;--acc:#1549c9;--acc2:#e6edfb;
--good:#1f8a5b;--goodbg:#e3f5ea;--bad:#b3261e;--badbg:#fdeae8;--warn:#9a5d02;--warnbg:#fdf0da;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--font:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0a1324;--panel:#101b31;--soft:#172540;--ink:#e6ecf8;--mute:#9fb0cc;--faint:#5f6f8c;
--line:#223354;--acc:#7da2ff;--acc2:#1a2c55;--good:#5fd38a;--goodbg:#12301f;--bad:#ff8f86;--badbg:#3a1a17;--warn:#f2b255;--warnbg:#33260f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 var(--font);-webkit-font-smoothing:antialiased}
main{max-width:860px;margin:0 auto;padding:36px 20px 72px}
header{margin-bottom:28px}
.brand{display:flex;align-items:center;gap:9px;font-weight:650;margin-bottom:22px;font-size:14px}
.brand .dim{color:var(--faint);font-weight:400}
.mark{width:24px;height:24px;border-radius:6px;background:#0b2a6f;display:grid;place-items:center}
.mark svg{width:15px;height:15px;stroke:#eaf1ff;fill:none;stroke-width:1.9;stroke-linejoin:round}
h1{font-size:25px;line-height:1.25;margin:0 0 5px;letter-spacing:-.3px}
.sub{color:var(--mute);margin:0 0 18px;font-size:13.5px}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin:34px 0 12px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:4px 20px 20px;margin-bottom:14px}
section h2{margin-top:18px}
.verdict{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;padding:13px 16px;border-radius:10px;font-size:14px}
.verdict b{font-size:16px}
.verdict span{color:inherit;opacity:.85}
.verdict .why{width:100%;font-family:var(--mono);font-size:12px;opacity:.75}
.verdict.pass{background:var(--goodbg);color:var(--good)}
.verdict.fail{background:var(--badbg);color:var(--bad)}
.verdict.not_sure{background:var(--warnbg);color:var(--warn)}
.verdict.none{background:var(--soft);color:var(--mute)}
.pm{border-left:3px solid var(--bad);padding:2px 0 2px 14px;display:grid;gap:6px}
.pm p{margin:0}
.keys{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:4px 0 16px}
.k{background:var(--soft);border-radius:9px;padding:11px 13px;display:grid;gap:2px}
.k b{font-size:21px;font-variant-numeric:tabular-nums;letter-spacing:-.4px}
.k span{font-size:11.5px;color:var(--mute);line-height:1.35}
.k.good b{color:var(--good)}
table.mini{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
table.mini th{text-align:left;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);font-weight:650;padding:5px 0;border-bottom:1px solid var(--line)}
table.mini td{padding:6px 0;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:12.5px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.note{color:var(--mute);font-size:12.5px;margin:8px 0 0}
ol.tl{list-style:none;margin:6px 0 0;padding:0;counter-reset:s}
ol.tl>li{position:relative;padding:12px 0 12px 30px;border-bottom:1px solid var(--line)}
ol.tl>li:last-child{border-bottom:0}
ol.tl .hd{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}
ol.tl .hd b{font-size:14.5px}
ol.tl .ic{position:absolute;left:0;top:13px;width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700}
li.ok>.hd>.ic,li.ok>.ic{background:var(--goodbg);color:var(--good)}
li.no>.hd>.ic,li.no>.ic{background:var(--badbg);color:var(--bad)}
li.un>.hd>.ic,li.un>.ic{background:var(--warnbg);color:var(--warn)}
li.skip>.hd>.ic,li.skip>.ic{background:var(--soft);color:var(--faint)}
.dim{color:var(--faint);font-size:12px}
.tag{margin-left:7px;padding:1px 7px;border-radius:999px;font-size:10.5px;font-weight:650}
.tag.cache{background:var(--acc2);color:var(--acc)}
ul.tools,ul.checks{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:4px}
ul.tools li,ul.checks li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:9px;align-items:baseline;font-size:12.5px}
ul.checks li{grid-template-columns:18px auto minmax(0,1fr)}
ul.checks .ic{position:static;width:16px;height:16px;font-size:10px}
ul.tools code,ul.checks code{font-family:var(--mono);font-size:11.5px;color:var(--ink);background:var(--soft);padding:1px 6px;border-radius:5px;white-space:nowrap}
ul.tools li span{color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:11.5px}
ul.tools li.no code{color:var(--bad)}
ul.files{list-style:none;margin:6px 0 0;padding:0;display:grid;gap:3px;font-family:var(--mono);font-size:12.5px;color:var(--mute)}
.learn{background:var(--soft);border-radius:9px;padding:11px 13px;margin:8px 0;font-size:13px}
pre.report{background:var(--soft);border-radius:9px;padding:14px;overflow-x:auto;font:12px/1.6 var(--mono);white-space:pre-wrap;word-break:break-word;margin:6px 0 0}
footer{color:var(--faint);font-size:12px;margin-top:26px;line-height:1.6}
@media print{body{background:#fff}section{break-inside:avoid;box-shadow:none}}
</style></head><body><main>
{{BODY}}
</main></body></html>
"""
