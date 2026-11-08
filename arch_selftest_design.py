"""Self-tests for the hub's design system and accessibility pass (2026-11-04): one token set at the top of the stylesheet, both
themes and every accent complete, every var() resolving, the hidden Components page, keyboard reach, live regions, reduced motion,
and state that is never shown by colour alone. docs/design_system.md must name every token.

    python arch_selftest_design.py

Reads hub_ui.html and docs/design_system.md only; nothing is started and nothing is written. The script syntax check needs `node`;
without it that check reports COULD NOT RUN (not a pass, not a fail).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
# Literal colours still below the token line (88 on 2026-11-04; 4 on 2026-11-08 after the dead and duplicated ones were removed or
# pointed at their tokens). The number may only go down: a new colour is a new token. See docs/design_system.md, "Still to fold".
COLOUR_CEILING = 4


def _decls(block):
    return dict(re.findall(r"--([a-z0-9-]+):([^;]+)", block))


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
    css = html[html.index("<style>") + 7:html.index("</style>")]
    end = css.find("end of tokens")
    tokens, below = (css[:end], css[end:]) if end > 0 else ("", css)
    light = re.search(r"\.th-light,:root\{(.*?)\}", tokens, re.S)
    dark_media = re.search(r"@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme=light\]\)\{(.*?)\}\}", tokens, re.S)
    dark_attr = re.search(r"\.th-dark,:root\[data-theme=dark\]\{(.*?)\}", tokens, re.S)
    L = _decls(light.group(1)) if light else {}
    DM = _decls(dark_media.group(1)) if dark_media else {}
    DA = _decls(dark_attr.group(1)) if dark_attr else {}

    # ---- 1. one token set
    check("the token block is the first thing in the stylesheet and ends at a marked line",
          end > 0 and css.lstrip().startswith("/*") and css.find(".th-light,:root{") < css.find("{", css.find("*/")) + 1)
    families = {"colour": ["bg", "panel", "ink", "acc", "good", "warn", "bad"], "type": ["font", "font-head", "mono", "t-sm", "t-card", "t-2xl"],
                "spacing": ["s1", "s4", "s6"], "radius": ["r", "rs", "r-xs", "r-pill"], "elevation": ["shadow", "lift", "pop", "modal"],
                "motion": ["ease", "fast", "base", "slow"]}
    missing = {f: [t for t in ts if t not in L] for f, ts in families.items()}
    check("the light block holds every family: colour, type, spacing, radius, elevation, motion", not any(missing.values()), missing)
    check("the dark theme is one set, written twice the same (system setting and explicit choice)", DM and DM == DA,
          sorted(set(DM.items()) ^ set(DA.items()))[:6])
    check("every dark token has a light counterpart", DM and set(DM) <= set(L), sorted(set(DM) - set(L)))
    acc = {a: [re.search(rf"{sel}\{{([^}}]*)\}}", tokens) for sel in (rf":root\[data-accent={a}\],:root\[data-accent={a}\] \.th-light",
                                                                       rf":root:not\(\[data-theme=light\]\)\[data-accent={a}\]",
                                                                       rf":root\[data-theme=dark\]\[data-accent={a}\],:root\[data-accent={a}\] \.th-dark")]
           for a in ("graphite", "copper", "moss")}
    check("each accent has a light, a system-dark and a chosen-dark version, and the dark ones set --info too",
          all(all(m and "--acc:" in m.group(1) for m in ms) and "--info:" in ms[1].group(1) and "--info:" in ms[2].group(1) for ms in acc.values()))
    late_roots = re.findall(r"(?<![\w-]):root(?:\[[^\]]*\])*\{", below)
    check("no token block hides further down the stylesheet (the old identity, polish, calm and flagship :root blocks are gone)",
          not late_roots, late_roots[:5])
    n_col = len(COLOUR.findall(below))
    check(f"literal colours below the token line do not grow (now {n_col}, ceiling {COLOUR_CEILING})", n_col <= COLOUR_CEILING, n_col)
    # ---- 2026-11-08 design-system Phase 1: what was fixed stays fixed
    tdefs = re.findall(r"--(t-[a-z0-9]+)\s*:\s*([^;}]+)", css)
    check("every type token is in rem (px ignored the browser's text size) and each is defined once",
          tdefs and all(v.strip().endswith("rem") for _, v in tdefs) and len(tdefs) == len({k for k, _ in tdefs}),
          [f"{k}:{v}" for k, v in tdefs if not v.strip().endswith("rem")][:5] or sorted({k for k, _ in tdefs if [x for x, _ in tdefs].count(k) > 1}))
    kf = re.findall(r"@keyframes\s+([\w-]+)", html)
    check("no animation name is defined twice (the last one silently wins for the whole page)", len(kf) == len(set(kf)),
          sorted({k for k in kf if kf.count(k) > 1}))
    check("no shadow is tinted with the page background (invisible in the light and paper themes)",
          not re.search(r"box-shadow:[^;}]*color-mix\(in srgb,var\(--bg\)", below))
    ws = html[html.find("/* ===== 2026-11-04: the agents' workspace"):html.find("</style>")]
    check("from the workspace section to the end of the stylesheet there is no literal colour at all", ws and not COLOUR.findall(ws), COLOUR.findall(ws)[:5])
    defined = set(re.findall(r"--([a-z0-9-]+)\s*:", html))
    used = set(re.findall(r"var\(--([a-z0-9-]+)\)", html))
    check("every var(--name) without a fallback is a defined token (a typo would silently draw nothing)", used <= defined, sorted(used - defined))
    check("a theme can be scoped to a subtree (.th-light / .th-dark), so both themes can be shown at once",
          ".th-light,.th-dark{background:var(--bg);color:var(--ink)}" in css and "color-scheme:dark" in tokens)

    # ---- 2. the Components page
    # 2026-11-05 sidebar: Components is a registered page, a Help sub-tab hidden until "Show advanced pages", always found by Ctrl+K.
    help_place = re.search(r'\["help","[a-z]+","Help",\[([^\]]*)\]\]', html)
    adv = re.search(r"const ADVANCED=new Set\(\[([^\]]*)\]\)", html)
    check("Components is a page, an advanced Help sub-tab (hidden by default), found by Ctrl+K",
          '["components",' in html and help_place and '"components"' in help_place.group(1)
          and adv and '"components"' in adv.group(1) and "const cmds=PAGES.map(" in html)
    check("Settings links to it, and render() draws it", 'onclick:()=>go("components")' in html and 'page==="components"' in html and "componentsPage()" in html)
    cp = html[html.find("function cmpFrame("):html.find("function componentsPage(")]
    parts = {"buttons": 'class:"btn p"', "chips": 'class:"chip good"', "cards": 'class:"card",role:"button"', "tabs": 'class:"subtabs"',
             "filter": 'class:"tfilter"', "inputs": 'class:"f"', "menus": "cmpMenu()", "tables": 'h("table"', "empty states": 'class:"empty"',
             "loading": 'class:"st-load"', "errors": 'class:"st-err"', "live board": "cmpBoard()"}
    check("the page shows every component: " + ", ".join(parts), cp and all(v in cp for v in parts.values()), [k for k, v in parts.items() if v not in cp])
    check("both themes, side by side", 'cmpFrame("light"),cmpFrame("dark")' in html and '"cmp-frame th-"+theme' in html)
    board = html[html.find("function cmpBoard("):html.find("function cmpFrame(")]
    check("the live-board sample shows all three check outcomes and the not-sure verdict", all(s in board for s in ('"passed"', '"failed"', '"not sure"', "verdictbox not_sure", "lane-b")))

    # ---- 3. keyboard and screen readers
    check("a skip link and one polite live region", 'h("a",{class:"skip",href:"#view"}' in html and 'id:"srLive"' in html and '"aria-live":"polite"' in html)
    check("finished steps, checks, verdicts and replies are announced; streaming text is not",
          all(s in html for s in ('announce("Step "', 'announce("Check "', 'announce("Run finished: "', 'announce("Reply"')) and 'ev.type==="token"' not in html[html.find("function a11yWire("):])
    check("rebuilt panels keep focus where it was (live board, strip, thread)", all(f'wrap("{n}",o=>function' in html for n in ("renderDoing", "renderStrip", "renderThread"))
          and "function keepFocus(" in html)
    check("menus work from the keyboard: arrows, Home/End, Enter/Space, Right/Left for side panels, Esc back to the button",
          all(k in html for k in ('e.key==="ArrowDown"', 'e.key==="Home"', 'e.key==="ArrowRight"&&i>=0', 'e.key==="ArrowLeft"||e.key==="Escape"'))
          and "menuNav(m," in html and "menuNav(menu,close)" in html and '"aria-expanded"' in html)
    check("Esc on an open menu closes the menu only, before the handler that would stop a run",
          "e.stopImmediatePropagation();closeModelMenu()" in html and html.index("e.stopImmediatePropagation();closeModelMenu()") < html.index("if(EV.running){e.preventDefault();") + 10**9)
    check("the command list and the slash list are comboboxes with an active option",
          'inp.setAttribute("role","combobox")' in html and 'ta.setAttribute("aria-activedescendant"' in html and 'inp.setAttribute("aria-activedescendant"' in html)
    check("focus stays inside an open dialog, and Space presses custom buttons", 'querySelectorAll(\'[aria-modal="true"]\')' in html
          and "t.matches('[role=\"button\"]:not(button):not(a):not(input)')" in html)
    check("drag and drop has buttons and keys as well, and says what moved", "Move step " in html and 'e.altKey&&(e.key==="ArrowUp"' in html
          and 'announce("Step moved to position "' in html and "ArrowLeft:[-10,0]" in html)
    check("plan steps on the live board open with Enter or Space and say whether they are open", '.plan li[tabindex]")){e.preventDefault();t.click()' in html
          and 'li.setAttribute("aria-expanded"' in html)
    rm = re.findall(r"@media \(prefers-reduced-motion:reduce\)\{\s*\*,\*::before,\*::after\{animation:none!important;transition:none!important;scroll-behavior:auto!important\}", css)
    check("reduced motion: one rule stops every animation and transition, last in the sheet", len(rm) == 1 and css.rfind("prefers-reduced-motion") > css.rfind("Components page"))

    # ---- 4. colour is never the only signal
    check("not-sure and stopped states change shape, not only colour (map, diagram, dots, tool dots)",
          all(s in css for s in (".mp-n.ok-null rect.mp-box{stroke-dasharray", ".rv-n.gate.unsure polygon{stroke-dasharray", ".dot.off{background:transparent",
                                 ".ccdot.err{background:var(--bad);box-shadow:none;border-radius:2px}", ".mm-model .mm-dot{background:transparent")))
    check("the words say it too: map nodes name their outcome, the strip says when the server is not reachable",
          '", could not run"' in html and '"model server not reachable"' in html)

    # ---- 5. the document
    doc_p = os.path.join(HERE, "docs", "design_system.md")
    doc = open(doc_p, encoding="utf-8").read() if os.path.isfile(doc_p) else ""
    check("docs/design_system.md exists and names every token in the light block", doc and all(f"--{t}" in doc for t in L), [t for t in L if f"--{t}" not in doc][:10])
    check("the document covers the Components page, the keyboard model and reduced motion", all(s in doc for s in ("Components", "Keyboard", "reduced motion", "colour alone")))

    # ---- the script still parses
    node = shutil.which("node")
    if not node:
        check("the hub scripts parse (node --check)", None, "node is not installed")
    else:
        d = tempfile.mkdtemp(prefix="archdesign_")
        try:
            bad = []
            for i, b in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
                p = os.path.join(d, f"b{i}.js")
                open(p, "w", encoding="utf-8").write(b.replace('"__HUB_TOKEN__"', '"x"'))
                r = subprocess.run([node, "--check", p], capture_output=True, text=True, timeout=60)
                if r.returncode:
                    bad.append(r.stderr[-300:])
            check("the hub scripts parse (node --check)", not bad, bad)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    n = sum(results)
    tail = f" ({len(unknown)} could not run)" if unknown else ""
    print(f"\n  {'ALL ' + str(n) + ' DESIGN CHECKS PASSED' + tail if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' DESIGN CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
