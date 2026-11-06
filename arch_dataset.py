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
  * Claude work: a run with any `claude` transport or `claude:` model call, and every run arch_learn.exclude_from_training() names.
  * the ledger's FORMAT REFERENCE few-shot block in a prompt, unless it quotes this same local model's own reply (else it is cut off).
  * SFT/distillation/trajectories from a real-project run whose final verdict is not "pass" (its in-run DPO pairs stay).
  * a reply with an unterminated <think, or cut off at its limit (done_reason "length"); complete <think> blocks go to meta.thinking.
  * any path under evals/runs/bench, and an eval run whose eval_task.json does not parse (fail closed).

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
    # keyword = LITERAL value. The name may carry prefixes/suffixes joined by underscores (aws_secret_access_key, DB_PASSWORD, auth_token),
    # an optional closing quote (JSON keys), then : or = (not ==). The value must be a literal: a quoted string of 8+ characters, or an
    # unquoted run of 8+ that holds a digit and is not followed by '(' '.' '[' (a call or an attribute: password = get_password()).
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_]*(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token|client[_-]?secret)"
               r"[A-Za-z0-9_]*['\"]?\s*[:=](?!=)\s*(?:(['\"])(?![<{$%])[^'\"\s]{8,}\1|(?=[A-Za-z0-9+/_\-]*\d)(?=[A-Za-z0-9+/_\-]*[A-Za-z])[A-Za-z0-9+/_\-]{8,}(?![\w.(\[]))"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\bauthorization['\"]?\s*[:=]\s*['\"]?basic\s+[A-Za-z0-9+/]{8,}={0,2}"),
]

THINK_DONE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>\s*", re.S)
FORMAT_REF = "\n\nFORMAT REFERENCE ("           # what arch_core.Engine._call appends to the end of a user turn (arch_ledger.example_for)


def split_think(reply):
    """<think> policy for training (one policy, documented in RECIPE.md): complete <think>...</think> blocks are cut out of the reply and
    returned separately (kept in meta.thinking, never trained as the answer); a reply with an opening '<think' and no close is unusable.
    -> (reply_without_thinking, thinking_text, unterminated)."""
    r = reply or ""
    thinking = "\n".join(m.strip() for m in THINK_DONE.findall(r))
    rest = THINK_DONE.sub("", r)
    return rest, thinking, bool(re.search(r"<think", rest, re.I))


def strip_format_ref(user, model, examples=None):
    """The ledger's few-shot block at the end of a user turn. Kept only when the example it quotes is verifiably this SAME local model's
    own reply (found in the ledger with that model); otherwise cut off, because it may be another model's (or Claude's) text.
    -> (user, "none" | "kept" | "stripped")."""
    i = (user or "").find(FORMAT_REF)
    if i < 0:
        return user, "none"
    quoted = user[i:].split("):\n", 1)[-1].strip()
    if examples is None:
        try:
            import arch_ledger
            examples = arch_ledger.load().get("_examples") or []
        except Exception:
            examples = []
    m = str(model or "")
    if quoted and not m.startswith(("claude:", "fake:")):
        for e in examples:
            if e.get("model") == m and (e.get("reply") or "").strip() == quoted:
                return user, "kept"
    return user[:i].rstrip(), "stripped"


def _claude_run_ids():
    """The Logbook's Claude-made task ids (arch_learn.exclude_from_training), as normalised run folders. Trace tasks are 'trace:<abs run dir>'."""
    try:
        import arch_learn
        ids = arch_learn.exclude_from_training()
    except Exception:
        return set()
    out = set()
    for i in ids or ():
        s = str(i)
        out.add(s)
        if s.startswith("trace:"):
            out.add(os.path.normcase(os.path.abspath(s[6:])))
    return out


def _is_bench_path(p):
    return "/evals/runs/bench" in os.path.normcase(os.path.abspath(p)).replace("\\", "/").lower()


