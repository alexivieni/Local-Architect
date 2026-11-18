"""ioc: an indicator list (iocs.txt) against evidence files, matched EXACTLY by code (an IP, an IP inside a listed CIDR, a domain or a subdomain of it, a
file hash, a defanged URL read as written normally). Each task plants the matches AND decoys that must not match (a neighbouring IP, a domain that only ends
like the indicator, a nearly-equal hash). The judges read the written report: the match count is the right one (it comes from code, not from the model),
every planted indicator is listed, no decoy is, every match has its note, and the person's files are untouched. A task with no match must say so."""
import hashlib

import _bank as B

FLOW = "ioc"
DEFAULT_N = 5

DROPPER = "MZ fake dropper sample, not executable\n"
DROPPER_SHA = hashlib.sha256(DROPPER.encode("utf-8")).hexdigest()
NEAR_SHA = DROPPER_SHA[:-1] + ("0" if DROPPER_SHA[-1] != "0" else "1")

# name, iocs.txt lines, {evidence path: text}, indicators that must be reported as matches, texts that must NOT be, how many matches code must find
# (the counts are worked out by hand from the lines below, one match per indicator per matching line or file; arch_selftest_ioc checks them against the matcher)
CASES = [
    ("ip-and-domain", ["203.0.113.9", "bad-update.example.org"],
     {"evidence/access.log": "10.0.0.5 GET /index.html 200\n203.0.113.9 POST /login 401\n10.0.0.7 GET http://bad-update.example.org/p.exe 200\n10.0.0.8 GET /about 200\n"},
     ["203.0.113.9", "bad-update.example.org"], ["10.0.0.5", "10.0.0.8"], 2),
    ("cidr", ["198.51.100.0/24"],
     {"evidence/fw.log": "DENY 198.51.101.5 -> 10.0.0.2:22\nALLOW 198.51.100.77 -> 10.0.0.2:443\nALLOW 192.0.2.1 -> 10.0.0.2:80\n"},
     ["198.51.100.0/24"], ["198.51.101.5", "192.0.2.1"], 1),
    ("file-hash", [DROPPER_SHA, NEAR_SHA],
     {"evidence/dropper.bin": DROPPER, "evidence/readme.txt": "nothing to see here\n"},
     [DROPPER_SHA], [NEAR_SHA], 1),
    ("defanged-no-match", ["hxxp://evil[.]example[.]org/payload", "192.0.2.200"],
     {"evidence/proxy.log": "GET http://good.example.org/payload 200\nGET http://example.org/evil 200\n10.1.1.1 -> 192.0.2.20\n"},
     [], ["evil.example.org", "192.0.2.200"], 0),
    ("subdomain-boundary", ["evil.example.org", "TOR-EXIT.example.net"],
     {"evidence/dns.log": "q login.evil.example.org A\nq notevil.example.org A\nq tor-exit.example.net A\nq evil.example.org.example.com A\n"},
     ["evil.example.org", "TOR-EXIT.example.net"], ["notevil.example.org", "evil.example.org.example.com"], 2),
]


def _report_check(want, decoys, hits):
    return ("import re\nt = open('docs/ioc_report.md', encoding='utf-8').read()\n"
            f"m = re.search(r'Matches found by code: (\\d+)', t)\nassert m and int(m.group(1)) == {hits}, 'match count: %r' % (m and m.group(1))\n"
            "head, _, notes = t.partition('## Analyst notes')\nmatches = head.partition('## Matches found by code')[2]\n"
            f"for w in {want!r}:\n    assert w.lower() in matches.lower(), 'a planted match is missing: ' + w\n"
            f"for d in {decoys!r}:\n    assert d.lower() not in matches.lower(), 'a decoy was reported as a match: ' + d\n"
            f"for i in range(1, {hits} + 1):\n    assert ('[H%d]' % i) in notes, 'no note for H%d' % i\n")


def notes_for(want, hits):
    return "\n".join(f"- [H{k}] A match code found; check the surrounding log lines and the host involved. `{want[min(k - 1, len(want) - 1)]}`" for k in range(1, hits + 1))


def generate(n, seed=0):
    rng = B.rng_for(seed, "ioc")
    out = []
    for i, (name, iocs, evidence, want, decoys, hits) in enumerate(B.pick(rng, CASES, n)):
        files = {"iocs.txt": "# indicators\n" + "\n".join(iocs) + "\n"}
        files.update(evidence)
        notes = notes_for(want, hits)
        report = "# Indicator match\n\n## Matches found by code: %d\n\n%s\n\n## Analyst notes\n\n%s\n" % (hits, "\n".join(f"- {w}" for w in want), notes)
        out.append({"id": f"ioc-{name}-{i:02d}", "tags": ["ioc", "security"], "fixture": {"files": files},
                    "goal": f"Match the indicators in iocs.txt against the evidence under evidence/ and write the report (case {name}).",
                    "budget": {"calls": 8, "tokens": 40000, "seconds": 600},
                    "judge": [{"type": "file_exists", "paths": ["docs/ioc_report.md"]}, {"type": "python_assert", "code": _report_check(want, decoys, hits)},
                              {"type": "files_unchanged", "paths": sorted(files)}],
                    "oracle": {"ioc_notes": notes},
                    "solution": {"files": {"docs/ioc_report.md": report}},
                    "wrong": {"files": {"docs/ioc_report.md": "# Indicator match\n\n## Matches found by code: 9\n\n" + "\n".join(f"- {d}" for d in decoys) + "\n\n## Analyst notes\n\nall bad\n"}}})
    return out
