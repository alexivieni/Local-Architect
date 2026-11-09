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
    # a clickable table row is reached through clickRow() (Tab, Enter/Space, aria-current). The Cost map's row, inside its guarded
    # slice, moved onto it with the owner's yes (2026-11-09), so the rule now holds for the whole page.
    raw_rows = re.findall(r'h\("tr",\{[^{}]*onclick', ui)
    check("every clickable table row on the page is reachable by keyboard (clickRow: Tab, Enter or Space), the Cost map's too",
          "function clickRow(fn,current)" in ui and 'h("tr",clickRow(()=>{CM.flow=b.f;cmDraw();},CM.flow===b.f)' in ui and not raw_rows)
    # 2026-11-05 one-box Home (docs/telemetry/ui_rebuild_log.md 26): the old Simple page is Home ("overview"); old ids still land there.
    check("Home (the old Simple mode) is a real page with plain-language step names",
          '["overview","home","Start"' in ui and 'simple:"overview"' in ui and "const PLAIN_STEP=" in ui and "Writing the change" in ui)
    check("Home is the default and Advanced (Console) is one click away",
          'if(!PAGES.some(x=>x[0]===id))id="overview"' in ui and 'function setMode(m){go(m==="simple"?"overview":"console");}' in ui)
    check("the main box and the folder field have accessible names (a visible label tied to each)",
          'h("label",{class:"sp-lbl",for:"spGoal"},"What do you want done?")' in ui and 'id:"spGoal"' in ui
          and 'for:"spRoot"},"Project folder' in ui and 'id:"spRoot",role:"combobox"' in ui)
    check("starter recipes are real buttons in a labelled group",
          'role:"group","aria-labelledby":"spTryL"' in ui and 'id:"spTryL"},"Try:"' in ui and 'class:"sp-recipe",type:"button"' in ui)
    # 2026-11-05 evening: the sleep-time path (data night, train the worker, morning card) must be clear, large and announced
    icon_only = re.findall(r'h\("button",\{[^{}]*class:"btn (?:p|pri)(?: [a-z-]+)*"[^{}]*\},icon\("[a-z-]+"\)\)', ui)
    check("no primary button is icon-only: every one carries a visible text label", not icon_only)
    btn_block = ui[ui.find("/* ===== BUTTONS: the one block"):ui.find("/* ===== end of BUTTONS")]
    check("primary actions are at least 44 px tall (a small one in a row of small buttons stays small), the data night buttons 48 px",
          "min-height:44px" in btn_block.split(".btn.p,.btn.pri{", 1)[-1].split("}", 1)[0] and ".btn.danger.dn-big{min-height:44px}" in ui
          and ".dn-big{min-height:48px" in ui)
    check("Start data night and Stop safely are real, labelled buttons; Stop safely shows only while it runs and focus moves to it on start",
          'id:"dnStart",type:"button"' in ui and 'id:"dnStop",type:"button",hidden:true' in ui and 'const s=$("#dnStop"); if(s)s.focus();' in ui and "Stopping after this task..." in ui)
    check("data night state changes are announced (started, stopping, finished) through live regions, not colour alone",
          'id:"dnLive",role:"status","aria-live":"assertive"' in ui and "Data night started." in ui and "Stopping safely." in ui and "Data night finished." in ui
          and 'class:"dn-preview",role:"status","aria-live":"polite"' in ui and 'class:"dn-run",id:"dnRun",role:"status","aria-live":"polite"' in ui)
    check("every data night field has a visible label; the switches have text; the time is a real time input",
          'dnField("End time"' in ui and 'dnField("Worker model"' in ui and "Teacher model on" in ui and "Curriculum on" in ui and 'id:"dnUntil",type:"time"' in ui)
    check("Train the worker says why it is disabled in visible text tied to the button (aria-describedby), announced politely",
          '"aria-describedby":"twWhy"' in ui and 'id:"twWhy",role:"status","aria-live":"polite"' in ui)
    check("the morning card is a labelled section; the lessons table has a caption, column scopes and a keyboard-reachable labelled region",
          'class:"card night","aria-labelledby":"nightT"' in ui and 'h("caption",{class:"sr-only"}' in ui and '{scope:"col"}' in ui
          and 'role:"region","aria-label":"Lessons table",tabindex:"0"' in ui)
    check("Settings: Advanced is a native details/summary (keyboard and screen reader operable) at least 44 px tall",
          'h("summary",{class:"more-sum"},"Advanced:' in ui and "details.adv>summary{min-height:44px" in ui)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' A11Y CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' A11Y CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
