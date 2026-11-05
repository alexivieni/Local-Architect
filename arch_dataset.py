"""Training data from your own runs: the one place where "it gets better the more you use it" becomes a file you can train on.

Every model call is traced (system prompt, user turn, reply) and then MARKED by the code checks -- tests, verifiers, the fake-code gate,
the scanners. That label is the only reason local training data can be trusted, so everything here is built on it and nothing here asks a
model what it thinks of itself.

Four kinds come out:

  SFT            one chat sample per accepted reply: {"messages": [system, user, assistant]}. Accepted means the checks ran and passed.
  DPO            a step that failed and then passed is a preference pair. The prompt is the FIRST try's prompt -- the clean task, with no
                 failure text in it -- because that is the prompt the model will actually see next time. (Using the corrected try's
                 prompt, which carries "THE TESTS FAILED: ...", trains the model on a situation that never occurs at inference. That is
                 what this file used to do.)
  distillation   when the worker failed a step and a bigger tier then passed it, the bigger model's accepted reply is exported as an SFT
                 sample aimed at the worker. On one laptop, the teacher and the student are the same hardware: the small model learns
                 exactly the cases it keeps getting wrong, from a teacher that ran here.
  trajectories   a whole tool loop as one multi-turn sample: the model's tool calls, each tool's real result, and the final answer. Linked
                 by the call id each tool record carries, not by guessing from timestamps.

What is deliberately left out, and why:
  * a reply whose checks COULD NOT RUN (mark `accepted: null`). Not a failure, so not a bad example -- and not verified, so not a good one.
  * anything containing something that looks like a credential. The screen runs over the system prompt, the user turn and the reply.
  * two tries that do not share a task. A retry appends its failure to the same prompt, so the two prompts share a long prefix; two
    unrelated questions do not. Without that check, any two unlabelled calls by one role in one run looked like a preference pair.
  * samples too long to train on a small card.

Output goes to `hub_data/datasets/`, per role AND per tier (`worker.sft.jsonl`, `tier-worker.sft.jsonl`, ...), because a tier is what you
actually fine-tune: one adapter for the worker, trained on everything the worker is asked to do. Files from an earlier export that this one
did not write are removed, so what is on disk is always one consistent set. `RECIPE.md` beside them holds the exact commands.

Nothing here trains anything, downloads anything or sends anything anywhere. It reads `.arch/runs/*/trace.jsonl` and writes JSONL.
"""
import collections
import hashlib
import json
import os
import re

import arch_core as core

MAX_CHARS = 24000          # ~6k tokens: fits a 4k-8k training window with the reply; longer samples are dropped and counted
MIN_TASK_PREFIX = 200      # two tries must share at least this much of their prompt to count as the same task
TIERS = ("router", "worker", "heavy", "draft")

# A credential must never reach a training file: the file is the one artefact a person is most likely to copy to another machine.
SECRET = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
]


def has_secret(*texts):
    """True when any of these texts looks like it carries a credential."""
    for t in texts:
        if not t:
            continue
        for rx in SECRET:
            if rx.search(t):
                return True
    return False


# ------------------------------------------------------------------ finding and reading runs

def runs_of(root):
    d = os.path.join(root, ".arch", "runs")
    try:
        return [os.path.join(d, r) for r in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, r, "trace.jsonl"))]
    except OSError:
        return []


def roots(root, include_portfolio=False, include_eval_training=True):
    """The project folders to read. Live eval runs of TRAINING tasks are kept in their own root (`arch_eval.kept_runs_dir("train")`)
    because each task's folder is thrown away; benchmark and held-out tasks are kept elsewhere and never read here."""
    out = [os.path.abspath(root)]
    if include_eval_training:
        try:
            import arch_eval
            t = arch_eval.kept_runs_dir("train")
            if os.path.isdir(os.path.join(t, ".arch", "runs")) and os.path.abspath(t) not in out:
                out.append(os.path.abspath(t))
        except Exception:
            pass
    if include_portfolio:
        pf = core.load_json(os.path.join(core.HOME, "portfolio.json")) or {}
        for app in (pf.get("apps") or pf.get("portfolio") or []):
            p = (app or {}).get("path") or (app or {}).get("folder")
            if p and os.path.isdir(p) and os.path.abspath(p) not in out:
                out.append(os.path.abspath(p))
    return out


