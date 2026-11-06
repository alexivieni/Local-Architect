import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(PLUGIN))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(PLUGIN, "tools"))

import arch_plugins  # noqa: E402
import arch_squad  # noqa: E402
import contrast  # noqa: E402

SKILLS = ["accessibility-review", "design-critique", "design-handoff", "design-system",
          "research-synthesis", "user-research", "ux-copy"]


def head(path):
    with open(path, encoding="utf-8") as f:
        t = f.read()
    m = re.match(r"---\n(.*?)\n---\n", t, re.S)
    assert m, path
    return dict(l.split(": ", 1) for l in m.group(1).splitlines() if ": " in l), t


class DesignPlugin(unittest.TestCase):
    def test_manifest(self):
        m, problems = arch_plugins.read_manifest(PLUGIN)
        self.assertEqual(problems, [])
        self.assertEqual(m["name"], "design")

    def test_skills_have_page_and_command(self):
        for s in SKILLS:
            sh, st = head(os.path.join(PLUGIN, "skills", s + ".md"))
            ch, ct = head(os.path.join(PLUGIN, "commands", s + ".md"))
            self.assertEqual(sh["name"], s)
            self.assertEqual(ch["name"], s)
            self.assertTrue(ch.get("args") and ch.get("description"))
            self.assertIn("{args}", ct)
            self.assertNotIn("Figma URL", ct)

    def test_formats_kept(self):
        t = open(os.path.join(PLUGIN, "skills", "design-critique.md"), encoding="utf-8").read()
        for sec in ("### Overall Impression", "### Usability", "### Visual Hierarchy", "### Consistency",
                    "### Accessibility", "### What Works Well", "### Priority Recommendations", "Critical / "):
            self.assertIn(sec, t)
        t = open(os.path.join(PLUGIN, "skills", "design-handoff.md"), encoding="utf-8").read()
        for sec in ("### Design Tokens Used", "### Components", "### States and Interactions",
                    "### Responsive Behavior", "### Animation / Motion"):
            self.assertIn(sec, t)

    def test_agents_local_and_workflows_validate(self):
        c = arch_plugins.contents(PLUGIN)
        self.assertEqual(c["problems"], [])
        self.assertEqual(c["code_agents"], [])
        for need in ("design_critique", "accessibility_review"):
            self.assertIn(need, c["workflows"])
        adir = os.path.join(PLUGIN, "agents")
        for fn in os.listdir(adir):
            d = arch_squad.load_agent(os.path.join(adir, fn))
            self.assertIn(d["model"], ("worker", "heavy", "fast", "small", "tiny"), fn)
            self.assertFalse(d.get("code"), fn)

    def test_contrast(self):
        self.assertEqual(contrast.ratio("#000", "#fff"), 21.0)
        self.assertEqual(contrast.ratio("#767676", "#ffffff"), 4.54)
        self.assertFalse(contrast.passes("#777777", "#ffffff"))
        self.assertTrue(contrast.passes("#777777", "#ffffff", large=True))

    def test_license_and_notice(self):
        self.assertIn("Apache", open(os.path.join(PLUGIN, "LICENSE"), encoding="utf-8").read())
        self.assertIn("Apache", open(os.path.join(PLUGIN, "NOTICE.md"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
