"""MCP client: let local models call tools from MCP servers the person trusts.

Transports: stdio (spawn a command; newline-delimited JSON-RPC) and streamable HTTP (POST JSON-RPC; JSON or SSE replies) to loopback or
private-LAN hosts only. Methods used: initialize, notifications/initialized, tools/list, tools/call. Standard library only.

Settings (architect_config.json):
    "mcp": {"enabled": false,                      # default OFF
            "timeout": 20,                         # seconds per request
            "output_cap": 4000,                    # characters returned to the model per call
            "servers": [{"name": "files", "command": ["python", "srv.py"], "env": {}, "allowed_tools": ["a", "b"]},
                        {"name": "lan", "url": "http://127.0.0.1:8765/mcp", "headers": {}, "allowed_tools": ["search"]}]}

Trust gate (like plugins): a server is never started or contacted until `trust <name>` approves its exact definition (command, env, url,
headers, allowed_tools). Any edit changes the fingerprint and revokes trust. Only tools named in allowed_tools are exposed (empty list
= none). A spawned server gets a minimal environment (PATH-like basics plus what its entry lists), never the person's other secrets.

mcp_tools(cfg) -> [{"name": "mcp.<server>.<tool>", "schema": {...}, "call": fn(e=None, **args) -> str}]. Nothing here raises into a run.
CLI: python arch_mcp_client.py list|trust <name>|untrust <name>|tools
"""
import atexit
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import arch_core as core
except Exception:  # pragma: no cover
    core = None

PROTOCOL = "2025-03-26"
DEFAULT_TIMEOUT = 20.0
DEFAULT_CAP = 4000
MAX_LINE = 4 * 1024 * 1024
NAME_RX = re.compile(r"[^A-Za-z0-9_-]+")
_ENV_KEEP = ("PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL")