def template_overlap(tasks=None):
    """Holdout-by-template self-check: template ids that appear in BOTH a training task and a bench (or holdout) task. -> sorted list.
    A non-empty list means the benchmark measures templates the adapter was trained on (only other variants of them)."""
    import arch_eval
    if tasks is None:
        tasks = list(arch_eval.load_tasks())
        try:
            import importlib.util
            p = os.path.join(arch_eval.EVALS_DIR, "families", "distributed.py")
            spec = importlib.util.spec_from_file_location("evals.families.distributed_all", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            tasks += [dict(t, family="distributed") for t in mod.all_tasks()]
        except Exception:
            pass
    tr, be = set(), set()
    for t in tasks:
        k = t.get("template")
        if not k:
            continue
        k = (t.get("family") or t.get("flow") or "?") + ":" + str(k)
        (tr if arch_eval.bucket_for(t) == "train" else be).add(k)
    return sorted(tr & be)


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


SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env", "site-packages", "AppData", "$Recycle.Bin", ".cache",
             ".npm", ".cargo", ".rustup", ".nuget", ".gradle", ".m2", "dist", "build"}


def discover(base, depth=6):
    """Project folders under `base` (at most `depth` levels down) that hold `.arch/runs/<run>/trace.jsonl`. Bounded and read-only:
    dependency folders, VCS folders and AppData (where the self-tests' throwaway homes live) are never entered."""
    out, stack = [], [(os.path.abspath(base), 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            names = os.listdir(d)
        except OSError:
            continue
        if ".arch" in names and runs_of(d):
            out.append(d)
        if lvl >= depth:
            continue
        for n in names:
            if n in SKIP_DIRS or n.startswith("."):
                continue
            p = os.path.join(d, n)
            if os.path.isdir(p) and not os.path.islink(p):
                stack.append((p, lvl + 1))
    return sorted(out)


def is_bench(root):
    """True for the benchmark bucket (benchmark families and held-out tasks): never a training root."""
    try:
        import arch_eval
        b = arch_eval.kept_runs_dir("bench")
    except Exception:
        return False
    return os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(b))


def roots(root, include_portfolio=False, include_eval_training=True, include_discovered=False, base=None, depth=6):
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
    if include_discovered:
        for p in discover(base or os.path.expanduser("~"), depth):
            if p not in out:
                out.append(p)
    return [p for p in out if not is_bench(p)]     # the holdout is never read, however the root list was built


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

def _lesson(et):
    """The curriculum lesson of a kept eval run ("lesson:<id>" in its tags), or ""."""
    for t in (et or {}).get("tags") or ():
        if str(t).startswith("lesson:"):
            return str(t)[7:]
    return ""


def _failed_by_task(root_list):
    """Task id -> the prompts the worker saw in kept TRAIN runs of that task whose hidden checks did NOT pass: [{role, user, model, tier, run}].
    The student half of a task-level distillation pair. Bench paths and unreadable eval_task.json files are skipped (fail closed)."""
    out = collections.defaultdict(list)
    for root in root_list:
        if is_bench(root) or _is_bench_path(root):
            continue
        for run in runs_of(root):
            if _is_bench_path(run):
                continue
            try:
                with open(os.path.join(run, "eval_task.json"), encoding="utf-8") as f:
                    et = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(et, dict) or et.get("bucket") != "train" or et.get("verdict") != "fail":
                continue
            full = read_full(os.path.join(run, "trace.jsonl"))
            if full.get("claude"):
                continue
            for c in full["calls"].values():
                mdl = str(c.get("model") or "")
                if not mdl or mdl.startswith(("fake:", "claude:")) or _is_cache_hit(c):
                    continue
                _s, user = _turns(c)
                user, _f = strip_format_ref(user, mdl, None)
                if user:
                    out[et.get("id")].append({"role": c.get("role") or "?", "user": user, "model": mdl, "tier": c.get("tier") or "worker",
                                              "run": os.path.basename(run)})
    return out


def _is_teacher_run(et, model):
    """A kept passing run counts as a teacher's when its label says so (tools/train_traces.py --curriculum labels the heavy retry
    "teacher") or its model is the configured heavy tier's."""
    if str((et or {}).get("label") or "").startswith("teacher"):
        return True
    try:
        h = configured_tiers().get("heavy")
    except Exception:
        h = None
    return bool(h) and base_matches(model, h) if h else False


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

    claude_ids = _claude_run_ids()
    failed_prompts = _failed_by_task(root_list)
    try:
        import arch_ledger
        ledger_examples = arch_ledger.load().get("_examples") or []
    except Exception:
        ledger_examples = []
    for root in root_list:
        if is_bench(root) or _is_bench_path(root):
            stats["bench_skipped"] += len(runs_of(root))
            continue
        for run in runs_of(root):
            stats["runs"] += 1
            if _is_bench_path(run):
                stats["bench_skipped"] += 1            # any path under evals/runs/bench is the holdout, wherever the root list came from
                continue
            if os.path.normcase(os.path.abspath(run)) in claude_ids:
                stats["claude_runs"] += 1              # the Logbook marks this task as Claude's (arch_learn.exclude_from_training)
                continue
            et = None
            et_path = os.path.join(run, "eval_task.json")
            if os.path.exists(et_path):
                try:
                    with open(et_path, encoding="utf-8") as _f:
                        et = json.load(_f)
                    if not isinstance(et, dict):
                        raise ValueError("eval_task.json is not an object")
                except (OSError, ValueError):
                    stats["eval_task_unreadable"] += 1 # fail closed: an eval run that cannot be classified might be the holdout
                    continue
                if et.get("bucket") and et.get("bucket") != "train":
                    stats["bench_skipped"] += 1        # a benchmark/holdout run copied anywhere else is still the holdout
                    continue
                if et.get("verdict") != "pass":        # a kept eval run whose HIDDEN checks did not pass teaches nothing (review 2026-11-05)
                    stats["eval_not_passed"] += 1      # counted, so the report shows what the hidden-check rule withholds
                    stats["eval_not_passed_accepted_marks"] += sum(
                        1 for m in read_full(os.path.join(run, "trace.jsonl"))["marks"].values() if m.get("accepted") is True)
                    continue
            lesson = _lesson(et)
            full = read_full(os.path.join(run, "trace.jsonl"))
            calls, marks = full["calls"], full["marks"]
            run_name = os.path.basename(run)
            by_step = collections.defaultdict(list)
            n_claude = full.get("claude", 0) + sum(1 for c in calls.values() if str(c.get("model") or "").startswith("claude:"))
            if n_claude:
                stats["claude_runs"] += 1              # Claude took part in this run: none of it is local work to fine-tune on
                stats["claude"] += n_claude
                continue
            # Real-project runs (no eval_task.json): an SFT sample needs the RUN to have passed, not only its own step. Preference pairs
            # inside a failed run stay: "this try failed, the next try of the same step passed" holds whatever the run's ending was.
            run_pass = et is not None or (full.get("verdict") or {}).get("verdict") == "pass"

            escalated_steps = {((e.get("role") or "?"), _step_key(e.get("step"))) for e in full["escalations"]}
            tries_seen, first_tier = collections.Counter(), {}

            for cid in sorted(calls):
                c = calls[cid]
                role = c.get("role") or "?"
                if roles and role not in roles:
                    continue
                mdl = str(c.get("model") or "")
                if mdl.startswith("fake:"):
                    stats["fixture"] += 1              # the scripted test stand-in: its replies are fixtures, not work
                    continue
                if mdl.startswith("claude:"):
                    stats["claude"] += 1               # Claude-written whatever transport recorded it: never fine-tuning data
                    continue
                if _is_cache_hit(c):
                    stats["cached"] += 1              # a reply reused from the cache was already exported when it was first produced
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
                reply, thinking, unterminated = split_think(c.get("reply") or "")
                if unterminated:
                    stats["think_unterminated"] += 1   # an opened <think> that never closed: the answer is not there to learn
                    continue
                user, fref = strip_format_ref(user, mdl, ledger_examples)
                if fref != "none":
                    stats["format_ref_" + fref] += 1   # the few-shot block is someone else's text unless it is this model's own
                if not user or not reply.strip():
                    stats["empty"] += 1
                    continue
                if has_secret(sys_t, user, reply):
                    stats["secrets_skipped"] += 1
                    continue
                if len(sys_t) + len(user) + len(reply) > max_chars:
                    stats["too_long"] += 1
                    continue
                tier = c.get("tier") or "worker"
                rec = {"accepted": bool(verdict), "system": sys_t, "user": user, "reply": reply, "thinking": thinking, "model": c.get("model"),
                       "tier": tier, "id": cid, "flow": c.get("flow"), "prompt_ref": c.get("prompt_ref") or "", "call": c}
                by_step[skey].append(rec)

                if verdict and not run_pass:
                    stats["run_not_passed"] += 1       # the step passed but the run did not: kept only as the winning half of a pair
                elif verdict and c.get("done_reason") == "length":
                    stats["truncated"] += 1            # cut off at its token limit: not a whole answer to imitate
                elif verdict:
                    diff = difficulty(c, reply, try_no + 1, skey in escalated_steps or tier != first_tier[skey])
                    q = quality_score(c, reply, try_no, max_chars)
                    if q < min_quality:
                        stats["low_quality"] += 1
                        continue
                    row = _sample(sys_t, user, reply, role, tier, c, run_name, source="accepted")
                    row["meta"].update({"quality": q, "difficulty": diff, "tags": _tags(diff)})
                    if lesson:
                        row["meta"]["lesson"] = lesson
                    if thinking:
                        row["meta"]["thinking"] = thinking[:4000]
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
                                               "cross_tier": g["tier"] != b["tier"], **({"lesson": lesson} if lesson else {})}})

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
                if not run_pass or (teacher["call"] or {}).get("done_reason") == "length":
                    stats["distill_withheld"] += 1     # same SFT rules as above: a passed run, a whole reply
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
                row["meta"]["for_model"] = esc.get("from_model")   # the student: the per-model tier file is the one it goes into
                row["meta"]["why"] = (esc.get("why") or "")[:200]
                row["meta"]["teacher_model"] = esc.get("to_model")
                row["meta"]["student_model"] = esc.get("from_model")
                if lesson:
                    row["meta"]["lesson"] = lesson
                distill[target].append(row)
                stats["distill"] += 1

            # ---- task-level distillation: the worker FAILED this training task's hidden checks in one kept run, and a local teacher
            # (the heavy model, a retry labelled "teacher") PASSED it in this one. Pair the prompts they shared; teach the worker the teacher's reply.
            if et is not None and et.get("bucket") == "train" and et.get("id") in failed_prompts:
                for skey, tries in by_step.items():
                    for t in tries:
                        if not t["accepted"] or (t["call"] or {}).get("done_reason") == "length" or not _is_teacher_run(et, t["model"]):
                            continue
                        stu = next((f for f in failed_prompts[et["id"]] if f["role"] == skey[0] and f["model"] != t["model"]
                                    and _same_task(f["user"], t["user"])), None)
                        if not stu:
                            continue
                        key = hashlib.sha1(("D" + near_key(t["user"]) if dedupe else "D" + t["user"] + t["reply"]).encode("utf-8", "replace")).hexdigest()
                        if key in seen_sft:
                            stats["duplicates"] += 1
                            stats["dup"] += 1
                            continue
                        q = quality_score(t["call"], t["reply"], 0, max_chars)
                        if q < min_quality:
                            stats["low_quality"] += 1
                            continue
                        seen_sft.add(key)
                        target = stu["tier"]
                        row = _sample(t["system"], t["user"], t["reply"], skey[0], target, {"flow": t["flow"], "model": t["model"]}, run_name, source="task-distill")
                        diff = difficulty(t["call"], t["reply"], 2, True, flow=t["flow"])
                        row["meta"].update({"quality": q, "difficulty": diff, "tags": _tags(diff), "taught_by": t["model"], "taught_by_tier": t["tier"],
                                            "for_tier": target, "for_model": stu["model"], "teacher_model": t["model"], "student_model": stu["model"],
                                            "student_run": stu["run"], "why": "the worker failed this task's hidden checks; the teacher passed them"})
                        if lesson:
                            row["meta"]["lesson"] = lesson
                        if t["thinking"]:
                            row["meta"]["thinking"] = t["thinking"][:4000]
                        distill[target].append(row)
                        stats["distill"] += 1
                        stats["task_distill"] += 1

            # ---- tool trajectories: one sample per tool loop, rebuilt from the call id each tool record carries
            if run_pass:
                new_traj = _trajectories(full, run_name, roles, max_chars, stats)
                if lesson:
                    for tr in new_traj:
                        if isinstance(tr.get("meta"), dict):
                            tr["meta"]["lesson"] = lesson
                traj.extend(new_traj)
            elif full["tools"]:
                stats["traj_run_not_passed"] += 1

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
        if str(c.get("model") or "").startswith(("fake:", "claude:")):
            continue                                    # fixtures and Claude-written turns never enter a trajectory
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
        user, _fr = strip_format_ref(user, first.get("model"))
        parts = [split_think(full["calls"][cid].get("reply") or "") for cid in chain + [final]]
        if any(p[2] for p in parts):
            stats["think_unterminated"] += 1
            continue
        msgs = ([{"role": "system", "content": sys_t}] if sys_t else []) + [{"role": "user", "content": user}]
        size = len(sys_t) + len(user)
        for cid in chain:
            c = full["calls"][cid]
            msgs.append({"role": "assistant", "content": split_think(c.get("reply") or "")[0]})
            for t in tools_by_call[cid]:
                res = f"{t.get('tool')} -> " + str(t.get("result") or "")[:1500]
                msgs.append({"role": "tool", "name": str(t.get("tool") or ""), "content": res})
                size += len(res)
            size += len(c.get("reply") or "")
        msgs.append({"role": "assistant", "content": split_think(full["calls"][final].get("reply") or "")[0]})
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

