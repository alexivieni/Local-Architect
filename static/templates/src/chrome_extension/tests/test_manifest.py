import json
import os
import re

EXT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension")


def manifest():
    with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def test_manifest_is_v3_with_a_semver_version():
    m = manifest()
    assert m["manifest_version"] == 3 and m["name"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", m["version"])


def test_permissions_stay_minimal():
    m = manifest()
    assert set(m["permissions"]) <= {"storage", "activeTab"}
    assert "<all_urls>" not in json.dumps(m)


def test_every_referenced_file_exists():
    m = manifest()
    refs = [m["action"]["default_popup"], m["background"]["service_worker"]]
    refs += [js for cs in m.get("content_scripts", []) for js in cs["js"]]
    missing = [r for r in refs if not os.path.isfile(os.path.join(EXT, r))]
    assert not missing, missing


def test_no_remote_code_anywhere():
    for name in os.listdir(EXT):
        if name.endswith((".js", ".html")):
            text = open(os.path.join(EXT, name), encoding="utf-8").read()
            assert not re.search(r"""src=["']https?:""", text), name
            assert "eval(" not in text, name
