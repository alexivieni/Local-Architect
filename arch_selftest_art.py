"""Self-tests for the hub's art direction (2026-11-04, docs/art_direction.md): the two curated themes (Midnight, Paper) complete and
at AA contrast with every accent, the section hues, one consistent icon set covering every place and page, one small theme-aware
drawing per page, the theme picker, and the signs of life (busy pulse, shimmer, count-ups) honouring reduced motion and hidden tabs.

    python arch_selftest_art.py

Reads hub_ui.html and docs/art_direction.md only; nothing is started and nothing is written. The script syntax check needs `node`;
without it that check reports COULD NOT RUN (not a pass, not a fail).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")


def _decls(block):
    return dict((k, v.strip()) for k, v in re.findall(r"--([a-z0-9-]+):([^;}]+)", block))


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


def _resolve(tokens, name, depth=0):
    v = tokens.get(name, "")
    m = re.fullmatch(r"var\(--([a-z0-9-]+)\)", v)
    if m and depth < 5:
        return _resolve(tokens, m.group(1), depth + 1)
    return v if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", v) else None


def _obj(js, name):
    m = re.search(r"const " + name + r"=\{(.*?)\};", js, re.S)
    return dict(re.findall(r"(\w+):\"([\w-]+)\"", m.group(1))) if m else {}


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
    tokens = css[:end]
    light = _decls(re.search(r"\.th-light,:root\{(.*?)\}", tokens, re.S).group(1))
    dark = _decls(re.search(r"\.th-dark,:root\[data-theme=dark\]\{(.*?)\}", tokens, re.S).group(1))
    mid_m = re.search(r"\.th-midnight,:root\[data-theme=midnight\]\{(.*?)\}", tokens, re.S)
    pap_m = re.search(r"\.th-paper,:root\[data-theme=paper\]\{(.*?)\}", tokens, re.S)
    mid = _decls(mid_m.group(1)) if mid_m else {}
    pap = _decls(pap_m.group(1)) if pap_m else {}
    derived = re.search(r"\.th-light,\.th-dark,\.th-midnight,\.th-paper,:root\{(.*?)\}", tokens, re.S)
    der = _decls(derived.group(1)) if derived else {}

    # ---- 1. the two curated themes
    check("Midnight and Paper are token blocks in the token section, each scoped to a subtree as well (.th-midnight / .th-paper)",
          bool(mid and pap) and "color-scheme:dark" in mid_m.group(0) and "color-scheme:light" in pap_m.group(0))
    check("each sets every token the dark theme sets (so the computer's dark setting can never leak into Paper)",
          set(dark) <= set(mid) and set(dark) <= set(pap), sorted((set(dark) - set(mid)) | (set(dark) - set(pap))))
    check("every token they set exists in the light block (no new names hiding in a theme)", set(mid) <= set(light) and set(pap) <= set(light),
          sorted((set(mid) | set(pap)) - set(light)))
    check("the derived tokens (ring, marker, section hues) are computed in every theme scope", bool(der) and all(
        k in der for k in ("grad", "ring", "hue-projects", "hue-workspace", "hue-models", "hue-settings", "hue")))

    # accents per theme: midnight shares the dark accents, paper has its own light ones that outrank the system-dark ones
    acc = {}
    for a in ("graphite", "copper", "moss"):
        md = re.search(rf":root\[data-theme=midnight\]\[data-accent={a}\],:root\[data-accent={a}\] \.th-midnight,:root\[data-theme=dark\]\[data-accent={a}\]"
                       rf",:root\[data-accent={a}\] \.th-dark\{{([^}}]*)\}}", tokens)
        pp = re.search(rf":root\[data-theme=paper\]\[data-accent={a}\],:root\[data-accent={a}\] \.th-paper\{{([^}}]*)\}}", tokens)
        acc[a] = (_decls(md.group(1)) if md else None, _decls(pp.group(1)) if pp else None, pp.start() if pp else -1)
    sysdark_acc = tokens.find(":root:not([data-theme=light])[data-accent=moss]")
    # an accent never moves a status colour (2026-11-08): --info stays the theme's own, so Paper's accents set --acc but not --info
    check("Midnight takes the dark accents, Paper has its own (and they come after the system-dark accents, so they win)",
          all(m and p and "acc" in p and "info" not in p and "info" not in m and at > sysdark_acc > 0 for m, p, at in acc.values()))

    # ---- 2. contrast, every theme x every accent (WCAG AA 4.5:1 for text, 3:1 for the hue marks)
    def theme_tokens(base, over):
        t = dict(light)
        t.update(base)
        t.update(over or {})
        t.update({k: v for k, v in der.items()})
        return t

    sets = {"light": theme_tokens({}, None), "dark": theme_tokens(dark, None), "midnight": theme_tokens(mid, None), "paper": theme_tokens(pap, None)}
    for a, (md, pp, _) in acc.items():
        sets[f"midnight+{a}"] = theme_tokens(mid, md)
        sets[f"paper+{a}"] = theme_tokens(pap, pp)
    text_pairs = [("ink", "panel"), ("ink", "bg"), ("mute", "panel"), ("faint", "panel"), ("faint", "bg"), ("acc", "panel"), ("on-acc", "acc"),
                  ("acc", "acc2"), ("good", "goodbg"), ("warn", "warnbg"), ("bad", "badbg"), ("info", "infobg"), ("infra", "infrabg"),
                  ("claude-ink", "panel"), ("term-ink", "term")]
    bad = []
    for name, t in sets.items():
        if not name.startswith(("midnight", "paper")):
            continue
        for fg, bg in text_pairs:
            a, b = _resolve(t, fg), _resolve(t, bg)
            if not (a and b) or contrast(a, b) < 4.5:
                bad.append(f"{name}: --{fg} on --{bg} = {contrast(a, b):.2f}" if a and b else f"{name}: --{fg}/--{bg} unresolved")
    check("AA contrast (4.5:1) in Midnight and Paper with every accent: text, muted, labels, chips, primary button, terminal", not bad, bad[:6])
    hue_bad = []
    for name, t in sets.items():
        for hname in ("hue-projects", "hue-workspace", "hue-models", "hue-settings"):
            a, b = _resolve(t, hname), _resolve(t, "panel")
            if not (a and b) or contrast(a, b) < 3:
                hue_bad.append(f"{name}: --{hname} = {contrast(a, b):.2f}" if a and b else f"{name}: --{hname} unresolved")
    check("every section hue reads at 3:1 or more on a card, in all four themes and every accent (icons and markers are non-text)", not hue_bad, hue_bad[:6])

    # ---- 3. the section hues are used sparingly, from tokens only
    at = css.find("/* ===== art direction (docs/art_direction.md)")      # the section markers, not a mention in a header comment
    stop = css.find("/* ===== Components page (Settings > Components)")
    layer = css[at:stop] if 0 < at < stop else ""
    check("the art layer sits after the workspace section and before the Components page", bool(layer) and at > css.find("the agents' workspace"))
    check("it holds no literal colour (hex or rgba)", layer and not HEX.findall(layer), HEX.findall(layer)[:5])
    check("one hue per place, given to the body and the side tab", all(f"body[data-sec={s}],.nav[data-sec={s}]{{--hue:var(--hue-{n})}}" in layer
          for s, n in (("home", "projects"), ("console", "workspace"), ("team", "models"), ("settings", "settings"))))
    check("the hue only tints icons and the active marker / underline",
          ".nav .ic{color:var(--hue)}" in layer and ".nav[aria-current=page]::before{background:var(--hue)}" in layer
          and ".subtabs button[aria-current=page]::after" in layer and ".card h2>.ic.hic" in layer)

    # ---- 4. one icon set
    symbols = dict(re.findall(r'<symbol id="i-([a-z0-9-]+)" viewBox="([^"]+)"', html))
    sym_bodies = re.findall(r'<symbol id="i-[a-z0-9-]+"[^>]*>(.*?)</symbol>', html, re.S)
    first = re.search(r'<script>\r?\n"use strict";', html)
    js = html[first.start() if first else 0:].replace("\r\n", "\n")
    import arch_uireg                      # the page registry read once from hub_ui.html (also GET /api/arch/ui/registry)
    reg = arch_uireg.load(os.path.join(HERE, "hub_ui.html"))
    page_icon = dict(reg["page_icon"])     # PAGE_ICON is derived from the registered pages: id -> icon
    tabs = [(p["id"], p["icon"]) for p in reg["places"]]
    pages = list(reg["page_ids"]) + ["components"]
    head_icons = re.findall(r',"([\w-]+)"\]', re.search(r"const HEAD_ICON=\[(.*?)\];", js, re.S).group(1)) if "const HEAD_ICON=[" in js else []
    check("one grid and one stroke: every symbol is 24 x 24 and draws with the inherited stroke (no colours, no stroke widths of its own)",
          all(v == "0 0 24 24" for v in symbols.values()) and not any(HEX.search(b) or "stroke-width" in b or re.search(r'stroke="(?!none")', b) for b in sym_bodies))
    check("every page has its own icon, and it exists in the sprite", all(p in page_icon for p in pages) and all(v in symbols for v in page_icon.values()),
          [p for p in pages if p not in page_icon] + [v for v in page_icon.values() if v not in symbols])
    check("every place (tab) and every card-header rule names an icon that exists", tabs and head_icons and all(i in symbols for _, i in tabs) and all(i in symbols for i in head_icons),
          [i for _, i in tabs if i not in symbols] + [i for i in head_icons if i not in symbols])
    check("sub-tabs, the page glyph, card headers and the command list carry the icons",
          "b.prepend(icon(PAGE_ICON[cur[3][i]]" in js and 'id:"pageGlyph"' in js and 'icon(hit?hit[1]:(PAGE_ICON[page]||"dot"),"hic")' in js
          and "ic:PAGE_ICON[p[0]]||tabOf(p[0])[1]" in js)
    check("card-header icons stay out of text a model or a program wrote, and never change the heading's words",
          "const ART_SKIP=" in js and ".msgs" in js[js.find("const ART_SKIP="):js.find("const ART_SKIP=") + 200] and "el.prepend(icon(" in js)

    # ---- 5. one drawing per page
    art_m = re.search(r"const ART=\{(.*?)\n\};", js, re.S)
    arts = dict(re.findall(r"\n  (\w+):'(.*?)',", art_m.group(1) + "\n") if art_m else [])
    wanted = {"console": "console", "run": "task", "studio": "studio", "agents": "crew", "fleet": "fleet", "map": "map", "monitor": "monitor",
              "logbook": "logbook", "library": "library", "blueprints": "blueprints", "queue": "queue", "tiers": "models", "settings": "settings"}
    page_art, sec_art = _obj(js, "PAGE_ART"), _obj(js, "SEC_ART")
    check("a drawing for Console, New task, Studio, Crew, Fleet, Map, Monitor, Logbook, Library, Blueprints, Queue, Models, Settings and the first run",
          all(page_art.get(p, sec_art.get("settings") if p == "settings" else None) == a for p, a in wanted.items()) and "journey" in arts
          and all(a in arts for a in wanted.values()), [p for p, a in wanted.items() if a not in arts])
    tab_of = {p: t for t, _ in tabs for p in re.findall(r'"(\w+)"', re.search(r'\["' + t + r'","[\w-]+","[^"]+",\[(.*?)\]\]', js).group(1))}
    check("every page resolves to a drawing (its own, or its place's)", all((page_art.get(p) or sec_art.get(tab_of.get(p, "settings"))) in arts for p in pages))
    allowed = {"a-pl", "a-ln", "a-hi", "a-fl", "a-sf", "a-tn", "a-dt"}
    used_cls = {c for a in arts.values() for c in re.findall(r'class="([\w-]+)"', a)}
    check("drawings are small, inline and theme-aware: under 1.5 KB each, no colour, no image, no link, only the art classes",
          arts and all(len(a) < 1500 and not HEX.search(a) and "<image" not in a and "href" not in a and "url(" not in a for a in arts.values()) and used_cls <= allowed,
          sorted(used_cls - allowed) or max((len(a), n) for n, a in arts.items()))
    check("the art classes draw with currentColor, the hue and the theme's surfaces only",
          all(s in layer for s in (".art .a-ln{fill:none;stroke:currentColor", ".art .a-hi{fill:none;stroke:var(--hue)", ".art .a-fl{fill:var(--panel)", ".art .a-dt{fill:var(--hue)")))
    check("the drawings appear in the page heading, in empty states, on Get started and in the first-visit tour",
          'a.id="pageArt"' in js and "el.prepend(art(artFor(page)))" in js and 'art("journey","gs-art")' in js and '"fv-art")' in js)
    check("empty states keep their sentence: a small inline 'none yet' is left alone, and nothing is drawn where a page drew its own",
          'el.matches("li,td,span")||el.querySelector("svg")' in js and "el.offsetWidth<260" in js)

    # ---- 6. the theme picker
    check("five looks: Auto, Light, Dark, Midnight, Paper", 'const THEMES=["system","light","dark","midnight","paper"];' in js
          and all(f"{t}:" in js[js.find("const THEME_LABEL="):js.find("const THEME_LABEL=") + 160] for t in ("midnight", "paper")))
    check("the picker is a menu of radio rows, one keyboard model (menuNav), focus on the chosen look, Esc back to the button",
          'role:"menu","aria-label":"Appearance"' in js and 'role:"menuitemradio","aria-checked":String(t===cur)' in js and "menuNav(m,back=>closeThemePick(back))" in js
          and '$("#themeBtn").setAttribute("aria-haspopup","menu")' in js)
    check("each look is previewed in its own tokens (.th-<name>), and an unknown stored value falls back to Auto",
          'h("span",{class:"thsw th-"+t' in js and 'if(!THEMES.includes(t))t="system";' in js)
    check("Esc closes the picker before it could ever stop a run", js.index("if(THPICK){e.preventDefault();closeThemePick(true);return;}") < js.index("if(EV.running){e.preventDefault();"))

    # ---- 7. signs of life, calm
    sec = js[js.find("Art direction (docs/art_direction.md). Display only"):js.find('go("overview");refresh(true)')]
    check("the Workspace tab breathes while a run or a job works (the side chip says it in words)",
          "body.is-busy .nav[data-sec=console]::after" in layer and 'document.body.classList.toggle("is-busy"' in sec and 'attributeFilter:["hidden","class"]' in sec)
    check("loading blocks shimmer, from tokens", ".card>div.st-load::after" in layer and "animation:shimmer" in layer)
    check("figures count up only when they change, and never under reduced motion or in a hidden tab",
          "prefers-reduced-motion: reduce" in sec and "document.hidden" in sec and "if(from===to||!isFinite(to)||artStill())return;" in sec
          and "el.textContent=txt;COUNTING.delete(el)" in sec)
    check("a hidden tab pauses every animation, and the art layer starts no timer of its own",
          ".tab-away *,.tab-away *::before,.tab-away *::after{animation-play-state:paused!important}" in layer and '"visibilitychange"' in sec
          and "setInterval" not in sec and "setTimeout" not in sec)
    rm = re.findall(r"@media \(prefers-reduced-motion:reduce\)\{\s*\*,\*::before,\*::after\{animation:none!important;transition:none!important", css)
    check("the one reduced-motion rule still comes after the art layer, so it stops everything the layer animates",
          len(rm) == 1 and css.rfind("prefers-reduced-motion") > stop > at)

    # ---- 8. the document
    doc_p = os.path.join(HERE, "docs", "art_direction.md")
    doc = open(doc_p, encoding="utf-8").read() if os.path.isfile(doc_p) else ""
    need = ["Midnight", "Paper", "--hue-projects", "--hue-workspace", "--hue-models", "--hue-settings", "reduced motion", "By page"] + [f"`{a}`" for a in arts]
    check("docs/art_direction.md records the system: themes, hues, every drawing, motion, and the changes by page", doc and all(s in doc for s in need),
          [s for s in need if s not in doc][:8])

    # ---- the scripts still parse
    node = shutil.which("node")
    if not node:
        check("the hub scripts parse (node --check)", None, "node is not installed")
    else:
        d = tempfile.mkdtemp(prefix="archart_")
        try:
            errs = []
            for i, b in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
                p = os.path.join(d, f"b{i}.js")
                open(p, "w", encoding="utf-8").write(b)
                r = subprocess.run([node, "--check", p], capture_output=True, text=True, timeout=60)
                if r.returncode:
                    errs.append(r.stderr[-300:])
            check("the hub scripts parse (node --check)", not errs, errs)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    n = sum(results)
    tail = f" ({len(unknown)} could not run)" if unknown else ""
    print(f"\n  {'ALL ' + str(n) + ' ART CHECKS PASSED' + tail if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' ART CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
