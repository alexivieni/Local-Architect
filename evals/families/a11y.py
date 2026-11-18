"""a11y: a page with one accessibility flaw (no alt text, an unlabelled field, an unnamed button, a clickable div, no language, a skipped
heading) must be fixed. The flow's own HTML scan (via the scanner_zero judge) finds none left, and a case-specific text check shows the
real fix; the page must still hold its content (a page emptied to satisfy the scanner fails)."""
import _bank as B

FLOW = "a11y"
DEFAULT_N = 6

PARTS = {
    "lang": ' lang="en"',
    "title": "<title>Contact us</title>",
    "second": "h2",
    "img": '<img src="logo.png" alt="Company logo">',
    "field": '<label for="email">Email address</label>\n<input id="email" type="email">',
    "button": '<button type="submit">Send</button>',
}


def page(**over):
    p = dict(PARTS, **over)
    return ('<!DOCTYPE html>\n<html' + p["lang"] + '>\n<head>\n<meta charset="utf-8">\n' + p["title"] + '\n</head>\n<body>\n'
            '<h1>Contact us</h1>\n<' + p["second"] + '>Write to the team</' + p["second"] + '>\n' + p["img"] + '\n<form>\n' + p["field"] + '\n'
            + p["button"] + '\n</form>\n</body>\n</html>\n')


CASES = [
    # name, what is wrong, what a correct page looks like instead, text the fix must show (regex), text the fix must lose
    ("img-no-alt", dict(img='<img src="logo.png">'), {}, r"<img[^>]*\balt=", []),
    # The text check accepts every fix the flow's own scanner accepts (a label, aria-label or title; a <button> or role="button"; a skipped level
    # closed by adding the missing heading OR lowering the later one): a judge stricter than the gate would fail a correct fix.
    ("unlabelled-input", dict(field='<input id="email" type="email" placeholder="Email address">'), {}, r"<label|aria-label=|aria-labelledby=|\btitle=", []),
    ("empty-button", dict(button='<button type="submit"><span class="icon-send"></span></button>'), {}, r"aria-label=|\btitle=|<button[^>]*>\s*(?:<[^>]+>\s*)*\w", []),
    ("click-on-div", dict(button='<div class="btn" onclick="send()">Send</div>'), dict(button='<button type="button" class="btn" onclick="send()">Send</button>'),
     r"<button[^>]*>\s*Send|role=[\"']button[\"']", []),
    ("no-lang", dict(lang=""), {}, r"<html[^>]*\blang=", []),
    ("heading-skip", dict(second="h3"), {}, r"<h2", []),
]
EMPTIED = '<!DOCTYPE html>\n<html lang="en">\n<head><title>Contact us</title></head>\n<body><p>Hello</p></body>\n</html>\n'


def generate(n, seed=0):
    rng = B.rng_for(seed, "a11y")
    out = []
    for i, (name, bad, good, shows, loses) in enumerate(B.pick(rng, CASES, n)):
        judge = [{"type": "scanner_zero", "kind": "a11y", "files": ["index.html"]},
                 {"type": "file_contains", "path": "index.html", "needles": [r"<form", r"Contact us", r"Send|aria-label", shows], "regex": True},
                 {"type": "no_shrink", "path": "index.html", "min_ratio": 0.7}]
        if loses:
            judge.append({"type": "file_lacks", "path": "index.html", "needles": loses})
        out.append({"id": f"a11y-{name}-{i:02d}", "tags": ["a11y"],
                    "goal": f"Fix the accessibility problem in index.html ({name}) so keyboard and screen-reader users can use the page. Keep its content.",
                    "fixture": {"files": {"index.html": page(**bad)}},
                    "budget": {"calls": 16, "tokens": 60000, "seconds": 600},
                    "judge": judge,
                    "solution": {"files": {"index.html": page(**good)}},
                    "wrong": {"files": {"index.html": EMPTIED}}})
    return out
