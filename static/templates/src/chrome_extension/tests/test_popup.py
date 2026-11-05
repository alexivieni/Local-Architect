import os
from html.parser import HTMLParser

EXT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension")


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.scripts, self.inline, self.labels, self._in_script = set(), [], 0, set(), False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "label" and "for" in a:
            self.labels.add(a["for"])
        if tag == "script":
            self._in_script = True
            if a.get("src"):
                self.scripts.append(a["src"])
        if any(k.startswith("on") for k in a):
            self.inline += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline += 1


def scan():
    s = Scan()
    s.feed(open(os.path.join(EXT, "popup.html"), encoding="utf-8").read())
    return s


def test_popup_has_no_inline_script_or_handlers():
    assert scan().inline == 0


def test_popup_scripts_exist_and_helpers_load_first():
    s = scan()
    assert s.scripts == ["notes.js", "popup.js"]
    assert all(os.path.isfile(os.path.join(EXT, p)) for p in s.scripts)


def test_ids_used_by_popup_js_exist_and_inputs_are_labelled():
    s = scan()
    js = open(os.path.join(EXT, "popup.js"), encoding="utf-8").read()
    import re
    used = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    assert used <= s.ids, used - s.ids
    assert "note" in s.labels