def read_trace(path):
    """-> (calls, marks). Kept for callers that only want those two; `read_full` also returns tool calls and escalations."""
    full = read_full(path)
    return full["calls"], full["marks"]


def read_full(path):
    """One pass over a trace. -> {"calls", "marks", "tools", "escalations", "verdict"}. A broken line is skipped, never fatal."""
    out = {"calls": {}, "marks": {}, "tools": [], "escalations": [], "verdict": None, "claude": 0, "assisted": set()}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                t = j.get("type")
                if t == "call" and j.get("id") is not None:
                    if j.get("transport") == "claude":          # cloud replies never become training data (provider terms)
                        out["claude"] += 1
                        continue
                    out["calls"][j["id"]] = j
                elif t == "mark" and j.get("id") is not None:
                    out["marks"][j["id"]] = j                   # a later mark on the same call wins, as it should
                elif t == "tool":
                    out["tools"].append(j)
                elif t == "escalation":
                    out["escalations"].append(j)
                elif t == "verdict":
                    out["verdict"] = j
                elif t == "assist" and j.get("id") is not None:  # code repaired the wiring for this try (arch_flows.wire_contract)
                    out["assisted"].add(j["id"])
    except OSError:
        pass
    return out


def _step_key(label):
    """'sprint2-try3' -> 'sprint2'; 'write-cand2' -> 'write'; 'design-revise-2' -> 'design-revise'; no attempt number = its own step."""
    s = str(label or "")
    s = re.sub(r"[-_]?(try|cand)\d+$", "", s)          # retries of one step, and best-of-N candidates for one step
    s = re.sub(r"[-_]\d+$", "", s)
    return s


def _same_task(a, b):
    """Do two prompts address the same task? A retry re-sends the whole prompt and appends its failure, so the two share a long prefix.
    Two unrelated questions do not. This is what stops unlabelled calls from being paired with each other."""
    if not a or not b:
        return False
    n = min(len(a), len(b))
    need = min(MIN_TASK_PREFIX, max(40, int(n * 0.6)))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i >= need


def _turns(call):
    """-> (system, user) from a traced call, for either message shape (system+user, or everything folded into one user turn)."""
    msgs = call.get("messages") or []
    sys_t = next((x.get("content", "") for x in msgs if x.get("role") == "system"), "")
    user = next((x.get("content", "") for x in reversed(msgs) if x.get("role") == "user"), "")
    return sys_t, user


# ------------------------------------------------------------------ building the sets

