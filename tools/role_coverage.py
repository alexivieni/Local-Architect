"""Where is each role used? A role can be live without being named in a flow file: as an agent profile (agents/<role>.md, which squads and crews run),
as a member of a job (arch_jobs.JOBS: models are chosen per job by measurement), or as a stand-in for another role (arch_roles.FALLBACK). This lists,
per role, which of those it has, and the ones that have NONE (genuinely orphaned: nothing would ever call them, assign a model to them or measure them).

    py -3.12 tools/role_coverage.py            the table
    py -3.12 tools/role_coverage.py --check    exit 1 when a role has no use at all, or a role named by a flow/agent/job does not exist
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def coverage():
    """-> {role: {"flow": [...], "agent": bool, "job": [...], "fallback": bool}} plus a list of names used somewhere that are not roles."""
    import arch_flows as F
    import arch_jobs
    import arch_roles
    roles = set(arch_roles.ROLES)
    yaml_text = {os.path.basename(p)[:-5]: open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(HERE, "flows", "*.yaml"))}
    agent_files = {os.path.basename(p)[:-3].replace("-", "_") for p in glob.glob(os.path.join(HERE, "agents", "*.md"))}
    fb = set(arch_roles.FALLBACK) | {r for rs in arch_roles.FALLBACK.values() for r in rs}
    out = {}
    for r in sorted(roles):
        flows = sorted(fid for fid, t in yaml_text.items() if re.search(r"\b%s\b" % re.escape(r), t))
        flows += sorted(fid + "(py)" for fid, f in F.FLOWS.items() if r in (getattr(f, "uses_roles", []) or []) and fid + "(py)" not in flows)
        out[r] = {"flow": flows, "agent": r in agent_files, "job": sorted(j for j, d in arch_jobs.JOBS.items() if r in d["roles"]), "fallback": r in fb}
    named = set()
    for d in arch_jobs.JOBS.values():
        named |= set(d["roles"])
    named |= {r for rs in arch_roles.FALLBACK.values() for r in rs} | set(arch_roles.FALLBACK)
    return out, sorted(named - roles)


def main(argv):
    cov, unknown = coverage()
    orphans = [r for r, c in cov.items() if not (c["flow"] or c["agent"] or c["job"])]
    if "--check" not in argv:
        for r, c in cov.items():
            print(f"  {r:24} flows: {', '.join(c['flow']) or '-':34} agent: {'yes' if c['agent'] else '-':3} jobs: {', '.join(c['job']) or '-'}")
    print(f"{len(cov)} roles; {sum(1 for c in cov.values() if c['flow'])} in a flow, {sum(1 for c in cov.values() if not c['flow'] and (c['agent'] or c['job']))} "
          f"live only as an agent profile or a job member, {len(orphans)} with no use at all: {', '.join(orphans) or 'none'}")
    if unknown:
        print("names used by a job or a fallback that are not roles: " + ", ".join(unknown))
    return 1 if ("--check" in argv and (orphans or unknown)) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
