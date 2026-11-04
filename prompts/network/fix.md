This network/firewall plan has problems found by checking real CIDR math (overlap, host capacity, gateway
placement) and firewall-rule math (default policy, any-any rules, admin ports exposed, shadowed rules). Fix them.
Keep the same JSON shape (top-level "subnets" for a network plan, or "default_inbound"/"rules" for a firewall plan).

FILE: {name}
PROBLEMS:
{problems}

CURRENT CONTENT:
{content}

{feedback}