def build(root_list, roles=None, max_chars=MAX_CHARS, pairs="same-task", min_quality=0.0, dedupe=True):
    """Read every run under these roots. -> {"sft", "dpo", "distill", "traj", "stats", "report"}.

    `min_quality` (0-1, default 0 = keep all) drops accepted samples scoring below it. `dedupe` keeps only the best-scoring sample per
    near-identical task (whitespace-normalised, first 2000 characters of the user turn, per role).

    `roles` limits which roles are exported. `pairs` controls how strict preference pairs are: "same-task" (the default) needs the two
    tries to share a task, "all" keeps every failed-then-passed pair in one step and labels each with whether the task matched.
    """
    sft, dpo, distill = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
    traj = []
    stats = collections.Counter()
    seen_sft, seen_dpo = set(), set()
    cands = {}              # (role, task key) -> the best accepted sample so far

    for root in root_list:
        for run in runs_of(root):
            stats["runs"] += 1
            try:                                       # a kept eval run whose HIDDEN checks did not pass teaches nothing, whatever
                with open(os.path.join(run, "eval_task.json"), encoding="utf-8") as _f:   # its in-run gates said (review 2026-11-05)
                    if (json.load(_f) or {}).get("verdict") != "pass":
                        stats["eval_not_passed"] += 1
                        continue
            except (OSError, ValueError):
                pass
            full = read_full(os.path.join(run, "trace.jsonl"))
            calls, marks = full["calls"], full["marks"]
            run_name = os.path.basename(run)
            by_step = collections.defaultdict(list)

            stats["claude"] += full.get("claude", 0)           # cloud replies, dropped while reading
            escalated_steps = {((e.get("role") or "?"), _step_key(e.get("step"))) for e in full["escalations"]}
            tries_seen, first_tier = collections.Counter(), {}

            for cid in sorted(calls):
                c = calls[cid]
                role = c.get("role") or "?"
                if roles and role not in roles:
                    continue
                if _is_cache_hit(c):
                    stats["cached"] += 1               # a reply reused from the cache was already exported when it was first produced
                    continue
                m = marks.get(cid)
                if not m:
                    stats["unmarked"] += 1
                    continue
                verdict = m.get("accepted")
                if verdict is None:
                    stats["unverified"] += 1           # the checks could not run: it teaches nothing in either direction
                    stats["not_sure"] += 1
                    continue
                if verdict and cid in full.get("assisted", ()):
                    stats["assisted"] += 1             # it passed only after code fixed its wiring: the reply alone is not the right answer
                    continue
                skey = (role, _step_key(c.get("label")))
                try_no = tries_seen[skey]              # how many tries of this step came before this one
                tries_seen[skey] += 1
                first_tier.setdefault(skey, c.get("tier") or "worker")
                sys_t, user = _turns(c)
                reply = c.get("reply") or ""
                if not user or not reply:
                    stats["empty"] += 1
                    continue
                if has_secret(sys_t, user, reply):
                    stats["secrets_skipped"] += 1
                    continue
                if len(sys_t) + len(user) + len(reply) > max_chars:
                    stats["too_long"] += 1
                    continue
                tier = c.get("tier") or "worker"
                rec = {"accepted": bool(verdict), "system": sys_t, "user": user, "reply": reply, "model": c.get("model"),
                       "tier": tier, "id": cid, "flow": c.get("flow"), "prompt_ref": c.get("prompt_ref") or "", "call": c}
                by_step[skey].append(rec)

                if verdict:
                    diff = difficulty(c, reply, try_no + 1, skey in escalated_steps or tier != first_tier[skey])
                    q = quality_score(c, reply, try_no, max_chars)
                    if q < min_quality:
                        stats["low_quality"] += 1
                        continue
                    row = _sample(sys_t, user, reply, role, tier, c, run_name, source="accepted")
                    row["meta"].update({"quality": q, "difficulty": diff, "tags": _tags(diff)})
                    key = (role, near_key(user)) if dedupe else (role, hashlib.sha1((user + "\n#\n" + reply).encode("utf-8", "replace")).hexdigest())
                    old = cands.get(key)
                    if old is not None:
                        stats["duplicates"] += 1       # the same task again: keep the better sample, count the other as dropped
                        stats["dup"] += 1
                        if q >= old["meta"]["quality"]:     # a tie goes to the newer one, which has seen more of the project's history
                            cands[key] = row
                        continue
                    cands[key] = row
                else:
                    stats["rejected"] += 1

            # ---- preference pairs: the same step failed, then passed
            for (role, step), tries in by_step.items():
                bad = [t for t in tries if not t["accepted"]]
                good = [t for t in tries if t["accepted"]]
                if not (bad and good):
                    continue
                g = good[-1]
                for b in bad:
                    if b["reply"].strip() == g["reply"].strip():
                        continue
                    same = _same_task(b["user"], g["user"])
                    if pairs != "all" and not same:
                        stats["pairs_dropped_different_task"] += 1
                        continue
                    key = hashlib.sha1((b["user"] + "\n#\n" + b["reply"] + "\n#\n" + g["reply"]).encode("utf-8", "replace")).hexdigest()
                    if key in seen_dpo:
                        stats["duplicates"] += 1
                        continue
                    seen_dpo.add(key)
                    # The prompt is the FAILED try's: the clean task, as it will arrive at inference. The corrected try's prompt carries
                    # the failure text, which never appears when the model is actually asked to do the work.
                    dpo[role].append({"prompt": b["user"], "system": b["system"], "chosen": g["reply"], "rejected": b["reply"],
                                      "meta": {"role": role, "step": step, "run": run_name, "same_task": same,
                                               "chosen_model": g["model"], "rejected_model": b["model"],
                                               "chosen_tier": g["tier"], "rejected_tier": b["tier"],
                                               "cross_tier": g["tier"] != b["tier"]}})

            # ---- distillation: the worker could not, a bigger tier could. Teach the worker that answer.
            for esc in full["escalations"]:
                step = _step_key(esc.get("step"))
                role = esc.get("role") or "?"
                if roles and role not in roles:
                    continue
                tries = by_step.get((role, step)) or []
                teacher = next((t for t in reversed(tries) if t["accepted"] and t["model"] == esc.get("to_model")), None)
                student_failed = any((not t["accepted"]) and t["model"] == esc.get("from_model") for t in tries)
                if not (teacher and student_failed):
                    continue
                key = hashlib.sha1(("D" + near_key(teacher["user"]) if dedupe else "D" + teacher["user"] + teacher["reply"]).encode("utf-8", "replace")).hexdigest()
                if key in seen_sft:
                    stats["duplicates"] += 1
                    stats["dup"] += 1
                    continue
                target = esc.get("from_tier") or "worker"
                q = quality_score(teacher["call"], teacher["reply"], max(0, len(tries) - 1), max_chars)
                if q < min_quality:
                    stats["low_quality"] += 1
                    continue
                seen_sft.add(key)
                row = _sample(teacher["system"], teacher["user"], teacher["reply"], role, target, {"flow": esc.get("flow"), "model": teacher["model"]},
                              run_name, source="escalation")
                diff = difficulty(teacher["call"], teacher["reply"], len(tries), True, flow=esc.get("flow"))
                row["meta"].update({"quality": q, "difficulty": diff, "tags": _tags(diff)})
                row["meta"]["taught_by"] = esc.get("to_model")
                row["meta"]["taught_by_tier"] = esc.get("to_tier")
                row["meta"]["for_tier"] = target
                row["meta"]["why"] = (esc.get("why") or "")[:200]
                distill[target].append(row)
                stats["distill"] += 1

            # ---- tool trajectories: one sample per tool loop, rebuilt from the call id each tool record carries
            traj.extend(_trajectories(full, run_name, roles, max_chars, stats))

    for (role, _k), row in cands.items():
        sft[role].append(row)
    stats["sft"] = sum(len(v) for v in sft.values())
    stats["dpo"] = sum(len(v) for v in dpo.values())
    stats["traj"] = len(traj)
    out = {"sft": dict(sft), "dpo": dict(dpo), "distill": dict(distill), "traj": traj, "stats": dict(stats)}
    out["report"] = report(out)
    return out


