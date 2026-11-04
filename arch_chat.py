"""Studio: a conversation with your model about a project, through the same engine as every flow.

The reply is grounded in the project the same way a flow's prompt is (the hybrid index picks the files and functions), it is traced like
any call, and nothing it says changes a file: Studio only talks. When the person wants something DONE, the hub turns the message into a
task on the Task page (router + flow + gates), and the run streams back into the thread. One thread per project folder, in memory.
"""
import os
import threading
import time

import arch_core as core

THREADS = {}            # root -> {"messages": [...], "busy": bool, "partial": str, "error": str|None, "seq": int}
_LOCK = threading.Lock()
SYSTEM = ("You are talking with the person who owns this project, as a senior engineer on their team. Answer from the PROJECT CONTEXT given; say plainly what you "
          "do not see. Be concrete and short. Do not write FILE:/EDIT: blocks here: this is a conversation; when a change is wanted, describe it so it can be run "
          "as a task, and suggest the one-line goal to give the team.")


def thread(root):
    with _LOCK:
        return THREADS.setdefault(root, {"messages": [], "busy": False, "partial": "", "error": None, "seq": 0})


def reset(root):
    with _LOCK:
        THREADS[root] = {"messages": [], "busy": False, "partial": "", "error": None, "seq": 0}


def ask(root, text, role="architect", use=None):
    """Start the reply in a background thread. -> False when the thread is busy."""
    t = thread(root)
    with _LOCK:
        if t["busy"]:
            return False
        t["busy"], t["partial"], t["error"] = True, "", None
        t["messages"].append({"who": "you", "text": text, "t": time.time(), "use": use or "local"})
        t["seq"] += 1
    threading.Thread(target=_reply, args=(root, text, role, use), daemon=True).start()
    return True


_CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php", ".cs", ".sql", ".sh")
_STOP = {"which", "where", "what", "does", "function", "functions", "class", "method", "file", "code", "that", "this", "with",
         "from", "into", "have", "there", "about", "would", "should", "could", "the", "and", "for", "how", "is", "are", "a", "an", "in"}


def code_hints(e, question, limit=6):
    """Before the chat model answers: find the code the question is about. A name match (function and class names that share word
    stems with the question, so 'checks that a plan is valid' finds validate_plan) plus the code index, code files only. Seen live
    (2026-11-04): asked which function checks a plan, the chat guessed 'the one that runs the tests'. -> text block, or ""."""
    import re as _re
    words = [w.lower() for w in _re.findall(r"[A-Za-z_]{4,}", question) if w.lower() not in _STOP]
    stems = {w[:5] for w in words}
    if not stems:
        return ""
    hits = []
    for rel in core.list_files(e.root, e.cfg):
        if not rel.endswith(_CODE_EXT):
            continue
        try:
            lines = core.read_text(os.path.join(e.root, rel), 400000)[0].splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            m = _re.match(r"\s*(?:async\s+)?(?:def|class|function|func|fn)\s+([A-Za-z_]\w*)", ln)
            if not m:
                continue
            parts = {p[:5] for p in _re.split(r"_|(?<=[a-z])(?=[A-Z])", m.group(1).lower()) if len(p) >= 4}
            score = len(parts & stems)
            if score:
                hits.append((score, rel, i + 1, lines[i:i + 6]))
    try:                                                      # plus the code index (code files only: docs crowd out the answer)
        for h in e.retriever().search(question, top=12):
            if h.get("path", "").endswith(_CODE_EXT) and not any(x[1] == h["path"] and abs(x[2] - h["start"]) < 3 for x in hits):
                lines = core.read_text(os.path.join(e.root, h["path"]), 400000)[0].splitlines()
                hits.append((0.5, h["path"], h["start"], lines[h["start"] - 1:h["start"] + 5]))
    except Exception:
        pass
    hits.sort(key=lambda x: -x[0])
    out = [f"{rel}:{ln}\n" + "\n".join(body) for _, rel, ln, body in hits[:limit]]
    return ("RELEVANT CODE FOUND IN THE PROJECT (name it with file:line when you answer):\n\n" + "\n\n".join(out)) if out else ""


def _reply(root, text, role, use=None):
    t = thread(root)
    try:
        e = core.Engine(root, "chat", text, review="dry", say=lambda m: None, persist=False)
        think = None
        import arch_cloud
        if use and use.startswith("claude"):                              # the person picked Claude for this message
            e.cfg["roles"][role] = use if use.startswith("claude:") else arch_cloud.chosen_model(e.cfg)
        elif use and use.startswith("local:"):                            # the person picked one of their own models
            e.cfg["roles"][role] = use[6:]
            think = False
        elif arch_cloud.mode(e.cfg) in ("chat", "all"):                       # Settings > Online > Use Claude = chat/all
            e.cfg["roles"][role] = arch_cloud.cloud_model(e.cfg)
        elif (e.cfg.get("behavior") or {}).get("chat_model", "worker") == "worker":
            # A chat must answer in seconds. The role may point at a big planner model (a 14B split across card and RAM writes ~3.5
            # tokens a second: minutes per reply, so the chat looked dead). For this reply only, in memory, it uses the fast worker.
            fast = e.tier_model("worker")
            if fast:
                e.cfg["roles"][role] = fast
                think = False
        ctx = e.context(text, min(e.effective_context_chars(), 14000)) if core.list_files(e.root, e.cfg) else "(the project folder is empty)"
        hist = "\n".join(f"{'Person' if m['who'] == 'you' else 'You'}: {m['text'][:1500]}" for m in t["messages"][-7:-1])
        hints = code_hints(e, text) if core.list_files(e.root, e.cfg) else ""
        user = (f"PROJECT CONTEXT:\n{ctx}\n\n" + (f"{hints}\n\n" if hints else "") + (f"EARLIER IN THIS CONVERSATION:\n{hist}\n\n" if hist else "") + f"PERSON: {text}")
        t0, before = time.time(), arch_cloud.spent()["usd"]
        if use and use.startswith("claude"):
            def _sink(piece):
                with _LOCK:
                    t["partial"] = (t.get("partial") or "") + piece
                    t["seq"] += 1
            with arch_cloud.asked_for(_sink):
                r = e.call(role, user, extra_system=SYSTEM, label="studio", think=think)
        else:
            r = e.call(role, user, extra_system=SYSTEM, label="studio", think=think)
        reply = r["text"].strip() or "(no reply)"
        usd = arch_cloud.spent()["usd"] - before
        with _LOCK:
            t["messages"].append({"who": "model", "text": reply, "model": r["model"], "t": time.time(), "secs": round(time.time() - t0, 1),
                                  "usd": round(usd, 5) if usd else None})
    except Exception as ex:                                        # a chat must never take the hub down
        with _LOCK:
            t["error"] = str(ex)[:300]
            t["messages"].append({"who": "model", "text": f"(no reply: {str(ex)[:200]})", "t": time.time()})
    finally:
        with _LOCK:
            t["busy"], t["partial"] = False, ""
            t["seq"] += 1


def snapshot(root):
    t = thread(root)
    with _LOCK:
        return {"messages": t["messages"][-40:], "busy": t["busy"], "error": t["error"], "seq": t["seq"], "partial": t.get("partial") or ""}
