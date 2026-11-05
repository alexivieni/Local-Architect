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


CTX_SIZES = {"small": 4000, "normal": 14000, "large": 60000, "max": 200000}


def ask(root, text, role="architect", use=None, effort=None, ctx=None, ultra=False, web=False):
    """Start the reply in a background thread. `web` = Claude may look this one message up on the web. -> False when busy."""
    t = thread(root)
    with _LOCK:
        if t["busy"]:
            return False
        t["busy"], t["partial"], t["error"] = True, "", None
        t["messages"].append({"who": "you", "text": text, "t": time.time(), "use": use or "local", "web": bool(web)})
        t["seq"] += 1
    threading.Thread(target=_reply, args=(root, text, role, use, effort, ctx, ultra, web), daemon=True).start()
    return True


# ---- the local scout: before Claude answers, your own worker model (free, on this machine) reads the big project context and writes a
# short brief; Claude then reads the brief plus the top few code hints instead of the whole context. Any failure keeps the full context.
SCOUT_MIN_CHARS = 6000          # below this the context is cheap enough to send as it is
SCOUT_TIMEOUT_S = 60
SCOUT_MAX_WORDS = 600
SCOUT_HINTS = 3                 # code hints that still go to Claude next to the brief
SCOUT_SYSTEM = ("You prepare a brief for a senior engineer who will answer a question about this project but will NOT see the project "
                "context. From the PROJECT CONTEXT and RELEVANT CODE only, write at most 600 words: 1) the files that matter, 2) the "
                "functions and classes that matter, each as path:line with one line on what it does, 3) what the question actually needs "
                "and what you could not find. Quote names exactly. Do not answer the question; do not invent files or lines.")


def scout_on(cfg):
    v = ((cfg or {}).get("online") or {}).get("local_scout", "on")
    return not (v is False or str(v).strip().lower() in ("off", "false", "0", "no"))


def _local_caller(model, timeout):
    """The default scout call: the local transport directly (not Engine.call, whose lock a timed-out scout would keep holding)."""
    def call(messages):
        r = core.stream_chat(model, messages, {"num_predict": 1200, "temperature": 0}, think=False, timeout=timeout)
        return (r or {}).get("text") or ""
    return call


def scout(question, ctx, hints, model, call=None, timeout=SCOUT_TIMEOUT_S):
    """-> (brief, why). brief is "" when the scout could not help (no local model, error, timeout, empty or useless reply);
    why says so in plain words. Runs in its own thread so a slow model never holds the chat past `timeout` seconds."""
    import arch_cloud
    if not model or arch_cloud.is_cloud(model):
        return "", "no local worker model set (Settings > Tiers > worker)"
    call = call or _local_caller(model, timeout)
    msgs = [{"role": "system", "content": SCOUT_SYSTEM},
            {"role": "user", "content": f"PROJECT CONTEXT:\n{ctx}\n\n" + (f"{hints}\n\n" if hints else "") + f"QUESTION: {question}\n\nBRIEF:"}]
    box = {}

    def work():
        try:
            box["text"] = call(msgs)
        except Exception as ex:                                      # a local model error is a fallback, never a failed chat
            box["err"] = f"{type(ex).__name__}: {str(ex)[:160]}"
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return "", f"the local scout took longer than {int(timeout)} s"
    if "err" in box:
        return "", "the local scout failed: " + box["err"]
    words = str(box.get("text") or "").strip().split()
    if len(words) < 15:
        return "", "the local scout's brief was empty or too short"
    brief = " ".join(words[:SCOUT_MAX_WORDS]) if len(words) > SCOUT_MAX_WORDS else str(box["text"]).strip()
    return brief, ""


def compose(question, ctx, hint_items, hist, cfg, to_claude, worker="", call=None, timeout=SCOUT_TIMEOUT_S):
    """The user turn for one chat answer. -> (user, scout_record or None). For a Claude answer over a big context (and the scout
    on), the brief + the top SCOUT_HINTS code hints replace the full context; the record keeps scout_chars vs full_chars."""
    hints = _hints_block(hint_items)
    tail = (f"EARLIER IN THIS CONVERSATION:\n{hist}\n\n" if hist else "") + f"PERSON: {question}"
    full = f"PROJECT CONTEXT:\n{ctx}\n\n" + (f"{hints}\n\n" if hints else "") + tail
    full_chars = len(ctx) + len(hints)
    if not (to_claude and scout_on(cfg) and full_chars > SCOUT_MIN_CHARS):
        return full, None
    t0 = time.time()
    brief, why = scout(question, ctx, hints, worker, call=call, timeout=timeout)
    rec = {"label": "scout", "model": worker or "(none)", "local": True, "usd": 0.0, "secs": round(time.time() - t0, 1),
           "full_chars": full_chars}
    if not brief:
        rec.update(label="scout (fell back to the full context)", scout_chars=None, why=why)
        return full, rec
    top = _hints_block(hint_items[:SCOUT_HINTS])
    user = ("PROJECT BRIEF (written by a local model that read the whole project context; check its file:line claims against the code "
            f"below and say when something is not shown):\n{brief}\n\n" + (f"{top}\n\n" if top else "") + tail)
    rec.update(scout_chars=len(brief) + len(top), saved_chars=full_chars - len(brief) - len(top), text=brief[:4000])
    return user, rec


_CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php", ".cs", ".sql", ".sh")
_STOP = {"which", "where", "what", "does", "function", "functions", "class", "method", "file", "code", "that", "this", "with",
         "from", "into", "have", "there", "about", "would", "should", "could", "the", "and", "for", "how", "is", "are", "a", "an", "in"}


_HINTS_HEAD = "RELEVANT CODE FOUND IN THE PROJECT (name it with file:line when you answer):\n\n"


def _hints_block(items):
    return (_HINTS_HEAD + "\n\n".join(items)) if items else ""


def code_hints(e, question, limit=6):
    """Before the chat model answers: find the code the question is about. A name match (function and class names that share word
    stems with the question, so 'checks that a plan is valid' finds validate_plan) plus the code index, code files only. Seen live
    (2026-11-04): asked which function checks a plan, the chat guessed 'the one that runs the tests'. -> text block, or ""."""
    return _hints_block(hint_items(e, question, limit))


def hint_items(e, question, limit=6):
    """code_hints as a list, best first: one "path:line\\n<6 lines>" string per hit."""
    import re as _re
    words = [w.lower() for w in _re.findall(r"[A-Za-z_]{4,}", question) if w.lower() not in _STOP]
    stems = {w[:5] for w in words}
    if not stems:
        return []
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
    return [f"{rel}:{ln}\n" + "\n".join(body) for _, rel, ln, body in hits[:limit]]


def _reply(root, text, role, use=None, effort=None, ctx_size=None, ultra=False, web=False):
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
        want = CTX_SIZES.get(ctx_size or "normal", 14000)
        cap = want if (use or "").startswith("claude") else min(e.effective_context_chars(), want)   # Claude reads 1M tokens; a local model only its own window
        ctx = e.context(text, cap) if core.list_files(e.root, e.cfg) else "(the project folder is empty)"
        hist = "\n".join(f"{'Person' if m['who'] == 'you' else 'You'}: {m['text'][:1500]}" for m in t["messages"][-7:-1])
        items = hint_items(e, text) if core.list_files(e.root, e.cfg) else []
        to_claude = arch_cloud.is_cloud(e.cfg["roles"].get(role))
        t0 = time.time()
        user, scout_rec = compose(text, ctx, items, hist, e.cfg, to_claude, worker=e.tier_model("worker"))
        before = arch_cloud.spent()["usd"]
        if use and use.startswith("claude"):
            def _sink(piece):
                with _LOCK:
                    t["partial"] = (t.get("partial") or "") + piece
                    t["seq"] += 1
            eff = arch_cloud.effort_for(text, effort)
            if ultra or "ultracode" in text.lower():                     # a team of Claude agents, then one lead answer
                with arch_cloud.asked_for():
                    r = arch_cloud.ultra(e.cfg["roles"][role], [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                                         core.load_config(), eff, _sink, web=web)
                r["model"] = e.cfg["roles"][role] + f" · ultra ({r.get('agents', 0)} agents)"
            else:
                with arch_cloud.asked_for(_sink, eff, web):
                    r = e.call(role, user, extra_system=SYSTEM, label="studio", think=think)
        elif to_claude:                                                  # Use Claude = chat/all: still a chat answer the person sent
            with arch_cloud.asked_for(None, None, web):
                r = e.call(role, user, extra_system=SYSTEM, label="studio", think=think)
        else:
            r = e.call(role, user, extra_system=SYSTEM, label="studio", think=think)
        reply = r["text"].strip() or "(no reply)"
        usd = r.get("usd") if r.get("agents") else arch_cloud.spent()["usd"] - before
        calls = r.get("calls") or (
            [{"label": "answer", "model": r.get("model"), "in": r.get("prompt_tokens"), "out": r.get("gen_tokens"),
              "secs": round(time.time() - t0, 1), "thinking": (r.get("thinking") or "")[:4000]}] if r.get("model") else [])
        if scout_rec:                                                    # Show work: the free local step first, with what it saved
            calls = [scout_rec] + list(calls)
        searches = sum(int(c.get("web_searches") or 0) for c in calls if isinstance(c, dict))
        msg = {"who": "model", "text": reply, "model": r["model"], "t": time.time(), "secs": round(time.time() - t0, 1),
               "usd": round(usd, 5) if usd else None, "calls": calls}
        if scout_rec:
            msg["scout"] = {k: scout_rec.get(k) for k in ("scout_chars", "full_chars", "saved_chars", "why")}
        if searches:
            msg["web_searches"] = searches
        with _LOCK:
            t["messages"].append(msg)
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
