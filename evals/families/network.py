"""network: a subnet plan with real problems (overlap, gateway outside, capacity); the fixed plan is re-checked by the same math."""
import json

import _bank as B

FLOW = "network"
DEFAULT_N = 6
BAD = [
    ("overlap", {"subnets": [{"name": "web", "cidr": "10.0.0.0/24", "hosts": 50, "gateway": "10.0.0.1"}, {"name": "db", "cidr": "10.0.0.128/25", "hosts": 20, "gateway": "10.0.0.129"}]}),
    ("gateway-outside", {"subnets": [{"name": "web", "cidr": "10.0.1.0/24", "hosts": 50, "gateway": "10.0.2.1"}, {"name": "db", "cidr": "10.0.2.0/24", "hosts": 20, "gateway": "10.0.2.1"}]}),
    ("too-small", {"subnets": [{"name": "web", "cidr": "10.0.1.0/28", "hosts": 50, "gateway": "10.0.1.1"}, {"name": "db", "cidr": "10.0.2.0/24", "hosts": 20, "gateway": "10.0.2.1"}]}),
    ("public-range", {"subnets": [{"name": "web", "cidr": "8.8.8.0/24", "hosts": 50, "gateway": "8.8.8.1"}, {"name": "db", "cidr": "10.0.2.0/24", "hosts": 20, "gateway": "10.0.2.1"}]}),
    ("overlap-three", {"subnets": [{"name": "a", "cidr": "192.168.1.0/24", "hosts": 10, "gateway": "192.168.1.1"}, {"name": "b", "cidr": "192.168.1.0/25", "hosts": 10, "gateway": "192.168.1.1"}, {"name": "c", "cidr": "192.168.2.0/24", "hosts": 10, "gateway": "192.168.2.1"}]}),
    ("mixed", {"subnets": [{"name": "web", "cidr": "10.1.0.0/26", "hosts": 100, "gateway": "10.1.0.1"}, {"name": "db", "cidr": "10.1.0.0/24", "hosts": 20, "gateway": "10.1.0.1"}]}),
]
GOOD = {"subnets": [{"name": "web", "cidr": "10.0.1.0/24", "hosts": 50, "gateway": "10.0.1.1"}, {"name": "db", "cidr": "10.0.2.0/24", "hosts": 20, "gateway": "10.0.2.1"}, {"name": "ops", "cidr": "10.0.3.0/24", "hosts": 10, "gateway": "10.0.3.1"}]}


def generate(n, seed=0):
    rng = B.rng_for(seed, "network")
    out = []
    for i, (name, plan) in enumerate(B.pick(rng, BAD, n)):
        out.append({"id": f"network-{name}-{i:02d}", "tags": ["network"], "goal": f"Fix the subnet plan in network/plan.json ({name}): the CIDR math reports problems.",
                    "fixture": {"files": {"network/plan.json": json.dumps(plan, indent=1) + "\n"}},
                    "budget": {"calls": 10, "tokens": 30000, "seconds": 600},
                    "judge": [{"type": "scanner_zero", "kind": "cidr", "path": "network/plan.json"}, {"type": "file_exists", "paths": ["docs/network.md"]}],
                    "oracle": {"net": json.dumps(GOOD, indent=1)}, "wrong": {"files": {"network/plan.json": json.dumps(plan)}}})
    return out