def settings(cfg):
    m = (cfg or {}).get("mcp") or {}
    if not isinstance(m, dict):
        m = {}
    try:
        timeout = float(m.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    try:
        cap = int(m.get("output_cap", DEFAULT_CAP))
    except (TypeError, ValueError):
        cap = DEFAULT_CAP
    raw = m.get("servers")
    servers = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
    return {"enabled": m.get("enabled", False) is True, "timeout": max(0.5, min(timeout, 300.0)), "cap": max(100, min(cap, 100000)),
            "servers": servers}


def _san(s):
    return NAME_RX.sub("_", str(s or "")).strip("_")


def _norm(s):
    cmd = s.get("command")
    if isinstance(cmd, str):
        cmd = cmd.split()
    env, hdr, al = s.get("env"), s.get("headers"), s.get("allowed_tools")
    return {"name": _san(s.get("name")), "command": [str(x) for x in cmd] if isinstance(cmd, list) and cmd else None,
            "url": str(s["url"]) if s.get("url") else None,
            "env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
            "headers": {str(k): str(v) for k, v in hdr.items()} if isinstance(hdr, dict) else {},
            "allowed_tools": sorted(str(x) for x in al) if isinstance(al, list) else []}


def fingerprint(server):
    return hashlib.sha256(json.dumps(_norm(server), sort_keys=True).encode()).hexdigest()[:24]


# ---------------------------------------------------------------- trust

def trust_file():
    base = core.data_dir() if core else os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_data")
    return os.path.join(base, "mcp_trust.json")


def _state():
    try:
        with open(trust_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    try:
        os.makedirs(os.path.dirname(trust_file()), exist_ok=True)
        tmp = trust_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, trust_file())
        return True
    except OSError:
        return False


def _find(cfg, name):
    for s in settings(cfg)["servers"]:
        if _san(s.get("name")) == _san(name):
            return s
    return None


def is_trusted(cfg, name):
    s = _find(cfg, name)
    return bool(s) and _state().get(_san(name)) == fingerprint(s)


def trust(cfg, name, yes=True):
    s = _find(cfg, name)
    if not s:
        return False
    st = _state()
    if yes:
        st[_san(name)] = fingerprint(s)
    else:
        st.pop(_san(name), None)
    return _save(st)


# ---------------------------------------------------------------- clients

class McpError(Exception):
    pass


def _host_ok(host):
    """Loopback or private/link-local only (every address a name resolves to)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for i in infos:
        try:
            ip = ipaddress.ip_address(i[4][0].split("%")[0])
        except ValueError:
            return False
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return False
    return True


class _Base:
    def __init__(self, server, timeout):
        self.server, self.timeout, self._id = _norm(server), timeout, 0
        self.ready = False
        self.lock = threading.RLock()

    def _next(self):
        self._id += 1
        return self._id

    def start(self):
        r = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "architect", "version": "13"}})
        if not isinstance(r, dict):
            raise McpError("bad initialize reply")
        self.notify("notifications/initialized")
        self.ready = True

    def list_tools(self):
        out, cursor = [], None
        for _ in range(20):
            r = self.request("tools/list", {"cursor": cursor} if cursor else {})
            out += [t for t in (r.get("tools") or []) if isinstance(t, dict) and t.get("name")]
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return out

    def call_tool(self, name, args):
        return self.request("tools/call", {"name": name, "arguments": args or {}})

    def alive(self):
        return self.ready


class StdioClient(_Base):
    def __init__(self, server, timeout):
        super().__init__(server, timeout)
        env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
        env.update(self.server["env"])
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        try:
            self.p = subprocess.Popen(self.server["command"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      env=env, **kw)
        except (OSError, ValueError) as ex:
            raise McpError(f"could not start: {type(ex).__name__}")
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in iter(lambda: self.p.stdout.readline(MAX_LINE), b""):
                try:
                    m = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(m, dict):
                    self.q.put(m)
        except Exception:
            pass
        self.q.put(None)

    def alive(self):
        return self.ready and self.p.poll() is None

    def _send(self, msg):
        try:
            self.p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.p.stdin.flush()
        except (OSError, ValueError):
            raise McpError("server closed the connection")

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params=None):
        with self.lock:
            rid = self._next()
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            end = time.monotonic() + self.timeout
            while True:
                left = end - time.monotonic()
                try:
                    if left <= 0:
                        raise queue.Empty
                    m = self.q.get(timeout=left)
                except queue.Empty:
                    self.close()                               # a late reply would confuse the next call: start fresh next time
                    raise McpError(f"timed out after {self.timeout:g}s")
                if m is None:
                    self.q.put(None)
                    raise McpError("server exited")
                if "method" in m and "id" in m:                    # a server-to-client request: we offer nothing
                    self._send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported"}})
                    continue
                if m.get("id") == rid:
                    if "error" in m:
                        raise McpError(str((m["error"] or {}).get("message", "error"))[:200])
                    return m.get("result") or {}

    def close(self):
        self.ready = False
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.terminate()
            self.p.wait(timeout=2)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


class HttpClient(_Base):
    def __init__(self, server, timeout):
        super().__init__(server, timeout)
        u = urllib.parse.urlparse(self.server["url"] or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise McpError("bad url")
        if not _host_ok(u.hostname):
            raise McpError("only loopback or private-network hosts are allowed")
        self.url, self.sid = self.server["url"], None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, msg):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        h.update(self.server["headers"])
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=h, method="POST")
        try:
            r = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as ex:
            raise McpError(f"http {ex.code}")
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise McpError(f"connection failed: {type(ex).__name__}")
        with r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.sid = sid
            ctype = (r.headers.get("Content-Type") or "").lower()
            body = r.read(MAX_LINE + 1)
        if len(body) > MAX_LINE:
            raise McpError("reply too large")
        return ctype, body.decode("utf-8", "replace")

    def notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params=None):
        with self.lock:
            rid = self._next()
            ctype, body = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            msgs = []
            if "text/event-stream" in ctype:
                for ev in re.split(r"\r?\n\r?\n", body):
                    data = "\n".join(ln[5:].lstrip() for ln in ev.splitlines() if ln.startswith("data:"))
                    if data:
                        try:
                            msgs.append(json.loads(data))
                        except ValueError:
                            pass
            else:
                try:
                    j = json.loads(body)
                    msgs = j if isinstance(j, list) else [j]
                except ValueError:
                    raise McpError("reply was not JSON")
            for m in msgs:
                if isinstance(m, dict) and m.get("id") == rid:
                    if "error" in m:
                        raise McpError(str((m["error"] or {}).get("message", "error"))[:200])
                    return m.get("result") or {}
            raise McpError("no reply")

    def close(self):
        self.ready = False


_CLIENTS = {}
_LOCK = threading.Lock()


def _client(server, timeout):
    key = (_san(server.get("name")), fingerprint(server))
    with _LOCK:
        c = _CLIENTS.get(key)
        if c and c.alive():
            return c
        if c:
            c.close()
        n = _norm(server)
        c = StdioClient(n, timeout) if n["command"] else HttpClient(n, timeout)
        try:
            c.start()
        except Exception:
            c.close()
            raise
        _CLIENTS[key] = c
        return c


def close_all():
    with _LOCK:
        for c in list(_CLIENTS.values()):
            try:
                c.close()
            except Exception:
                pass
        _CLIENTS.clear()


atexit.register(close_all)


# ---------------------------------------------------------------- tools

def _text(result, cap):
    parts = []
    for c in (result.get("content") or []):
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            else:
                parts.append(f"[{c.get('type', 'content')} omitted]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"])[:cap + 1])
    t = "\n".join(parts) or "(no output)"
    if result.get("isError"):
        t = "tool error: " + t
    return t if len(t) <= cap else t[:cap] + f"\n... [cut: {len(t) - cap} more characters]"


def _args_for(schema):
    props = schema.get("properties") if isinstance(schema, dict) else None
    args = {}
    if isinstance(props, dict):
        for k, v in props.items():
            v = v if isinstance(v, dict) else {}
            args[str(k)] = {"type": v.get("type") if isinstance(v.get("type"), str) else "string", "description": str(v.get("description", ""))[:200]}
    req = schema.get("required") if isinstance(schema, dict) else None
    return args, [r for r in (req if isinstance(req, list) else []) if r in args]


def _make_call(cfg, server, tool_name, timeout, cap):
    def call(e=None, **args):
        try:
            if not is_trusted(cfg, server.get("name")):
                return "refused: this MCP server is not trusted (or its entry was edited)"
            return _text(_client(server, timeout).call_tool(tool_name, args), cap)
        except McpError as ex:
            return f"mcp error: {ex}"
        except Exception as ex:
            return f"mcp error: {type(ex).__name__}: {str(ex)[:150]}"
    return call


def mcp_tools(cfg):
    """[{name, schema, call}] for every allowed tool of every trusted server. [] when off, untrusted, or failing. Never raises."""
    out = []
    try:
        s = settings(cfg)
        if not s["enabled"]:
            return out
        for server in s["servers"]:
            try:
                name = _san(server.get("name"))
                if not name or not is_trusted(cfg, name):
                    continue
                allowed = set(_norm(server)["allowed_tools"])
                if not allowed:
                    continue
                for t in _client(server, s["timeout"]).list_tools():
                    tn = str(t["name"])
                    if tn not in allowed:
                        continue
                    args, req = _args_for(t.get("inputSchema"))
                    desc = f"[MCP {name}] " + str(t.get("description") or tn)[:300]
                    out.append({"name": f"mcp.{name}.{_san(tn) or tn}",
                                "schema": {"desc": desc, "args": args, "required": req, "writes": True, "inputSchema": t.get("inputSchema")},
                                "call": _make_call(cfg, server, tn, s["timeout"], s["cap"])})
            except Exception:
                continue
    except Exception:
        pass
    return out


def register(tools, cfg):
    """Add the MCP tools to arch_tools.TOOLS (same record shape as @tool). Returns the names added. Never raises."""
    names = []
    for t in mcp_tools(cfg):
        sc, call = t["schema"], t["call"]
        tools[t["name"]] = {"name": t["name"], "fn": (lambda e, _c=call, **kw: _c(e, **kw)), "desc": sc["desc"], "args": sc["args"],
                            "required": sc["required"], "writes": True}
        names.append(t["name"])
    return names


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        cfg = core.load_config(os.getcwd()) if core else {}
    except Exception:
        cfg = {}
    cmd = a[0] if a else "list"
    if cmd == "list":
        for s in settings(cfg)["servers"]:
            print(f"{_san(s.get('name'))}\t{'trusted' if is_trusted(cfg, s.get('name')) else 'NOT trusted'}\t{s.get('command') or s.get('url')}")
        return 0
    if cmd in ("trust", "untrust") and len(a) > 1:
        ok = trust(cfg, a[1], yes=(cmd == "trust"))
        print("ok" if ok else "no such server or could not save")
        return 0 if ok else 1
    if cmd == "tools":
        for t in mcp_tools(cfg):
            print(t["name"], "-", t["schema"]["desc"])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