def slug(model):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(model or "unknown")).strip("-.") or "unknown"


# A LoRA only fits the base it was trained on. The Ollama/GGUF model the worker runs -> the Hugging Face base to train from.
BASES = [
    (r"qwen3[^0-9]*(?<![0-9.])4b\b", "unsloth/Qwen3-4B-Instruct-2507"),
    (r"qwen3[^0-9]*(?<![0-9.])8b\b", "unsloth/Qwen3-8B"),
    (r"qwen3[^0-9]*(?<![0-9.])14b\b", "unsloth/Qwen3-14B"),
    (r"qwen2\.5-coder[^0-9]*(?<![0-9.])7b\b", "unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit"),
    (r"qwen2\.5-coder[^0-9]*(?<![0-9.])14b\b", "unsloth/Qwen2.5-Coder-14B-Instruct-bnb-4bit"),
    (r"qwen2\.5-coder[^0-9]*(?<![0-9.])3b\b", "unsloth/Qwen2.5-Coder-3B-Instruct-bnb-4bit"),
]


def base_for(model):
    """-> the training base for this served model, or None when it is not known (then the recipe says so instead of guessing)."""
    m = str(model or "").lower()
    if "moe" in m or re.search(r"\ba\d+b\b", m):
        return None                                   # mixture-of-experts: not a LoRA-on-6GB target, and no safe guess
    for rx, base in BASES:
        if re.search(rx, m):
            return base
    return None


