"""Input checks with no framework import: -> (clean dict, {field: problem})."""

LIMITS = {"title": 120, "body": 5000}


def note(data):
    if not isinstance(data, dict):
        return None, {"_": "body must be a JSON object"}
    clean, errors = {}, {}
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        errors["title"] = "required"
    elif len(title.strip()) > LIMITS["title"]:
        errors["title"] = f"at most {LIMITS['title']} characters"
    else:
        clean["title"] = title.strip()
    body = data.get("body", "")
    if not isinstance(body, str):
        errors["body"] = "must be text"
    elif len(body) > LIMITS["body"]:
        errors["body"] = f"at most {LIMITS['body']} characters"
    else:
        clean["body"] = body
    return (None if errors else clean), errors
