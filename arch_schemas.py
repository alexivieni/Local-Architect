"""JSON schemas for the model calls whose answer code reads (plan, judge, router), and a small validator.

Ollama can constrain a reply to a JSON schema (`format` = the schema), so the model cannot return prose, a missing key or a wrong type. The
reply is STILL validated here: an older server that ignores the schema, or a model that fills it with nonsense, gives None (the callers already
handle None), never a half-valid object. Supported keywords: type, properties, required, items, enum, minimum, maximum, minItems, maxItems,
additionalProperties (a schema), minLength.
"""
PLAN = {"type": "object", "required": ["sprints"], "properties": {"sprints": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
    "type": "object", "required": ["name", "goal", "acceptance"], "properties": {
        "name": {"type": "string", "minLength": 1}, "kind": {"type": "string", "enum": ["backend", "ui", "sql", "mixed"]},
        "goal": {"type": "string", "minLength": 1}, "files": {"type": "array", "items": {"type": "string"}},
        "acceptance": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "required": ["id", "text"],
                                                                                   "properties": {"id": {"type": "string"}, "text": {"type": "string", "minLength": 1}}}}}}},
    "interface": {"type": "array", "maxItems": 30, "items": {"type": "string"}}}}     # optional: the public names of the finished code, decided up front

def validate_plan(plan, root=None, max_sprints=8):
    """Code checks a plan before any work starts. -> (ok, [plain reasons]). Every sprint needs an acceptance check, files must stay
    inside the project, and the plan must be small enough for a small model to follow."""
    import os
    why = []
    sprints = (plan or {}).get("sprints") if isinstance(plan, dict) else None
    if not isinstance(sprints, list) or not sprints:
        return False, ["the plan has no sprints"]
    if len(sprints) > max_sprints:
        why.append(f"the plan has {len(sprints)} sprints; at most {max_sprints}")
    for i, sp in enumerate(sprints, 1):
        if not isinstance(sp, dict):
            why.append(f"sprint {i} is not an object")
            continue
        if not sp.get("acceptance"):
            why.append(f"sprint {i} ('{sp.get('name', '')}') has no acceptance check")
        for f in sp.get("files") or []:
            p = str(f).replace("\\", "/")
            if p.startswith("/") or ".." in p.split("/") or (len(p) > 1 and p[1] == ":"):
                why.append(f"sprint {i} names a file outside the project: {f}")
            elif root and os.path.isabs(os.path.join(root, p)) and not os.path.abspath(os.path.join(root, p)).startswith(os.path.abspath(root)):
                why.append(f"sprint {i} names a file outside the project: {f}")
    return not why, why


JUDGE = {"type": "object", "required": ["scores", "verdict", "issues"], "properties": {
    "scores": {"type": "object", "additionalProperties": {"type": "object", "required": ["score"], "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5}, "evidence": {"type": "string"}}}},
    "verdict": {"type": "string", "enum": ["pass", "fail"]}, "issues": {"type": "array", "items": {"type": "string"}}}}


def router(flow_ids):
    return {"type": "object", "required": ["flow", "why"], "properties": {"flow": {"type": "string", "enum": sorted(flow_ids)}, "why": {"type": "string"}}}


def template_pick(ids):
    return {"type": "object", "required": ["template", "why"], "properties": {"template": {"type": "string", "enum": sorted(ids)}, "why": {"type": "string"}}}


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def errors(obj, schema, path="$"):
    """-> list of plain-language problems (empty = valid)."""
    out = []
    t = schema.get("type")
    if t == "integer":
        if not isinstance(obj, int) or isinstance(obj, bool):
            return [f"{path}: expected an integer"]
    elif t == "number":
        if not isinstance(obj, (int, float)) or isinstance(obj, bool):
            return [f"{path}: expected a number"]
    elif t in _TYPES and not isinstance(obj, _TYPES[t]):
        return [f"{path}: expected {t}"]
    if "enum" in schema and obj not in schema["enum"]:
        out.append(f"{path}: must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            out.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and obj > schema["maximum"]:
            out.append(f"{path}: above {schema['maximum']}")
    if isinstance(obj, str) and len(obj.strip()) < schema.get("minLength", 0):
        out.append(f"{path}: empty")
    if isinstance(obj, list):
        if len(obj) < schema.get("minItems", 0):
            out.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if len(obj) > schema.get("maxItems", 10 ** 9):
            out.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, x in enumerate(obj[:50]):
                out += errors(x, schema["items"], f"{path}[{i}]")
    if isinstance(obj, dict):
        for k in schema.get("required", []):
            if k not in obj:
                out.append(f"{path}: missing '{k}'")
        props = schema.get("properties", {})
        for k, v in obj.items():
            if k in props:
                out += errors(v, props[k], f"{path}.{k}")
            elif isinstance(schema.get("additionalProperties"), dict):
                out += errors(v, schema["additionalProperties"], f"{path}.{k}")
    return out


def valid(obj, schema):
    return not errors(obj, schema)
