import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pkg():
    with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as f:
        return json.load(f)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_manifest_basics():
    p = pkg()
    assert re.fullmatch(r"[a-z0-9_.-]+", p["name"]) and p["engines"]["vscode"].startswith("^")
    assert os.path.isfile(os.path.join(ROOT, p["main"]))


def test_every_contributed_command_is_registered_and_vice_versa():
    declared = {c["command"] for c in pkg()["contributes"]["commands"]}
    registered = set(re.findall(r'registerCommand\("([^"]+)"', read("src/extension.js")))
    assert declared == registered


def test_settings_read_in_code_are_declared():
    props = pkg()["contributes"]["configuration"]["properties"]
    section = pkg()["name"]
    used = {f"{section}.{k}" for k in re.findall(r'\.get\("([^"]+)"', read("src/extension.js"))}
    assert used <= set(props), used - set(props)
    assert all("description" in v and "type" in v for v in props.values())
