import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__.server import serve  # noqa: E402
from __PKG__.store import Store  # noqa: E402

SECRET = "test-secret-0123456789"


@pytest.fixture
def base():
    httpd = serve(Store(iterations=1000), SECRET, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, method="GET", body=None, auth=None):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_register_login_me_logout(base):
    creds = {"email": "a@x.io", "password": "correct horse battery"}
    assert call(base + "/register", "POST", creds)[0] == 201
    status, body = call(base + "/login", "POST", creds)
    assert status == 200 and body["expires_in"] == 900
    status, me = call(base + "/me", auth="Bearer " + body["access_token"])
    assert status == 200 and me["email"] == "a@x.io"
    assert call(base + "/logout", "POST", {}, auth="Session " + body["session"])[0] == 204


def test_bad_requests(base):
    assert call(base + "/me")[0] == 401
    assert call(base + "/me", auth="Bearer forged.token")[0] == 401
    assert call(base + "/login", "POST", {"email": "x@y.io", "password": "nope nope nope"})[0] == 401
    assert call(base + "/nowhere")[0] == 404