# ------------------------------------------------------------------ quality, difficulty, the report

_BLOCK = re.compile(r"^(?:FILE|EDIT|DEF):\s*(\S+?)(?:::\S+)?\s*$", re.M)


def _is_cache_hit(call):
    """A reply the reply cache gave back (`cached`, transport 'cache', done_reason 'cached'). NOT `cached_exact`: that one only says the
    server counted its prefix-cache tokens exactly, on a call the model really answered."""
    return bool(call.get("cached")) or call.get("transport") == "cache" or call.get("done_reason") == "cached"


def near_key(user):
    """Same task, near enough: whitespace-normalised, case kept, hash of the WHOLE user turn.
    Prompts are prefix-stable (instructions and project context first, the per-task part last), so the head of a turn is shared by
    every task in a project; keying on it collapsed a role's samples from one project into one. The whole turn carries the task."""
    norm = " ".join(str(user or "").split())
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


def quality_score(call, reply, tries_before=0, max_chars=MAX_CHARS):
    """0..1 from code-side evidence only: the checks passed (0.4, every exported sample earns it), got there on the first try (0.3, then
    0.15, 0.05, 0), the reply was not cut off (0.15), and its length is sane (0.15)."""
    q = 0.4
    q += (0.3, 0.15, 0.05)[tries_before] if 0 <= tries_before < 3 else 0.0
    if (call or {}).get("done_reason") != "length":
        q += 0.15
    n = len(reply or "")
    if 20 <= n <= max_chars // 2:
        q += 0.15
    elif n > 0:
        q += 0.05
    return round(min(1.0, q), 3)