def base_matches(model, base):
    """Does this HF base fit this served model? Same family word and same parameter size. Unknown model -> None (cannot tell)."""
    want = base_for(model)
    if not want:
        return None
    return want.lower() == str(base or "").lower() or (
        re.sub(r"-(instruct|bnb-4bit|2507)", "", want.lower()) == re.sub(r"-(instruct|bnb-4bit|2507)", "", str(base or "").lower()))


def configured_tiers(root=None):
    """{tier: model} the settings name (empty tiers left out)."""
    try:
        cfg = core.load_config(root or os.getcwd())
        return {t: m for t, m in (cfg.get("tiers") or {}).items() if m and t in TIERS}
    except Exception:
        return {}


def _row_model(r):
    m = r.get("meta") or {}
    return m.get("for_model") or m.get("model") or "unknown"


def write(data, out_dir, tier_models=None):
    """Write every set, remove anything a previous export left behind, and return the summary.

    Tier files are PER MODEL: `tier-<tier>.<model-slug>.sft.jsonl`, because an adapter fits one base. `tier-<tier>.sft.jsonl` holds only
    the rows of the model configured for that tier (`tier_models`, default: the settings); with no configured model it is not written."""
    os.makedirs(out_dir, exist_ok=True)
    tier_models = configured_tiers() if tier_models is None else dict(tier_models)
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
    per_model = collections.defaultdict(lambda: collections.defaultdict(int))
    for tier, rows in sorted(by_tier.items()):
        groups = collections.defaultdict(list)
        for r in rows:
            groups[_row_model(r)].append(r)
        for mdl, grp in sorted(groups.items()):
            dump(f"tier-{tier}.{slug(mdl)}.sft.jsonl", grp)  # one adapter fits one base: one file per model
            per_model[tier][mdl] = len(grp)
        want = tier_models.get(tier) or (next(iter(groups)) if len(groups) == 1 else None)   # unconfigured: only when unambiguous
        if want:
            dump(f"tier-{tier}.sft.jsonl", groups.get(want, []))   # what you actually fine-tune: the configured model's rows only

    dpo_by_tier = collections.defaultdict(list)
    for role, rows in (data.get("dpo") or {}).items():
        dump(f"{role}.dpo.jsonl", rows)
        for r in rows:
            dpo_by_tier[r["meta"].get("rejected_tier") or "worker"].append(r)
    for tier, rows in sorted(dpo_by_tier.items()):
        groups = collections.defaultdict(list)
        for r in rows:
            groups[r["meta"].get("rejected_model") or "unknown"].append(r)
        for mdl, grp in sorted(groups.items()):
            dump(f"tier-{tier}.{slug(mdl)}.dpo.jsonl", grp)
        want = tier_models.get(tier) or (next(iter(groups)) if len(groups) == 1 else None)
        if want:
            dump(f"tier-{tier}.dpo.jsonl", groups.get(want, []))
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
               "per_tier": {t: {"sft": len(rows), "dpo": len(dpo_by_tier.get(t, []))} for t, rows in by_tier.items()},
               "per_model": {t: dict(v) for t, v in per_model.items()}, "tier_models": tier_models}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    with open(os.path.join(out_dir, "RECIPE.md"), "w", encoding="utf-8") as f:
        f.write(recipe(summary, out_dir))
    return summary


