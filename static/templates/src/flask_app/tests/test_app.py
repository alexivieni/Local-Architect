import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytest.importorskip("flask")

from __PKG__ import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app.test_client()


def test_create_list_delete(client):
    r = client.post("/api/notes", json={"title": "First", "body": "hello"})
    assert r.status_code == 201 and r.get_json()["id"] == 1
    assert [n["title"] for n in client.get("/api/notes").get_json()] == ["First"]
    assert b"First" in client.get("/").data
    assert client.delete("/api/notes/1").status_code == 204
    assert client.delete("/api/notes/1").get_json() == {"error": "not found"}


def test_validation_errors_are_400_with_fields(client):
    r = client.post("/api/notes", json={"title": ""})
    assert r.status_code == 400 and r.get_json()["fields"] == {"title": "required"}
    assert client.post("/api/notes", data="nope", content_type="text/plain").status_code == 400


def test_html_is_escaped(client):
    client.post("/api/notes", json={"title": "<script>x</script>"})
    assert b"<script>x" not in client.get("/").data
