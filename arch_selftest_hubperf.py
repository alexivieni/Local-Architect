"""Checks for the hub speed fixes (docs/perf_report_20261104.md #1, #3, #7, #8, #9).

Run: python arch_selftest_hubperf.py
"""
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("ARCHITECT_DATA_DIR", tempfile.mkdtemp(prefix="arch_hubperf_"))
os.environ.setdefault("ARCHITECT_NO_CACHE", "1")
sys.path.insert(0, HERE)

N = [0]


def check(cond, what):
    if not cond:
        raise SystemExit("FAILED: " + what)
    N[0] += 1


def fake_hw():
    hw = types.SimpleNamespace(calls=0)

    def live(run=None):
        hw.calls += 1
        return {"gpu_pct": 11, "vram_gb": 8.0, "ram_free_gb": 3.0}

    def record_once(path, run=None, max_bytes=0, extra=None):
        return dict(t=round(time.time(), 1), **live(), **(extra or {}))
    hw.live, hw.record_once = live, record_once
    return hw


def backend():
    import architect_hub as ah
    hw = fake_hw()
    ah._LIVE_LAST.clear()
    # no recorder sample yet: the endpoint measures for itself
    s = ah._live_sample(hw)
    check(hw.calls == 1 and s["gpu_pct"] == 11, "without a recorder sample the endpoint calls live()")
    ah._keep_recorder_samples(hw)
    wrapped = hw.record_once
    ah._keep_recorder_samples(hw)
    check(hw.record_once is wrapped, "wrapping twice is a no-op")
    row = hw.record_once("x.log", extra={"servers": ["worker"]})
    check(row["servers"] == ["worker"], "the recorder still gets its own row back")
    before = hw.calls
    s = ah._live_sample(hw)
    check(hw.calls == before, "a fresh recorder sample is reused: no second measurement")
    check(s == {"gpu_pct": 11, "vram_gb": 8.0, "ram_free_gb": 3.0}, "the reused sample has live()'s fields only")
    s = ah._live_sample(hw, now=time.time() + ah.LIVE_FRESH_S + 1)
    check(hw.calls == before + 1, "a stale sample (recorder off or stalled) means a fresh measurement")
    check("_at" in ah._LIVE_LAST, "reading a sample does not change the kept one")


def ui():
    with open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8") as f:
        src = f.read()
    check('STATE_VOLATILE=new Set(["time","ram_free_gb","vram_free_gb","expires_at","size_vram"])' in src,
          "volatile readings are left out of the state change key")
    check("JSON.stringify(nxt,(k,v)=>STATE_VOLATILE.has(k)?undefined:v)" in src, "refresh() keys on STATE_VOLATILE")
    check("if(refresh.busy&&!force)return; refresh.busy=1;" in src and "finally{refresh.busy=0;}" in src,
          "state refreshes do not stack")
    check("async function jobBadge(){\n  if(document.hidden)return;" in src, "jobBadge does not poll a hidden tab")
    i = src.index("async function pollConsole(){")
    body = src[i:src.index("function renderThread", i)]
    check("if(document.hidden){pollConsole._t=setTimeout(pollConsole,4000);return;}" in body,
          "pollConsole backs off in a hidden tab")
    check(body.index("document.hidden") < body.index("api(\"/api/arch/chat"), "the hidden check comes before any request")
    check("S!==pollConsole._S||box!==pollConsole._el" in body, "the panels redraw only on new events, state or a rebuilt page")
    check(body.count("pollConsole._t=setTimeout") >= 3, "pollConsole still re-arms itself on every path")


def ollama_down_fast():
    """2026-11-08: with Ollama stopped, Windows took about 2 s to refuse each loopback connect, so every /api/state waited 2 s."""
    import socket
    import model_hub as mh
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                            # a loopback port nothing listens on
    old = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{port}"
    try:
        t = time.time()
        down = mh._loopback_down()
        took = time.time() - t
        check(down and took < 1.0, f"a stopped local Ollama is reported in under a second (took {took:.2f} s)")
        try:
            mh.model_list({"vram_gb": 0, "ram_gb": 8}, "f16", 100, 20)
            check(False, "model_list reports a stopped Ollama")
        except mh.OllamaError as e:
            check("not reachable" in e.msg, "model_list says Ollama is not reachable")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.getsockname()[1]}"
        check(not mh._loopback_down(), "a local port that accepts is not reported down")
        srv.close()
        os.environ["OLLAMA_HOST"] = "10.1.2.3:11434"
        check(not mh._loopback_down(), "a remote Ollama host is never probed by the loopback shortcut")
    finally:
        if old is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = old


if __name__ == "__main__":
    backend()
    ui()
    ollama_down_fast()
    print(f"ALL {N[0]} HUBPERF CHECKS PASSED")