def recipe(summary, out_dir):
    st = summary.get("stats") or {}
    n_sft, n_dpo, n_tr = st.get("sft", 0), st.get("dpo", 0), st.get("traj", 0)
    per_tier = summary.get("per_tier") or {}
    per_model = (summary.get("per_model") or {}).get("worker") or {}
    best = "worker"
    worker = (summary.get("tier_models") or {}).get("worker") or ""
    base = base_for(worker) if worker else None
    biggest = os.path.join(out_dir, f"tier-worker.{slug(worker)}.sft.jsonl" if worker else "tier-worker.sft.jsonl")
    n_worker = per_model.get(worker, 0) if worker else 0
    if not worker:
        target_line = ("- **No worker model is configured** (Settings > Models > tiers.worker), so no training target is named. "
                       "Per-model files: " + (", ".join(f"`tier-worker.{slug(m)}.sft.jsonl` ({n})" for m, n in sorted(per_model.items())) or "none") + ".")
    elif not base:
        target_line = (f"- Your worker is `{worker}`. **No known training base matches it**, so no command is given: an adapter trained "
                       "on another base will not load on it. Find that model's exact Hugging Face base before training.")
    else:
        target_line = f"- Train your worker `{worker}` from `{os.path.basename(biggest)}` ({n_worker} samples) on base `{base}`."
    others = [f"`tier-worker.{slug(m)}.sft.jsonl` ({n})" for m, n in sorted(per_model.items()) if m != worker]
    lines = [
        "# Training the worker on your own runs",
        "",
        "These files came from your runs. Every sample in them is a reply that the code checks accepted: tests passed, the verifier",
        "agreed, nothing fake. Replies whose checks could not run were left out, and so was anything that looked like a credential.",
        "",
        f"- SFT samples: **{n_sft}**   preference pairs: **{n_dpo}**   tool trajectories: **{n_tr}**   taught by a bigger tier: **{st.get('distill', 0)}**",
        target_line,
        ("- Samples from other models are in their own files and are NOT for this adapter: " + ", ".join(others) + ".") if others else "",
        "",
        "## What is in the samples, and what is not",
        "",
        "- Only runs whose final verdict was **pass** give SFT samples (tool trajectories and distillation too). Preference pairs may come",
        "  from a run that failed overall: the failed try and the passing try of the same step are still a true pair.",
        "- **Thinking:** a complete `<think>...</think>` block is cut out of the reply and kept in `meta.thinking`; the model is trained on",
        "  the answer only. A reply with an unterminated `<think` is skipped, and so is a reply cut off at its token limit",
        "  (`done_reason: length`).",
        "- The ledger's `FORMAT REFERENCE` few-shot block is cut from the prompt unless it quotes this same local model's own reply.",
        "- Nothing Claude wrote or took part in (a `claude` transport, a `claude:` model, a Logbook task marked by_claude), no benchmark",
        "  or holdout run, and no eval run whose `eval_task.json` cannot be read.",
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
        ("python train_lora.py --data " + biggest.replace("\\", "/") + " \\") if base else "# no command: see the target line above",
        (f"    --model {worker} --base {base} \\") if base else "",
        "    --out hub_data/adapters/worker-lora --epochs 2 --max-seq 4096" if base else "",
        "```",
        "",
        "`train_lora.py` refuses a file that mixes models, and a `--base` that does not match `--model`. A 7B at 4-bit trains in",
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
        f"FROM {worker or '<your worker model>'}",
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
