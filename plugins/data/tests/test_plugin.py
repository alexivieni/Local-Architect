"""Offline checks for the data plugin: manifest, command + skill per source skill, local-only agents, valid workflows."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(PLUGIN))
sys.path.insert(0, ROOT)

import arch_ci  # noqa: E402
import arch_commands  # noqa: E402
import arch_plugins  # noqa: E402
import arch_skills  # noqa: E402
import arch_squad  # noqa: E402

SOURCE_SKILLS = ['analyze','build-dashboard','create-viz','data-context-extractor','data-visualization','explore-data','sql-queries','statistical-analysis','validate-data','write-query']
REQUIRED_WORKFLOWS = ['validate-data', 'analyze']


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _yaml_files(kind):
    d = os.path.join(PLUGIN, kind)
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".yaml")]


def test_manifest_validates():
    m = arch_plugins.read_manifest(PLUGIN)[0]
    assert m and m["name"] == "data"
    assert arch_plugins.validate_manifest(m) == []


def test_licence_and_notice():
    assert "Apache License" in _read(os.path.join(PLUGIN, "LICENSE"))
    n = _read(os.path.join(PLUGIN, "NOTICE.md"))
    assert "Apache License 2.0" in n and "What was changed" in n


def test_every_source_skill_has_command_and_skill_page():
    for name in SOURCE_SKILLS:
        c = arch_commands.parse(_read(os.path.join(PLUGIN, "commands", name + ".md")), name + ".md", "plugin")
        assert c and c["name"] == name and c["args_hint"], name
        s = arch_skills.parse(_read(os.path.join(PLUGIN, "skills", name + ".md")), name + ".md", "plugin")
        assert s and s["name"] == name and s["when"] and not s["truncated"], name
        assert len(s["body"]) > 400, name
    have = {f[:-3] for f in os.listdir(os.path.join(PLUGIN, "commands")) if f.endswith(".md")}
    assert have == set(SOURCE_SKILLS)


def test_agents_are_local():
    tiers = ("worker", "fast", "reasoner", "judge")
    for p in _yaml_files("agents"):
        text = _read(p)
        d = arch_ci.load_yaml(text)
        assert arch_squad.validate(d) == [], p
        assert "claude" not in str(d.get("model")).lower() and not re.search(r"^\s*claude\s*:", text, re.M), p
        assert not d.get("code"), p
        assert d.get("model") in tiers or re.match(r"^[\w.:/-]+$", str(d.get("model"))), p


def test_workflows_validate():
    names = {os.path.splitext(os.path.basename(p))[0] for p in _yaml_files("workflows")}
    for w in REQUIRED_WORKFLOWS:
        assert w in names, w
    c = arch_plugins.contents(PLUGIN)
    assert c["problems"] == [], c["problems"]
    assert names <= set(c["workflows"])
    for p in _yaml_files("workflows"):
        assert "claude:" not in _read(p), p


def test_plugin_has_no_problems_in_validate_all():
    assert arch_plugins.validate().get("data") == []