def difficulty(call, reply, tries, escalated, flow=None):
    """What the trace says about how hard this step was: tries needed, a bigger tier involved, files the reply touches, the flow."""
    files = len(set(_BLOCK.findall(reply or "")))
    pts = min(max(tries - 1, 0), 2) + (2 if escalated else 0) + (1 if files >= 3 else 0)
    return {"tries": int(tries), "escalated": bool(escalated), "files": files, "flow": flow or (call or {}).get("flow"),
            "level": "hard" if pts >= 3 else "medium" if pts >= 1 else "easy"}


def _tags(d):
    t = ["first-try" if d["tries"] <= 1 else "retried", "escalated" if d["escalated"] else "own-tier",
         "multi-file" if d["files"] >= 2 else "single-file" if d["files"] == 1 else "no-files", "level-" + d["level"]]
    if d.get("flow"):
        t.append("flow-" + str(d["flow"]))
    return t


def band(q):
    return "high" if q >= 0.8 else "mid" if q >= 0.5 else "low"


def report(data):
    """Counts of what was exported and what was dropped and why. A plain dict; `report_text` prints it."""
    st = data.get("stats") or {}
    rows = [r for v in (data.get("sft") or {}).values() for r in v] + [r for v in (data.get("distill") or {}).values() for r in v]
    by = {k: collections.Counter() for k in ("flow", "role", "tier", "quality", "level")}
    for r in rows:
        m = r.get("meta") or {}
        by["flow"][str(m.get("flow") or "?")] += 1
        by["role"][str(m.get("role") or "?")] += 1
        by["tier"][str(m.get("tier") or "?")] += 1
        by["quality"][band(m.get("quality", 0))] += 1
        by["level"][str((m.get("difficulty") or {}).get("level") or "?")] += 1
    dropped = {"duplicate": st.get("dup", 0), "not_sure": st.get("not_sure", 0), "claude": st.get("claude", 0),
               "low_quality": st.get("low_quality", 0), "cached": st.get("cached", 0), "rejected": st.get("rejected", 0),
               "secrets": st.get("secrets_skipped", 0), "too_long": st.get("too_long", 0)}
    return {"samples": len(rows), "by_flow": dict(by["flow"]), "by_role": dict(by["role"]), "by_tier": dict(by["tier"]),
            "by_quality": dict(by["quality"]), "by_level": dict(by["level"]), "dropped": dropped}


def report_text(rep):
    lines = [f"{rep.get('samples', 0)} training samples kept."]
    for title, key in (("flow", "by_flow"), ("role", "by_role"), ("tier", "by_tier"), ("quality", "by_quality"), ("difficulty", "by_level")):
        if rep.get(key):
            lines.append(f"  by {title}: " + ", ".join(f"{k} {n}" for k, n in sorted(rep[key].items())))
    why = {"duplicate": "same task again (best kept)", "not_sure": "checks could not run", "claude": "cloud replies",
           "low_quality": "below the quality floor", "cached": "reused from the reply cache", "rejected": "rejected by the checks",
           "secrets": "looked like a credential", "too_long": "too long to train on"}
    gone = [f"  dropped {n}: {why[k]}" for k, n in (rep.get("dropped") or {}).items() if n]
    return "\n".join(lines + gone)


def _sample(sys_t, user, reply, role, tier, call, run_name, source="accepted"):
    return {"messages": ([{"role": "system", "content": sys_t}] if sys_t else []) +
                        [{"role": "user", "content": user}, {"role": "assistant", "content": reply}],
            "meta": {"role": role, "tier": tier, "flow": (call or {}).get("flow"), "model": (call or {}).get("model"),
                     "run": run_name, "source": source, "prompt_ref": (call or {}).get("prompt_ref") or ""}}


