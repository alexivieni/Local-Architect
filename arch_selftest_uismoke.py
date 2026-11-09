"""Self-test: the hub works in a real browser, end to end, the way a person uses it.

A throwaway hub (its own home and data folder, server.kind = ollama) talks to a scripted stand-in model. A real Edge/Chrome, headless
on 127.0.0.1 (arch_browser), opens Home, types a project folder and a bug report, presses Do it, and must see: the progress box fill
("Working on it"), the verdict ("It worked"), the run in Recent runs, then Undo restores the file and removes the generated test.
No uncaught page error may happen on the way.

This is the check the text-matching hub checks could not be: the 2026-11-08 Home bug ("Working on it" never filled unless the
Console was open) passed all 220 of them. No Chromium browser installed -> every check here is "could not run" (NOT_SURE), never a pass.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PAGER = '"""Tiny pagination helper."""\n\n\ndef paginate(items, page, size):\n    """Items on 1-based page `page`."""\n    start = page * size\n    return items[start:start + size]\n'
TESTS = ("from pager import paginate\n\n\ndef test_first_page():\n    assert paginate(list(range(10)), 1, 3) == [0, 1, 2]\n\n\n"
         "def test_last_partial_page():\n    assert paginate(list(range(10)), 4, 3) == [9]\n")
REPRO = "from pager import paginate\n\n\ndef test_repro_page_one():\n    assert paginate(list(range(10)), 1, 3) == [0, 1, 2]\n"
FIX = "EDIT: pager.py\n<<<<<<< SEARCH\n    start = page * size\n=======\n    start = (page - 1) * size\n>>>>>>> REPLACE\n"
A11Y_AUDIT = r"""(()=>{const v=document.getElementById('view'),bad=[];
 v.querySelectorAll('button:not([aria-label])').forEach(b=>{const t=(b.textContent||'').trim();if(!t||/^[↑↓←→×✕⋯…▾▸›‹+−?]$/.test(t))bad.push('button '+t)});
 v.querySelectorAll('input:not([aria-label]):not([aria-labelledby]):not([type=hidden]),textarea:not([aria-label]):not([aria-labelledby]),select:not([aria-label]):not([aria-labelledby])').forEach(i=>{if(!i.closest('label')&&!(i.id&&document.querySelector('label[for="'+i.id+'"]')))bad.push('field '+(i.placeholder||i.name||i.tagName))});
 v.querySelectorAll('div,section,pre').forEach(el=>{if(el.hasAttribute('tabindex')||el.scrollWidth<=el.clientWidth+1||el.clientWidth===0)return;const ox=getComputedStyle(el).overflowX;if(ox!=='auto'&&ox!=='scroll')return;if(el.querySelector('a[href],button,input,select,textarea,[tabindex]'))return;bad.push('scroll '+el.className)});
 v.querySelectorAll('[onclick]:not(button):not(a):not(input):not([tabindex])').forEach(el=>bad.push('click '+el.tagName));return bad})()"""   # what the scoped a11y pass must leave behind: nothing unnamed or unreachable
GOAL = "paginate() returns the wrong rows: page 1 should give the first size items (pages are 1-based). Fix it so the tests pass."


def run():
    import arch_core  # noqa: F401  (a standalone run gets a throwaway home first)
    import arch_browser
    import arch_selftest as base
    from arch_selftest_pro import fence

    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    planned = ["the hub opens in a real browser with no page error", "Home takes a typed folder and goal", "Do it starts the run",
               "Home shows 'Working on it' while it runs", "Home shows the verdict 'It worked'", "the run's events were pushed over one stream", "the page asks for the live half of /api/state", "an unchanged progress box is not rebuilt by the timer", "the fix is on disk and the tests pass",
               "the run is listed in Recent runs", "Undo restores the file and removes the generated test",
               "Home stops polling once the run is over", "the one dialog: focus in, Esc and backdrop close", "a clickable row opens with Enter", "the accessibility pass left nothing behind", "no uncaught page error"]
    if not arch_browser.find_browser():
        for p in planned:
            print(f"  NOT RUN  {p} (no Edge or Chrome installed here)")
        print(f"\n  0 of {len(planned)} UI SMOKE CHECKS FAILED, {len(planned)} COULD NOT RUN (not a pass)")
        return 2

    class Model(base.Fake):
        def script(self, role, user, full, st):
            if "Write ONE focused pytest test that reproduces this bug" in user:
                return fence("tests/test_repro_bug.py", REPRO)
            if "Fix this bug with the smallest correct change" in user:
                return FIX
            return "- no concerns"

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="archuismoke_")
    home, data, proj = (os.path.join(tmp, x) for x in ("home", "data", "proj"))
    for d in (home, data, os.path.join(proj, "tests")):
        os.makedirs(d)
    with open(os.path.join(home, "architect_config.json"), "w", encoding="utf-8") as f:
        json.dump({"default_model": "fake:7b", "server": {"kind": "ollama"}, "behavior": {"record_live": "off", "close_on_exit": "off"}}, f)
    for rel, text in (("pager.py", PAGER), ("tests/test_pager.py", TESTS)):
        with open(os.path.join(proj, rel), "w", encoding="utf-8") as f:
            f.write(text)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, ARCHITECT_HOME=home, ARCHITECT_DATA_DIR=data, OLLAMA_HOST=f"127.0.0.1:{fake.server_address[1]}",
               ARCHITECT_NO_CACHE="1", PYTHONUNBUFFERED="1")
    hub = subprocess.Popen([sys.executable, os.path.join(HERE, "architect_hub.py"), "--no-browser", "--port", str(port)], cwd=HERE, env=env,
                           stdout=open(os.path.join(tmp, "hub.log"), "wb"), stderr=subprocess.STDOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0)
    job = None
    try:
        import arch_sandbox
        job = arch_sandbox.attach(hub, 8192)              # the hub's run children go with it
    except Exception:
        job = None
    b = None
    try:
        import urllib.request
        base_url = f"http://127.0.0.1:{port}/"
        for _ in range(80):
            try:
                urllib.request.urlopen(base_url, timeout=2).read()
                break
            except OSError:
                time.sleep(0.25)
        b = arch_browser.Browser.launch()
        b.navigate(base_url)
        up = b.wait_for("typeof simpleStart === 'function' && document.querySelector('#spGoal') && document.querySelector('#spRoot')", 30)
        check("the hub opens in a real browser with no page error", up and not b.exceptions, b.exceptions)
        b.wait_for("performance.getEntriesByType('resource').some(e=>e.name.includes('part=live'))", 15)
        res0 = b.eval("performance.getEntriesByType('resource').map(e=>e.name.replace(location.origin,''))") or []
        plain = [r for r in res0 if r.startswith("/api/state") and "part=" not in r]
        check("the page asks for the live half of /api/state (28 KB) and the catalog once, never the whole 140 KB",
              not plain and any("part=live" in r for r in res0) and sum("part=catalog" in r for r in res0) == 1, (plain[:3], [r for r in res0 if "part=" in r][:4]))

        b.type_into("#spRoot", proj)
        b.eval("document.querySelector('#spRoot').dispatchEvent(new Event('change',{bubbles:true}))")
        b.type_into("#spGoal", GOAL)
        typed = b.wait_for(f"run.root === {json.dumps(proj)} && run.goal === {json.dumps(GOAL)}", 5)
        check("Home takes a typed folder and goal", typed, b.eval("({root: run.root, goal: (run.goal||'').slice(0,40)})"))

        b.eval("performance.setResourceTimingBufferSize(10000);performance.clearResourceTimings()")
        b.eval("document.querySelector('#spGo').click()")
        started = b.wait_for("RUNBAR.goal !== ''", 15)
        check("Do it starts the run", started, b.eval("document.querySelector('#spGoalErr') && document.querySelector('#spGoalErr').textContent"))

        seen_working, finished, t0 = False, False, time.time()
        while time.time() - t0 < 120:
            txt = b.eval("(()=>{const p=document.querySelector('#spProgress');return p&&!p.hidden?p.innerText:''})()") or ""
            seen_working = seen_working or "Working on it" in txt
            if "Finished" in txt:
                finished = True
                break
            time.sleep(0.15)
        box = b.eval("(()=>{const p=document.querySelector('#spProgress');return p?p.innerText:''})()") or ""
        check("Home shows 'Working on it' while it runs", seen_working, box[:200])
        check("Home shows the verdict 'It worked'", finished and "It worked" in box, box[:300])
        res = b.eval("performance.getEntriesByType('resource').map(e=>e.name.replace(location.origin,''))") or []
        polls = [r for r in res if r.startswith("/api/arch/events?")]
        streams = [r for r in res if r.startswith("/api/arch/events/stream")]
        check("the run's events were pushed over one stream, not polled (it asked every 1.5 s)", streams and not polls, (len(streams), len(polls)))
        b.eval("window.__spHead=document.querySelector('#spProgress .sp-h')")
        time.sleep(3.5)                                   # two ticks of the 1.5 s Home timer
        kept = b.eval("!!window.__spHead && document.querySelector('#spProgress .sp-h')===window.__spHead")
        check("an unchanged progress box is not rebuilt by the timer (it re-read itself to screen readers every 1.5 s)", kept)

        fixed = "(page - 1) * size" in open(os.path.join(proj, "pager.py"), encoding="utf-8").read()
        tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=proj, capture_output=True, text=True, timeout=120)
        check("the fix is on disk and the tests pass", fixed and tests.returncode == 0, (fixed, tests.stdout[-200:]))

        listed = b.wait_for("[...document.querySelectorAll('.feedrow')].some(r=>r.innerText.includes('paginate() returns the wrong rows'))", 15)
        check("the run is listed in Recent runs", listed)

        undone = False
        if b.click_text("Undo this change"):
            if b.wait_for("document.querySelector('dialog[open]') !== null", 5) and b.click_text("Undo", "document.querySelector('dialog[open]')"):
                t1 = time.time()
                while time.time() - t1 < 20 and not undone:
                    undone = ("start = page * size" in open(os.path.join(proj, "pager.py"), encoding="utf-8").read()
                              and not os.path.exists(os.path.join(proj, "tests", "test_repro_bug.py")))
                    time.sleep(0.2)
        check("Undo restores the file and removes the generated test", undone, os.listdir(os.path.join(proj, "tests")))
        b.eval("performance.setResourceTimingBufferSize(10000);performance.clearResourceTimings()")
        time.sleep(6)
        polls = b.eval("performance.getEntriesByType('resource').filter(e=>e.name.includes('/api/arch/events')).length")
        check("once the run is over, Home stops asking for run events (it polled every 1.5 s forever before)", polls == 0, f"{polls} requests in 6 s")
        # the one dialog component: the shortcuts drawer opens with focus inside, Esc closes it and focus returns; a backdrop click closes
        b.eval("openKeys()")
        opened = b.wait_for("!!document.querySelector('dialog[open].xdlg-drawer') && document.querySelector('dialog[open]').contains(document.activeElement)", 5)
        for kind in ("rawKeyDown", "keyUp"):
            b.cmd("Input.dispatchKeyEvent", {"type": kind, "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
        closed = b.wait_for("!document.querySelector('dialog[open]') && HELP_EL===null", 5)
        b.eval("showText('A window','Some text.')")
        b.wait_for("!!document.querySelector('dialog[open].xdlg-wide')", 5)
        for kind in ("mousePressed", "mouseReleased"):
            b.cmd("Input.dispatchMouseEvent", {"type": kind, "x": 4, "y": 4, "button": "left", "clickCount": 1})
        backdrop = b.wait_for("!document.querySelector('dialog[open]')", 5)
        check("every window is one native dialog: focus goes in, Esc closes it, a backdrop click closes it", opened and closed and backdrop, (opened, closed, backdrop))
        # clickRow (the Cost map's and Self-improve's clickable rows): Tab reaches the row, Enter does what a click does
        b.eval("window.__rowHits=0;const t=document.createElement('table'),tb=document.createElement('tbody');"
               "const r=h('tr',clickRow(()=>{window.__rowHits++;},false),h('td',null,'row'));tb.append(r);t.append(tb);t.id='rowProbe';document.body.append(t);r.focus()")
        for kind in ("rawKeyDown", "keyUp"):
            b.cmd("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
        row_ok = b.eval("document.activeElement&&document.activeElement.tagName==='TR'&&document.activeElement.tabIndex===0&&window.__rowHits===1")
        b.eval("document.getElementById('rowProbe').remove()")
        check("a clickable table row takes focus and opens with Enter (clickRow, as on the Cost map)", row_ok)
        left = b.eval(A11Y_AUDIT) or []
        check("after the run, the scoped accessibility pass left nothing unnamed or unreachable on Home", not left, left[:5])
        b.eval("1")                                        # collect any late page error
        check("no uncaught page error", not b.exceptions, b.exceptions)
    except Exception as ex:
        check("the smoke test ran to the end", False, f"{type(ex).__name__}: {ex}")
    finally:
        if b is not None:
            b.close()
        try:
            if job is not None:
                import arch_sandbox
                arch_sandbox.close(job)
            hub.kill()
            hub.wait(10)
        except Exception:
            pass
        fake.shutdown()
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' UI SMOKE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' UI SMOKE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
