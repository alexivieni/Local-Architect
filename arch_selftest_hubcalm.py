"""Self-tests for the hub's calm layer (UX review 2026-11-04): one card style, AA text contrast in both themes, the New task page in
two columns, empty states that say what to do, the "?" shortcuts panel, visible focus, reduced motion, and the 375 px rules.

    python arch_selftest_hubcalm.py

Reads hub_ui.html only; nothing is started and nothing is written. The JavaScript syntax check needs `node`; without it that one
check reports COULD NOT RUN (not a pass, not a fail).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _lum(hexv):
    h = hexv.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tok(block, name):
    m = re.findall(r"--" + re.escape(name) + r":(#[0-9a-fA-F]{3,6})\b", block)
    return m[-1] if m else None


def _balanced(src):
    """Brackets balance outside string literals (a cheap guard on the hand-edited runPage)."""
    pairs, stack, i, q = {")": "(", "]": "[", "}": "{"}, [], 0, None
    while i < len(src):
        c = src[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in "\"'`":
            q = c
        elif c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack or stack.pop() != pairs[c]:
                return False
        i += 1
    return not stack and q is None


def run():
    results, unknown = [], []

    def check(name, ok, extra=""):
        if ok is None:
            unknown.append(name)
            print(f"  ----  {name}   (could not run: {extra})")
            return
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    html = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
    css = html[html.index("<style>"):html.index("</style>")]
    calm_at = css.find("CALM LAYER")
    calm = css[calm_at:] if calm_at >= 0 else ""
    base = css[:css.index("@media (prefers-color-scheme:dark)")]
    dark = re.search(r":root\[data-theme=dark\]\{(.*?)\}", css, re.S).group(1)
    calm_light = re.search(r":root\{(.*?)\}", calm, re.S).group(1) if calm else ""
    calm_dark = (re.search(r":root\[data-theme=dark\]\{(.*?)\}", calm, re.S) or [None, ""])[1] if calm else ""

    check("the calm layer exists and is the last word in the stylesheet", calm_at > 0 and css.rfind("@media (max-width:600px)") > calm_at)

    # ---- 1. consistency
    check("the corner ticks (read as a dashed edge) are switched off on every card",
          ".card::after,.thread::after,.doing::after,.tiercard::after{content:none}" in calm)
    check("dashed borders become solid (start buttons, thinking box, foreign card, 'more' tile, Blueprints canvas)",
          ".starts button,.starts button:hover,.ccthink,.bp-card.foreign,.crew .more{border-style:solid}" in calm and ".bp-canvas{border:1px solid var(--line)}" in calm)
    check("one radius pair: cards use --r, controls and rows use --rs", "--r:12px;--rs:8px" in calm and ".btn,input,select,textarea" in calm and "{border-radius:var(--rs)}" in calm)
    check(".pri is the same filled primary as .p (Send used to look secondary)", ".btn.p,.btn.pri{background:var(--acc);color:var(--on-acc)" in calm)
    check("plain cards no longer jump on hover; clickable ones still do", ".card:not([role=button]):hover{box-shadow:var(--shadow);border-color:var(--line);transform:none}" in calm)

    # ---- 2. contrast (WCAG AA 4.5:1 for small text)
    light_panel, dark_panel = "#fffffd", _tok(dark, "panel")
    pairs = [
        ("light: faint text on a card", _tok(calm_light, "faint"), light_panel),
        ("light: muted text on a card", _tok(base, "mute"), light_panel),
        ("light: good chip", _tok(calm_light, "good"), _tok(base, "goodbg")),
        ("light: warning chip", _tok(calm_light, "warn"), _tok(base, "warnbg")),
        ("light: error chip", _tok(base, "bad"), _tok(base, "badbg")),
        ("light: infra chip", _tok(calm_light, "infra"), _tok(base, "infrabg")),
        ("light: accent chip", _tok(base, "acc"), _tok(base, "acc2")),
        ("light: Claude text on a card", _tok(calm_light, "claude-ink"), light_panel),
        ("light: text on the primary button", _tok(base, "on-acc"), _tok(base, "acc")),
        ("dark: faint text on a card", _tok(calm_dark, "faint"), dark_panel),
        ("dark: muted text on a card", _tok(dark, "mute"), dark_panel),
        ("dark: warning chip", _tok(dark, "warn"), _tok(dark, "warnbg")),
        ("dark: Claude text on a card", _tok(calm_dark, "claude-ink"), dark_panel),
        ("dark: text on the primary button", _tok(dark, "on-acc"), _tok(dark, "acc")),
    ]
    for name, fg, bg in pairs:
        ok = bool(fg and bg) and contrast(fg, bg) >= 4.5
        check(f"AA contrast, {name}", ok, f"{fg} on {bg} = {contrast(fg, bg):.2f}" if fg and bg else "token missing")
    check("the Claude orange is a token, and the lanes/bars stop using stray colours", "--claude:#d97757" in calm and ".lane-b.heavy{background:var(--llm)}" in calm and ".crew-bar .bar>i{background:var(--claude)}" in calm)
    check("figures use tabular numbers", "font-variant-numeric:tabular-nums" in calm and "td.n" in calm)

    # ---- New task page
    rp = html[html.index("function runPage(){"):html.index("const EXAMPLES=")]
    check("New task is two columns: the request + templates | output + past runs + tools",
          'h("div",{class:"runlay"},h("div",{class:"runmain"}' in rp and 'h("div",{class:"runside"}' in rp and ".runlay{display:grid" in calm)
    check("runPage still balances its brackets after the layout change", _balanced(rp))
    check("the output card is no longer called 'Console' (a different page)", '"jobTitle"},"Run output"' in rp and 'j.title||"Run output"' in html)

    # ---- 3. boxes
    check("the output box grows with its content and scrolls inside itself", ".term{height:auto;min-height:180px;max-height:min(60vh,560px)}" in calm)
    check("empty output, runs and queue each say what to do",
          ".term:empty::before{content:" in calm and "No runs in this folder yet. Describe a change" in html and "Nothing queued yet. Type a task above" in html
          and 'emptyState("projects","Pick a project first","Runs belong to a project folder.","Choose project"' in html)  # 2026-11-05: an empty state with its action
    check("runs and queue show a loading shimmer first", 'box.append(h("div",{class:"skeleton"' in html and 'id:"qList"},h("li",{class:"skeleton"' in html)
    check("long lists scroll inside their box", ".boxlist{max-height:420px;overflow:auto}" in calm and ".qlist{max-height:" in calm)

    # ---- 4. plain words
    check("the page title matches the sub-tab you clicked (Home is called Home)", '$("#pageTitle").textContent=id==="overview"?"Home":(SUBNAME[id]||p[2])' in html)
    check("jargon renamed: 'resident' -> 'loaded', 'call trace' -> 'model calls', 'fake model' -> 'no GPU needed'",
          '" models")),"loaded"))' in html and '"model calls")' in html and "Self-test (no GPU needed)" in html)

    # ---- 5. phone
    check("375 px: no sideways scroll and 36 px tap targets", "body{overflow-x:clip}" in calm and "@media (max-width:820px),(pointer:coarse)" in calm and "{min-height:36px}" in calm)

    # ---- 6. delights
    check("'?' opens the shortcuts panel; the command list stays on Ctrl+K",
          'if(e.key==="?"){e.preventDefault();HELP_EL?closeHelp():openKeys();return;}' in html and "function openKeys()" in html
          and '$("#keysBtn").addEventListener("click"' in html and 'id="keysBtn"' in html)
    check("the shortcuts list names Ctrl+K, Esc, / and j/k", all(f'["{k}"' in html for k in ("Ctrl", "Esc", "/", "j")) and "const KEYS=[" in html)
    check("Esc closes a panel before it would ever stop a run",
          html.index("if(HELP_EL){e.preventDefault();closeHelp();return;}") < html.index("if(EV.running){e.preventDefault();"))
    check("focus is always visible (no outline:none on :focus-visible)", ":focus-visible{outline:none" not in css and ":focus-visible{outline:2px solid var(--acc)" in calm)
    check("the page-enter transition stands down under reduced motion", "@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;" in calm)  # the one global block covers #view and .card

    # ---- the script still parses
    node = shutil.which("node")
    if not node:
        check("the hub script parses (node --check)", None, "node is not installed")
    else:
        start = re.search(r'<script>\r?\n"use strict";', html).start()
        js = "\n".join(re.findall(r"<script>(.*?)</script>", html[start:], re.S))   # every script block on its own (there may be more than one)
        d = tempfile.mkdtemp(prefix="archcalm_")
        try:
            p = os.path.join(d, "hub.js")
            open(p, "w", encoding="utf-8").write(js)
            r = subprocess.run([node, "--check", p], capture_output=True, text=True, timeout=60)
            check("the hub script parses (node --check)", r.returncode == 0, r.stderr[-400:])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    n = sum(results)
    tail = f" ({len(unknown)} could not run)" if unknown else ""
    print(f"\n  {'ALL ' + str(n) + ' HUB CALM CHECKS PASSED' + tail if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' HUB CALM CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