def _trajectories(full, run_name, roles, max_chars, stats):
    """A tool loop as one multi-turn sample. Only loops that ended in an accepted final answer, so the whole path is known to be good."""
    out = []
    tools_by_call = collections.defaultdict(list)
    for t in full["tools"]:
        if t.get("call") is not None:
            tools_by_call[t["call"]].append(t)
    if not tools_by_call:
        return out
    by_step = collections.defaultdict(list)
    for cid, c in full["calls"].items():
        if c.get("role") and (not roles or c["role"] in roles):
            by_step[(c.get("flow"), c.get("role"))].append(cid)
    for (_flow, role), cids in by_step.items():
        cids = sorted(cids)
        chain = [cid for cid in cids if cid in tools_by_call]
        if not chain:
            continue
        final = next((cid for cid in reversed(cids) if cid not in tools_by_call
                      and (full["marks"].get(cid) or {}).get("accepted") is True), None)
        if final is None:
            stats["traj_unfinished"] += 1
            continue
        first = full["calls"][chain[0]]
        sys_t, user = _turns(first)
        msgs = ([{"role": "system", "content": sys_t}] if sys_t else []) + [{"role": "user", "content": user}]
        size = len(sys_t) + len(user)
        for cid in chain:
            c = full["calls"][cid]
            msgs.append({"role": "assistant", "content": c.get("reply") or ""})
            for t in tools_by_call[cid]:
                res = f"{t.get('tool')} -> " + str(t.get("result") or "")[:1500]
                msgs.append({"role": "tool", "name": str(t.get("tool") or ""), "content": res})
                size += len(res)
            size += len(c.get("reply") or "")
        msgs.append({"role": "assistant", "content": full["calls"][final].get("reply") or ""})
        size += len(full["calls"][final].get("reply") or "")
        if size > max_chars * 2:
            stats["traj_too_long"] += 1
            continue
        if has_secret(*[m.get("content", "") for m in msgs]):
            stats["secrets_skipped"] += 1
            continue
        out.append({"messages": msgs, "meta": {"role": role, "tier": first.get("tier") or "worker", "flow": first.get("flow"),
                                               "run": run_name, "source": "tool_loop", "turns": len(chain)}})
    return out


# ------------------------------------------------------------------ writing

