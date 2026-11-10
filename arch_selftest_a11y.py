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
    # Phase A (2026-11-09): the progress card is rebuilt at every step, so it is a plain region and ONE sentence is spoken per change
    check("progress is announced once per change (current step, then the outcome), not re-read at every redraw",
          'id:"spProgress",role:"region","aria-label":"Progress"' in ui and 'simpleDraw._said!==EV.epoch+"|"+say' in ui
          and '"It worked. Every check passed."' in ui)
    check("panels rebuilt on a timer are not live regions (run bar, queue board); the end of a run is spoken once",
          'id="runBar" class="runbar" hidden role="region"' in ui and 'h("div",{id:"qBoard"})' in ui
          and 'announce("Run finished: "' in ui)
    check("toasts are heard, also inside an open window: live regions there, errors assertive, no role on the visual toast",
          "function srRegion(kind)" in ui and 'if(cls==="err")srAlert(m);else announce(m);' in ui
          and 't.removeAttribute("role")' in ui and 'wrap("toast"' not in ui and "const el=srRegion(\"polite\")" in ui)
    check("form fields have their own border token (3:1), in every theme",
          ui.count("--field-line:#") >= 5 and "border:1px solid var(--field-line);" in ui)
    check("focus is kept when a panel is rebuilt after a fetch (background jobs, queue, status chip) and lands on the heading after a page change",
          "if(r&&typeof r.then===\"function\")return r.then(" in ui
          and 'function loadBgJobs(){return keepFocus($("#bgList")' in ui and 'function loadQueue(){return keepFocus($("#qBoard")' in ui
          and 'function liveChip(){return keepFocus($("#liveChip")' in ui
          and "function focusPageHeading()" in ui and ui.count("focusPageHeading()") >= 3)
    check("the browser tab names the page; there is exactly one skip link",
          'document.title=(ttl?ttl+" · ":"")+"Architect Hub"' in ui and 'if(!$(".skip-link,.skip"))' in ui
          and ui.count('class="skip-link"') == 1)
    check("the status chips and the Try: examples are buttons, not clickable spans or links",
          '<button type="button" id="sJob" class="chip" hidden>' in ui and '<button type="button" id="sOllama" class="chip"' in ui
          and 'h("button",{type:"button",class:"chip acc","aria-label":"Use this example: "+t' in ui)
    check("text the audit measured below 4.5:1 is fixed at its rule (filter counts, terminal diff, kbd, plan to-dos, queue note, y/n/a/q)",
          ".tfilter .n{font-variant-numeric:tabular-nums;opacity" not in ui and ".term .dh{color:var(--term-hunk)}" in ui and ui.count("--term-hunk:") >= 5
          and "ul.plan li.todo{color:var(--faint)}" in ui and 'class:"qempty"' in ui
          and 'b.disabled=!j.running;   /* answers only' in ui and '$("#inRow").style.opacity' not in ui)
    check("the GPU chip, rebuilt every 10 s, is not a live region",
          '<span id="liveChip" class="chip livechip" hidden>' in ui)
    # Phase B (2026-11-10)
    check("single-key shortcuts can be turned off (2.1.4), from the shortcut list and from Settings; Space on Home scrolls",
          "function singleKeys()" in ui and 'if(!singleKeys()&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&e.key.length===1)' in ui
          and ui.count("singleKeysSwitch()") >= 3 and '||!singleKeys())return;' in ui and 'e.key!=="?"&&e.key!==" "' in ui)
    check("every page keeps one h1: Home and Watch a run hide the title row visually, not from the outline; focus lands on a heading you can see",
          ".sheet-wrap .top,body.oneScreen .top{position:absolute" in ui and ".sheet-wrap .top{display:none}" not in ui
          and "body.oneScreen .top{display:none}" not in ui and "const vis=e=>e&&e.getClientRects().length" in ui)
    check("Markdown inside a page starts at h3, so a document's own title is never a second h1",
          'h("h"+(m[1].length+2),{class:"md-h"+m[1].length}' in ui)
    check("a Settings field is named by its label only; the hint and recommendation describe it",
          'i.setAttribute("aria-labelledby",lid)' in ui and 'i.setAttribute("aria-describedby",[recEl?lid+"_r":"",hint?lid+"_h":"",lid+"_e"]' in ui)
    check("a refused Settings save says which field and why, next to it, and stays (no fading toast only); focus goes there",
          "function fieldErr(path,msg)" in ui and 'id:"cfgErr",hidden:true' in ui and 'note.textContent="Not saved: "+msg' in ui
          and 'srAlert("Settings not saved. "+msg)' in ui and 'c.focus({preventScroll:true});}}' in ui)
    vl = re.findall(r'"data-vlabel":"([^"]+)"', ui)
    check("fields built without a label get a visible one (data-vlabel -> label for, wrapped in place); 21 fields carry one (23 with Review and the backlog)",
          'h("label",{class:"vlbl",for:i.id},i.dataset.vlabel)' in ui and len(vl) >= 23
          and not re.search(r'h\("(?:input|select|textarea)",\{[^{}]*"aria-label":"(?:Flow|Rounds|Time span|Time window|Search the gallery|Plugin folder or zip)"', ui))
    check("reflow: sub-tabs wrap on wide screens, the top bar wraps, card grids never need more than the screen",
          ".subtabs{overflow:visible;flex-wrap:wrap" in ui and ".topbar{display:flex;flex-wrap:wrap" in ui
          and "minmax(min(340px,100%),1fr)" in ui)
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
