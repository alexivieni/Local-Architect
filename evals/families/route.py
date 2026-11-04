"""route: plain requests must reach the right workflow, answered by the router model (a keyword fallback does not count)."""
import _bank as B

FLOW = "route"
DEFAULT_N = 12
CASES = [("the login page crashes with a traceback when the password is empty", "bugfix"),
         ("make the report generation a lot faster", "optimize"),
         ("write unit tests for the parser module", "test"),
         ("set up GitHub Actions so our tests run on every push", "cicd"),
         ("restructure the order module without changing what it does", "refactor"),
         ("do a code review of the payments module", "review"),
         ("add an endpoint that returns the last ten orders as JSON", "build"),
         ("something in the checkout gives the wrong total when a coupon is applied", "bugfix"),
         ("harden the Dockerfile and the CI config", "config"),
         ("find hidden bugs in the scheduler before we ship", "hunt"),
         ("sketch the architecture and the data model for a booking service before we code", "design"),
         ("fix the security findings in auth.py: hardcoded token and shell=True", "secure"),
         ("the firewall plan has overlapping subnets, fix the network plan", "network"),
         ("remove the placeholder stubs and fake tests from the repo", "purge")]


def generate(n, seed=0):
    rng = B.rng_for(seed, "route")
    out = []
    for i, (req, want) in enumerate(B.pick(rng, CASES, n)):
        out.append({"id": f"route-{want}-{i:02d}", "kind": "route", "tags": ["routing"], "goal": req, "want": want,
                    "fixture": {"files": {"app.py": "x = 1\n", "tests/test_app.py": "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"}},
                    "budget": {"calls": 2, "tokens": 4000, "seconds": 120},
                    "judge": [{"type": "route_equals", "want": want}]})
    return out
