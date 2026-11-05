"""HTTP layer: JSON in, JSON out, one handler class bound to a Store and a signing secret."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import crypto
from .store import AuthError

MAX_BODY = 16 * 1024
ACCESS_TTL = 900


def make_handler(store, secret):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise AuthError(413, "body too large")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise AuthError(400, "body must be JSON") from None
            if not isinstance(data, dict):
                raise AuthError(400, "body must be a JSON object")
            return data

        def _auth(self, scheme):
            value = self.headers.get("Authorization", "")
            prefix = scheme + " "
            return value[len(prefix):] if value.startswith(prefix) else ""

        def do_POST(self):
            try:
                if self.path == "/register":
                    b = self._body()
                    return self._send(201, {"id": store.register(b.get("email"), b.get("password"))})
                if self.path == "/login":
                    b = self._body()
                    uid, session = store.login(b.get("email"), b.get("password"))
                    token = crypto.sign({"sub": uid}, secret, ACCESS_TTL, now=store.clock())
                    return self._send(200, {"session": session, "access_token": token, "expires_in": ACCESS_TTL})
                if self.path == "/logout":
                    store.logout(self._auth("Session"))
                    return self._send(204)
                self._send(404, {"error": "not found"})
            except AuthError as e:
                self._send(e.status, {"error": str(e)})

        def do_GET(self):
            if self.path != "/me":
                return self._send(404, {"error": "not found"})
            payload = crypto.verify(self._auth("Bearer"), secret, now=store.clock())
            if not payload:
                return self._send(401, {"error": "invalid or expired token"})
            self._send(200, {"id": payload["sub"], "email": store.email_of(payload["sub"])})

    return Handler


def serve(store, secret, host="127.0.0.1", port=8085):
    return ThreadingHTTPServer((host, port), make_handler(store, secret))
