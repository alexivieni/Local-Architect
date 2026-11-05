"""Self-test for Simple mode and the accessibility layer in hub_ui.html (static checks on the page source).

    python arch_selftest_a11y.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run():
    ui = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
    results = []

    def check(name, ok):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    check("a skip link to the main content is the first thing in the page", re.search(r"<body[^>]*><a class=\"skip-link\" href=\"#view\">", ui))
    check("every focusable thing shows a visible focus ring", ":focus-visible{outline:3px solid var(--acc)" in ui)
    check("controls are at least 36 px tall (easy to hit)", "button,select,input,textarea,summary{min-height:36px}" in ui)
    check("a screen-reader-only class exists for words that colour alone would carry", ".sr-only{" in ui)
    check("progress is announced to screen readers (live region)", 'id:"spProgress",role:"status","aria-live":"polite"' in ui)
    check("each Simple-mode step says its state in words, not only with a colour or symbol", 'h("span",{class:"sr-only"}," — "+label[st])' in ui and '"aria-hidden":"true"' in ui)
    check("icon-only and arrow buttons get spoken names after every render", "function a11yPass(" in ui and '"↑":"Move up","↓":"Move down"' in ui and "MutationObserver" in ui)
    check("clickable non-buttons become keyboard-reachable and answer Enter/Space", 'setAttribute("tabindex","0")' in ui and 'e.key==="Enter"||e.key===" "' in ui)
    check("Simple mode is a real page with plain-language step names", '["simple","spark","Simple"' in ui and "const PLAIN_STEP=" in ui and "Writing the change" in ui)
    check("Simple mode is the default and Advanced is one click away (remembered)", 'store.get("uiMode","simple")' in ui and 'setMode("advanced")' in ui)
    check("the main box and the folder field have accessible names", '"aria-label":"What do you want done?"' in ui and '"aria-label":"Project folder"' in ui)
    check("starter recipes are real buttons in a labelled group", 'role:"group","aria-label":"Start from an example"' in ui and 'class:"sp-recipe",type:"button"' in ui)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' A11Y CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' A11Y CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