def write(data, out_dir):
    """Write every set, remove anything a previous export left behind, and return the summary."""
    os.makedirs(out_dir, exist_ok=True)
    written, wrote_files = {}, set()

    def dump(name, rows):
        p = os.path.join(out_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        written[name[:-6]] = len(rows)
        wrote_files.add(name)

    by_tier = collections.defaultdict(list)
    for role, rows in (data.get("sft") or {}).items():
        dump(f"{role}.sft.jsonl", rows)
        for r in rows:
            by_tier[r["meta"].get("tier") or "worker"].append(r)
    for tier, rows in (data.get("distill") or {}).items():
        dump(f"distill-{tier}.sft.jsonl", rows)
        by_tier[tier].extend(rows)
    for tier, rows in sorted(by_tier.items()):
        dump(f"tier-{tier}.sft.jsonl", rows)             # what you actually fine-tune: one adapter per tier

    dpo_by_tier = collections.defaultdict(list)
    for role, rows in (data.get("dpo") or {}).items():
        dump(f"{role}.dpo.jsonl", rows)
        for r in rows:
            dpo_by_tier[r["meta"].get("rejected_tier") or "worker"].append(r)
    for tier, rows in sorted(dpo_by_tier.items()):
        dump(f"tier-{tier}.dpo.jsonl", rows)
    if data.get("traj"):
        dump("trajectories.jsonl", data["traj"])

    stale = []
    for fn in sorted(os.listdir(out_dir)):
        if fn.endswith(".jsonl") and fn not in wrote_files:
            try:
                os.remove(os.path.join(out_dir, fn))
                stale.append(fn)
            except OSError:
                pass

    roles_seen = set(data.get("sft") or {}) | set(data.get("dpo") or {})
    rep = data.get("report") or report(data)
    with open(os.path.join(out_dir, "REPORT.txt"), "w", encoding="utf-8") as f:
        f.write(report_text(rep) + "\n")
    summary = {"stats": data["stats"], "report": rep, "files": written, "removed_stale": stale,
               "per_role": {r: {"sft": len((data.get("sft") or {}).get(r, [])), "dpo": len((data.get("dpo") or {}).get(r, []))} for r in roles_seen},
               "per_tier": {t: {"sft": len(rows), "dpo": len(dpo_by_tier.get(t, []))} for t, rows in by_tier.items()}}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    with open(os.path.join(out_dir, "RECIPE.md"), "w", encoding="utf-8") as f:
        f.write(recipe(summary, out_dir))
    return summary


def recipe(summary, out_dir):
    st = summary.get("stats") or {}
    n_sft, n_dpo, n_tr = st.get("sft", 0), st.get("dpo", 0), st.get("traj", 0)
    per_tier = summary.get("per_tier") or {}
    best = max(per_tier.items(), key=lambda kv: kv[1]["sft"])[0] if per_tier else "worker"
    biggest = os.path.join(out_dir, f"tier-{best}.sft.jsonl")
    lines = [
        "# Training the worker on your own runs",
        "",
        "These files came from your runs. Every sample in them is a reply that the code checks accepted: tests passed, the verifier",
        "agreed, nothing fake. Replies whose checks could not run were left out, and so was anything that looked like a credential.",
        "",
        f"- SFT samples: **{n_sft}**   preference pairs: **{n_dpo}**   tool trajectories: **{n_tr}**   taught by a bigger tier: **{st.get('distill', 0)}**",
        f"- Train the **{best}** tier from `{os.path.basename(biggest)}` ({per_tier.get(best, {}).get('sft', 0)} samples).",
        "",
        "## Is there enough yet?",
        "",
        "Below about 200 samples a LoRA mostly learns your formatting, which is still worth having: fewer malformed edit blocks means",
        "fewer wasted turns. Past about 1000 it starts learning how this project is written. Keep using the app; the files grow.",
        "",
        "## What to train, in order of value",
        "",
        "1. `tier-worker.sft.jsonl` -- everything the worker got right. This is the one to start with.",
        f"2. `distill-worker.sft.jsonl` -- {st.get('distill', 0)} cases the worker failed and a bigger model on this same machine solved.",
        "   These are the most valuable samples you have: they are exactly what your worker cannot yet do.",
        "3. `tier-worker.dpo.jsonl` -- after the SFT adapter is in place, preference tuning on your own corrected mistakes.",
        "   Check `meta.same_task` is true on the pairs you use.",
        "4. `trajectories.jsonl` -- multi-turn tool use, if you want the worker better at choosing tools.",
        "",
        "## The run (one card, offline after the base model is downloaded once)",
        "",
        "```bash",
        "pip install unsloth trl datasets          # once; needs a network, unlike everything else here",
        "python train_lora.py --data " + biggest.replace("\\", "/") + " \\",
        "    --base unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit \\",
        "    --out hub_data/adapters/worker-lora --epochs 2 --max-seq 4096",
        "```",
        "",
        "Pick the base model that matches the worker you are actually running, or the adapter will not fit it. A 7B at 4-bit trains in",
        "roughly 2-4 hours on a 6 GB card at a 4096 window; drop `--max-seq` to 2048 if it runs out of memory.",
        "",
        "## Then make it a model this app can use",
        "",
        "```bash",
        "python -m llama_cpp.convert_lora --adapter hub_data/adapters/worker-lora --out hub_data/adapters/worker-lora.gguf",
        "```",
        "",
        "```bash",
        "ollama create my-worker -f Modelfile",
        "```",
        "",
        "with a `Modelfile` of:",
        "",
        "```",
        "FROM qwen2.5-coder:7b",
        "ADAPTER ./hub_data/adapters/worker-lora.gguf",
        "```",
        "",
        "## Prove it is actually better before you keep it",
        "",
        "```bash",
        "python architect.py eval --quick --label candidate",
        "```",
        "",
        "```bash",
        "python architect.py promote-model --tier worker --model my-worker --compare baseline",
        "```",
        "",
        "The second command refuses to switch your worker over unless the fixed task set says the new model is no worse: not a lower",
        "pass rate, not meaningfully slower, not more tokens. A model that only feels better does not get promoted.",
    ]
    return "\n".join(lines) + "\n"
