"""One-click jobs: a recipe is a goal template plus the flow, workflow and agents that suit it and the pieces of the
project's contract it needs. recipes/*.json holds them; nothing here runs a model.

    list()                      -> every recipe, JSON-able, each with its measured success
    get(rid)                    -> one recipe
    fill(recipe, project, **p)  -> {"goal", "flow", "workflow", "agents", "missing", "ready", ...}: the goal with the project's
                                   own names filled in from its CONTRACT.json (derived from the code when the file is absent)
    success(recipe)             -> {"measured": bool, "pass", "n", "rate", "source", "label"}: read from run traces; "not measured" otherwise
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "recipes")
MIN_RUNS = 3                    # fewer runs than this is an anecdote, not a rate


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get(rid):
    p = os.path.join(DIR, os.path.basename(str(rid)) + ".json")
    return _load(p) if os.path.isfile(p) else None


def _ids():
    try:
        return sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
    except OSError:
        return []


def list(roots=None):                                           # noqa: A001 - the module's public name
    out = []
    for rid in _ids():
        try:
            r = _load(os.path.join(DIR, rid + ".json"))
        except (OSError, ValueError):
            continue
        r["success"] = success(r, roots)
        out.append(r)
    return out


# ------------------------------------------------------------------------------------------------ success from traces
def _verdicts(roots=None):
    """-> [(flow, recipe or "", verdict)] from every trace.jsonl under <root>/.arch/runs and the eval run buckets."""
    import arch_costs
    dirs = []
    for r in arch_costs._roots(roots):
        dirs.append(os.path.join(r, ".arch", "runs"))
    if roots is None:
        try:
            import arch_core
            for b in ("train", "bench"):
                dirs.append(os.path.join(arch_core.data_dir(), "evals", "runs", b, ".arch", "runs"))
        except (ImportError, AttributeError):
            pass
    out = []
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            try:
                with open(os.path.join(d, n, "trace.jsonl"), encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if '"verdict"' not in line or '"type": "verdict"' not in line.replace('"type":"verdict"', '"type": "verdict"'):
                            continue
                        try:
                            j = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(j, dict) and j.get("type") == "verdict":
                            out.append((str(j.get("flow") or ""), str(j.get("recipe") or ""), str(j.get("verdict") or "")))
            except OSError:
                continue
    return out


def success(recipe, roots=None):
    """Runs tagged with this recipe win; otherwise every run of its flow (labelled flow-level). < MIN_RUNS = not measured."""
    r = get(recipe) if isinstance(recipe, str) else recipe
    if not r:
        return {"measured": False, "n": 0, "label": "not measured"}
    vs = _verdicts(roots)
    mine = [v for f, rc, v in vs if rc == r["id"]]
    source = "recipe"
    if len(mine) < MIN_RUNS:
        mine, source = [v for f, rc, v in vs if f == r["flow"]], "flow"
    n = len(mine)
    if n < MIN_RUNS:
        return {"measured": False, "n": n, "label": "not measured"}
    p = sum(1 for v in mine if v == "pass")
    return {"measured": True, "pass": p, "n": n, "rate": round(p / n, 3), "source": source,
            "label": f"{p}/{n} passed" + (" (all runs of this flow)" if source == "flow" else "")}


# ------------------------------------------------------------------------------------------------ fill
def _contract(project):
    import arch_templates_catalog as C
    c = C.read_contract(project)
    if c:
        return c, "CONTRACT.json"
    files = {}
    for dp, dn, fn in os.walk(project):
        dn[:] = [d for d in dn if not d.startswith(".") and d not in ("node_modules", "venv", "__pycache__")]
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                try:
                    with open(p, encoding="utf-8") as h:
                        files[os.path.relpath(p, project).replace(os.sep, "/")] = h.read()
                except (OSError, UnicodeDecodeError):
                    pass
    return C.derive_contract(files, pkg=os.path.basename(os.path.abspath(project))), "derived from the code"


def _has(c, piece):
    if piece == "http":
        return bool(c.get("http"))
    if piece == "cli":
        return bool((c.get("cli") or {}).get("commands"))
    if piece == "tests":
        return bool((c.get("tests") or {}).get("files"))
    if piece == "modules":
        return any(m.get("public") for m in c.get("modules", []))
    return False


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def fill(recipe, project, **params):
    """-> {"recipe", "goal", "flow", "workflow", "agents", "missing": [pieces the project lacks], "ready", "contract_source", "params"}.
    Parameters not given take the recipe default, then names from the project's contract."""
    r = get(recipe) if isinstance(recipe, str) else recipe
    if not r:
        raise KeyError(f"unknown recipe {recipe!r}")
    c, src = _contract(project)
    tdir = os.path.dirname(((c.get("tests") or {}).get("files") or ["tests/x"])[0]) or "tests"
    first_mod = next((m["module"] for m in c.get("modules", []) if m.get("public")), "")
    vals = {"test_dir": tdir, "cli_module": (c.get("cli") or {}).get("module") or first_mod}
    for p in r.get("params", []):
        vals[p["name"]] = p["default"]
    vals.update({k: v for k, v in params.items() if v not in (None, "")})
    if "short" in vals and "short" not in params and vals.get("module"):
        vals["short"] = str(vals["module"]).rsplit(".", 1)[-1]
    if "function" not in params and any(p["name"] == "function" for p in r.get("params", [])):
        fn = next((p["name"] for m in c.get("modules", []) for p in m["public"] if p["kind"] == "function"), "")
        vals["function"] = fn or vals["function"]
    missing = [x for x in r.get("requires", []) if not _has(c, x)]
    return {"recipe": r["id"], "title": r["title"], "goal": r["goal"].format_map(_Safe(vals)), "flow": r["flow"],
            "workflow": r.get("workflow"), "agents": list(r.get("agents", [])), "missing": missing, "ready": not missing,
            "missing_text": [r.get("requires_text", {}).get(x, x) for x in missing], "contract_source": src, "params": vals}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2:
        print(json.dumps(fill(sys.argv[1], sys.argv[2]), indent=2))
    else:
        for x in list():
            print(f"{x['id']:24} {x['flow']:9} {x['success']['label']:16} {x['title']}")
