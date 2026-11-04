"""How does THIS model behave inside the harness? Three small real tasks, judged by code (never by the model):

  * edit with search/replace blocks (patch style): does it act, produce blocks that apply, change only what was asked?
  * the same with whole-file blocks (what small models often do better)
  * answer in a JSON shape the harness reads (constrained by schema, then validated)

Community reports of local agents describe the same few failures: asks permission instead of acting, prints the call as text, returns only the
new section so the file is overwritten, drifts from the required shape. This measures which of them a given model has, so the harness can pick the edit
style that works for it and the Models page can say so, instead of discovering it halfway through a build.
"""
import os
import re
import shutil
import tempfile

import arch_core as core
import arch_roles
import arch_schemas

FIXTURE = '''"""Small greeting helpers."""


def greet(name):
    """Return a greeting for one person."""
    return f"hello, {name}"


def greet_all(names):
    """Greet several people, one per line."""
    return "\\n".join(greet(n) for n in names)


def shout(name):
    """Greeting in capitals."""
    return greet(name).upper()


def polite(name):
    """A formal greeting."""
    return f"hello, dear {name}"


def empty():
    """What to say to nobody."""
    return "hello, nobody"


if __name__ == "__main__":
    print(greet("world"))
'''
TASK = ("In greeter.py, change the greeting word \"hello\" to \"hi\" in the three strings that return a greeting (greet, polite, empty). "
        "Change nothing else.\n\ngreeter.py:\n```python\n{code}```")
ASK_RX = core.ASKS_RX


def judge_reply(reply):
    """Code's verdict on one reply. -> 'ok' | 'wrong' | 'broken' | 'asks' | 'no_blocks'."""
    blocks = core.parse_blocks(reply)
    if not blocks:
        return "asks" if ASK_RX.search((reply or "")[-400:]) else "no_blocks"
    tmp = tempfile.mkdtemp(prefix="archprobe_")
    try:
        with open(os.path.join(tmp, "greeter.py"), "w", encoding="utf-8") as f:
            f.write(FIXTURE)
        cfg = core.load_config(tmp)
        for b in blocks[:3]:
            if b.get("truncated"):
                return "broken"
            try:
                p = core.prepare_block(tmp, cfg, b)
            except (ValueError, OSError):
                return "broken"
            if p["status"] == "error":
                return "broken"
            if p["status"] == "ok":
                core.write_text(p["full"], p["new"], p["crlf"])
        new = open(os.path.join(tmp, "greeter.py"), encoding="utf-8").read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    old_lines, new_lines = FIXTURE.splitlines(), new.splitlines()
    changed = [ln for ln in new_lines if ln not in old_lines]
    wanted = {'    return f"hi, {name}"', '    return f"hi, dear {name}"', '    return "hi, nobody"'}
    untouched = [ln for ln in old_lines if "hello" not in ln]
    if not wanted <= set(changed) or abs(len(new_lines) - len(old_lines)) > 1:
        return "wrong"
    if any(ln not in new_lines for ln in untouched) or len(changed) > 4:
        return "wrong"
    compile(new, "greeter.py", "exec")
    return "ok"


def modes(ask, say=print):
    """`ask(system, user, fmt) -> reply text`. -> {"patch", "whole", "json", "acts", "recommend_edit_style", "notes"} (all judged by code)."""
    out = {"patch": None, "whole": None, "json": None, "notes": []}
    for style in ("patch", "whole"):
        system = arch_roles.system_prompt("generator", "")
        note = arch_roles.edit_style_note("whole" if style == "whole" else "patch")
        if note:
            system = system + "\n\n" + note
        try:
            verdict = judge_reply(ask(system, TASK.format(code=FIXTURE), None))
        except SyntaxError:
            verdict = "broken"
        out[style] = verdict
        say(f"    edit with {style} blocks: {verdict}")
    schema = arch_schemas.router(["build", "bugfix"])
    try:
        reply = ask("Answer ONLY JSON.", 'Pick the workflow for: "the login button crashes". Answer {"flow": "<build|bugfix>", "why": "short"}.', schema)
        j = core.parse_json(reply)
        out["json"] = bool(j) and arch_schemas.valid(j, schema)
    except core.LLMError:
        out["json"] = False
    say(f"    JSON in the required shape: {'yes' if out['json'] else 'no'}")
    out["acts"] = out["patch"] in ("ok", "wrong", "broken") or out["whole"] in ("ok", "wrong", "broken")
    if out["patch"] == "ok":
        out["recommend_edit_style"] = "patch"
    elif out["whole"] == "ok":
        out["recommend_edit_style"] = "whole"
        out["notes"].append("search/replace edits did not apply cleanly for this model; whole-file edits did, so the harness should use those")
    else:
        out["recommend_edit_style"] = None
        out["notes"].append("neither edit style produced a correct change on a 30-line file: expect a poor builder; prefer another model for that role")
    if out["patch"] in ("asks",) or out["whole"] in ("asks",):
        out["notes"].append("it asked a question instead of editing when told to act")
    if out["patch"] == "no_blocks" and out["whole"] == "no_blocks":
        out["notes"].append("it did not answer in file blocks at all (it may print tool JSON or prose); the harness's file-block format needs a different model for building")
    if not out["json"]:
        out["notes"].append("it did not return JSON in the required shape; plans and verdicts from it will often be discarded")
    return out


def live_ask(model, ctx=8192):
    """An `ask` that really calls the model through the same path the harness uses."""
    def ask(system, user, fmt):
        r = core.stream_chat(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                             {"num_ctx": ctx, "temperature": 0.2, "num_predict": 1500}, False, "10m", {"first": 900, "idle": 300, "total": 900}, fmt)
        return core.strip_think(r["text"])
    return ask
