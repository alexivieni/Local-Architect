import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_core_does_not_import_vscode():
    text = open(os.path.join(ROOT, "src", "core.js"), encoding="utf-8").read()
    assert 'require("vscode")' not in text and re.search(r"module\.exports\s*=", text)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_count_words_in_node():
    script = ("const c=require('./src/core.js');"
              "const md='one two\\n```\\ncode here\\n```\\nthree';"
              "console.log([c.countWords('Hello, world'),c.countWords(md,{excludeCode:true}),c.countWords('')].join(','))")
    out = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "2,3,0"
