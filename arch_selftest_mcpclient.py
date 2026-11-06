"""Self-test for arch_mcp_client: a stdio MCP fixture, a loopback HTTP one, the trust gate, allow-list, caps, timeouts, never-raise.
No model, no network beyond 127.0.0.1.   python arch_selftest_mcpclient.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="arch_mcp_")
os.environ["ARCHITECT_HOME"] = TMP
os.environ["ARCHITECT_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["ARCHITECT_NO_CACHE"] = "1"

import arch_mcp_client as mc  # noqa: E402

TOOLS = [
    {"name": "echo", "description": "Echo text back.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "what to echo"}}, "required": ["text"]}},
    {"name": "big", "description": "Return a very long text.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Never answers in time.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "secret", "description": "Not on the allow-list.", "inputSchema": {"type": "object", "properties": {}}},
]

# the stdio fixture: a whole MCP server in a few lines
STDIO_SRC = r'''
import sys, json, time
TOOLS = %s
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    r, i = None, m["id"]
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "fx", "version": "1"}}
    elif m["method"] == "tools/list":
        r = {"tools": TOOLS}
    elif m["method"] == "tools/call":
        n, a = m["params"]["name"], m["params"].get("arguments") or {}
        if n == "echo":
            r = {"content": [{"type": "text", "text": "echo:" + str(a.get("text"))}]}
        elif n == "big":
            r = {"content": [{"type": "text", "text": "x" * 50000}]}
        elif n == "slow":
            time.sleep(30)
            r = {"content": []}
        else:
            r = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    print(json.dumps({"jsonrpc": "2.0", "id": i, "result": r}) if r is not None else json.dumps({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "nope"}}), flush=True)
''' % repr(TOOLS)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        m = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "id" not in m:
            self.send_response(202)
            self.end_headers()
            return
        meth, p = m["method"], m.get("params") or {}
        if meth == "initialize":
            res = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "h", "version": "1"}}
        elif meth == "tools/list":
            res = {"tools": TOOLS}
        else:
            a = p.get("arguments") or {}
            res = {"content": [{"type": "text", "text": "http-echo:" + str(a.get("text"))}]}
        body = json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res})
        sse = meth == "tools/call"                                # call replies come as SSE, the rest as JSON
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "s1")
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        out = (f"event: message\ndata: {body}\n\n" if sse else body).encode()
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def run():
    results = []

    def check(name, ok):
        results.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    script = os.path.join(TMP, "fx_server.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(STDIO_SRC)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    try:
        stdio = {"name": "fx", "command": [sys.executable, script], "env": {"X": "1"}, "allowed_tools": ["echo", "big", "slow"]}
        http = {"name": "web", "url": url, "allowed_tools": ["echo"]}
        cfg = {"mcp": {"enabled": False, "timeout": 2, "output_cap": 300, "servers": [stdio, http]}}

        check("default off: no tools", mc.mcp_tools({}) == [] and mc.mcp_tools(cfg) == [])
        cfg["mcp"]["enabled"] = True
        check("enabled but untrusted: nothing starts", mc.mcp_tools(cfg) == [] and not mc._CLIENTS)
        check("trust of an unknown server is refused", mc.trust(cfg, "nope") is False)
        check("trust a server", mc.trust(cfg, "fx") and mc.is_trusted(cfg, "fx") and not mc.is_trusted(cfg, "web"))

        tools = {t["name"]: t for t in mc.mcp_tools(cfg)}
        check("only allowed tools of trusted servers, named mcp.<server>.<tool>", sorted(tools) == ["mcp.fx.big", "mcp.fx.echo", "mcp.fx.slow"])
        sc = tools["mcp.fx.echo"]["schema"]
        check("schema in arch_tools shape", sc["args"]["text"]["type"] == "string" and sc["required"] == ["text"] and "[MCP fx]" in sc["desc"])
        check("stdio call works", tools["mcp.fx.echo"]["call"](None, text="hi") == "echo:hi")
        out = tools["mcp.fx.big"]["call"](None)
        check("output capped", len(out) < 400 and "[cut:" in out)
        import time
        t0 = time.monotonic()
        out = tools["mcp.fx.slow"]["call"](None)
        check("timeout is reported, not raised, and is prompt", out.startswith("mcp error: timed out") and time.monotonic() - t0 < 6)
        check("client recovers after a timeout (respawned)", tools["mcp.fx.echo"]["call"](None, text="again") == "echo:again")
        check("bad arguments never raise", isinstance(tools["mcp.fx.echo"]["call"](None, bogus=1), str))

        mc.trust(cfg, "web")
        t2 = {t["name"]: t for t in mc.mcp_tools(cfg)}
        check("HTTP server tools listed over JSON", "mcp.web.echo" in t2)
        check("HTTP call over SSE works", t2["mcp.web.echo"]["call"](None, text="yo") == "http-echo:yo")

        # registration into an arch_tools-shaped registry
        reg = {}
        names = mc.register(reg, cfg)
        check("register adds arch_tools-shaped records", "mcp.web.echo" in reg and reg["mcp.web.echo"]["fn"](None, text="z") == "http-echo:z" and names)

        # editing revokes trust
        stdio["allowed_tools"].append("secret")
        check("an edit revokes trust", not mc.is_trusted(cfg, "fx"))
        check("revoked: not listed and an old handle refuses", all(not n.startswith("mcp.fx") for n in (t["name"] for t in mc.mcp_tools(cfg)))
              and tools["mcp.fx.echo"]["call"](None, text="a").startswith("refused"))
        stdio["allowed_tools"].remove("secret")
        check("restoring the exact entry restores trust", mc.is_trusted(cfg, "fx"))
        check("untrust", mc.trust(cfg, "fx", yes=False) and not mc.is_trusted(cfg, "fx"))

        # failure modes never raise
        bad = {"mcp": {"enabled": True, "servers": [{"name": "dead", "command": ["definitely-not-a-program-xyz"], "allowed_tools": ["a"]},
                                                      {"name": "wan", "url": "http://8.8.8.8/mcp", "allowed_tools": ["a"]},
                                                      {"name": "gone", "url": "http://127.0.0.1:1/mcp", "allowed_tools": ["a"]}, "junk", 7]}}
        for n in ("dead", "wan", "gone"):
            mc.trust(bad, n)
        check("unstartable / non-LAN / unreachable servers yield no tools and no exception", mc.mcp_tools(bad) == [])
        check("non-LAN host is refused by the client", _refused(mc))
        check("garbage config never raises", mc.mcp_tools({"mcp": "x"}) == [] and mc.mcp_tools(None) == [] and mc.mcp_tools({"mcp": {"enabled": True, "servers": "x"}}) == [])
    finally:
        mc.close_all()
        srv.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' MCP CLIENT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' MCP CLIENT CHECKS FAILED'}")
    return n == len(results)


def _refused(mc):
    try:
        mc.HttpClient({"name": "w", "url": "http://8.8.8.8/mcp"}, 1)
    except mc.McpError:
        return True
    return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
