"""The hub's page registry as data: places (the sidebar), pages (NAV_GROUPS rows), the old-id map and the page functions, read ONCE
from hub_ui.html so a rename edits one place and no test has to carry its own regex.

    import arch_uireg; reg = arch_uireg.load()      # cached by file time; GET /api/arch/ui/registry serves the same dict

hub_ui.html stays the single source (no build step). This module only reads it; it writes nothing and imports nothing heavy."""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "hub_ui.html")
_CACHE = {}


def _block(js, start, end):
    a = js.find(start)
    if a < 0:
        return ""
    a += len(start)
    b = js.find(end, a)
    return js[a:b] if b >= 0 else ""


def _json_list(src):
    """A JS array literal made only of double-quoted strings and arrays is JSON already."""
    try:
        return json.loads("[" + re.sub(r",\s*$", "", src.strip()) + "]")
    except ValueError:
        return []


def _obj(src):
    return dict(re.findall(r'(\w+):"([^"]*)"', src))


def parse(html):
    js = html.replace("\r\n", "\n")
    nav = _json_list(_block(js, "const NAV_GROUPS=[", "];\nconst PAGES"))
    side = _json_list(_block(js, "const SIDEBAR=[", "\n];"))
    pages = []
    for grp in nav:
        for row in grp[1]:
            if len(row) >= 4:
                pages.append({"id": row[0], "icon": row[1], "label": row[2], "description": row[3], "group": grp[0]})
    sidebar = []
    for sec, places in side:
        sidebar.append({"section": sec, "places": [{"id": p[0], "icon": p[1], "label": p[2], "pages": list(p[3])} for p in places]})
    places = [p for s in sidebar for p in s["places"]]
    funcs = sorted(set(re.findall(r"\bfunction (\w+Page)\(", js)) | {n for n in ("settings", "train", "overview", "models", "roles", "flows", "portfolio", "scoreboard", "start")
                                                                    if re.search(r"\bfunction %s\(" % n, js)})
    return {"sidebar": sidebar, "places": places, "pages": pages, "page_ids": [p["id"] for p in pages],
            "page_icon": {p["id"]: p["icon"] for p in pages}, "legacy": _obj(_block(js, "const LEGACY={", "};")),
            "page_art": _obj(_block(js, "const PAGE_ART={", "};")), "sec_art": _obj(_block(js, "const SEC_ART={", "};")),
            "page_functions": funcs,
            "place_pages": {p["id"]: p["pages"] for p in places}}


def load(path=None):
    """-> the registry dict for hub_ui.html (re-read only when the file changed)."""
    path = path or UI_FILE
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return parse("")
    if _CACHE.get("key") != key:
        with open(path, encoding="utf-8", errors="replace") as f:
            _CACHE["key"], _CACHE["val"] = key, parse(f.read())
    return _CACHE["val"]


if __name__ == "__main__":
    r = load()
    print(f"{len(r['sidebar'])} sections, {len(r['places'])} places, {len(r['pages'])} pages, {len(r['legacy'])} legacy ids, {len(r['page_functions'])} page functions")
