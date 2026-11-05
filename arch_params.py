"""The ONE schema for every setting the Architect exposes: Settings, "Options for this run" and the per-role /
per-model advanced overrides. The hub and the CLI both validate against this instead of trusting the client,
and a selftest checks that every path here actually exists in arch_core.DEFAULTS (and vice versa), so the
schema and the real defaults cannot drift apart silently.

A field is (path, type, label, hint, section). Types:
  "int"      -> int(value), optionally clamped to [min, max] via a 4th tuple element (min, max) or None
  "float"    -> float(value), same clamping
  "bool"     -> True/False
  "choice:a,b,c" -> value must be one of a/b/c
  "list"     -> comma-separated text -> list[str]
  "text"     -> str, stripped
"""
import re

# ----------------------------------------------------------------------------------------------- settings
# section -> [(path, type, label, hint, range_or_None)]
SETTINGS = {
    "Limits": [
        ("limits.num_ctx", "int", "Context size (tokens)", "Per-model values from the Models tab win.", (512, 262144)),
        ("limits.max_calls", "int", "Max model calls per run", "Hard stop against runaway runs.", (1, 2000)),
        ("limits.max_revisions", "int", "Fix attempts per step", "Revisions after a failed check.", (0, 20)),
        ("limits.max_sprints", "int", "Max sprints in a build", "The planner may not plan more.", (1, 20)),
        ("limits.max_files_per_step", "int", "Files per step", "", (1, 50)),
        ("limits.context_chars", "int", "Context size for prompts (chars)", "", (500, 500000)),
        ("limits.request_timeout", "int", "Model reply timeout (s)", "Ceiling for any single model call. In auto mode each call gets a shorter limit derived from that model's measured speed.", (10, 7200)),
        ("limits.escalate_max_s", "int", "Escalation time limit (s)", "A stuck step is handed to a larger model only if, by that model's measured speed here (load + writing a typical answer), it can answer within this many seconds. A 14B split between a 6 GB card and RAM takes minutes per answer, so it is skipped and the step stays on its own model.", (30, 7200)),
        ("limits.timeout_mode", "choice:auto,fixed", "Timeout mode", "auto = first-token, idle and total limits learned per model (a 2B gives up in seconds, a slow 32B gets minutes); fixed = always the reply timeout above.", None),
        ("limits.ctx_autogrow", "bool", "Grow context when a prompt overflows", "If a prompt fills the whole window (the server silently drops its start), retry once with a doubled window.", None),
        ("limits.num_ctx_max", "int", "Largest context to grow to", "Ceiling for the automatic growth above. Keep it within what your VRAM holds.", (2048, 262144)),
        ("limits.test_timeout", "int", "Test timeout (s)", "", (5, 3600)),
        ("limits.keep_alive", "text", "Keep model loaded", "e.g. 30m, 1h, -1 (forever)", None),
        ("limits.max_file_bytes", "int", "Largest file read (bytes)", "", (1000, 20000000)),
    ],
    "Behaviour": [
        ("behavior.review_mode", "choice:ask,auto,dry", "Default review mode", "ask = you approve every diff", None),
        ("behavior.evaluator", "choice:final,each,off", "Strict judge", "final = once per build; each = every sprint; off = code checks only", None),
        ("behavior.best_of_n", "int", "Best of N (sampled attempts)", "Run a step N times and keep the first that passes checks; 1 = off.", (1, 5)),
        ("behavior.offline_tests", "bool", "Run tests offline (legacy)", "False here means the same as network=open. Prefer the Network setting below.", None),
        ("behavior.playwright", "choice:auto,always,off", "Browser smoke test", "Needs: pip install playwright", None),
        ("behavior.seed", "int", "Seed", "Same seed = more repeatable. Blank = unset.", (0, 2 ** 31 - 1)),
        ("storage.archive", "text", "Archive folder", "A second drive (a memory card or USB drive is fine) where a copy of every finished run's records is written in the background. Slow or unplugged drives are fine: copies wait and are retried. Models never go there. Empty = off.", None),
        ("behavior.sandbox_mem_mb", "int", "Sandbox memory (MB)", "The most memory one test or app process may use while a run checks its work; it is stopped above that, and everything it started is stopped when it ends. 0 = no limit.", (0, 262144)),
        ("behavior.tests_on_ecores", "choice:auto,off", "Tests on efficiency cores", "auto = on a CPU with performance and efficiency cores (Intel 12th gen and later), tests and checks run on the efficiency cores, so the performance cores stay free for the models writing code at the same time. off = tests may use any core.", None),
        ("behavior.retry_stop", "choice:auto,off", "Stop retrying when it never pays", "auto = a build step stops before try 3 or later when, for this model, at least 10 measured attempts at that try all failed and no later try ever passed: the time goes to the next step instead. Thin data never stops anything. off = always use every try.", None),
        ("behavior.reply_budget", "choice:auto,off", "Learned reply budgets", "auto = after 30 replies of one kind of call, its token limit becomes 2x what 95% of them needed (never more than the fixed limit, never below 1024 tokens, 512 for structured replies), so a model stuck repeating itself stops sooner; if any of the last 20 replies hit its limit, the fixed limit is used again. off = the fixed limits only.", None),
        ("behavior.close_on_exit", "choice:auto,off", "Close everything when the hub exits", "auto = when the hub closes (its window, Ctrl+C or a normal exit) the app stops its model servers, unloads the models Ollama is holding, stops projects started from Launch and deletes leftover eval folders, so the next run starts on a free machine. A run still in progress in another window keeps its servers. off = leave everything running.", None),
        ("behavior.chores_on_ecores", "choice:auto,off", "Background chores on efficiency cores", "auto = work nobody waits for (the archive copy of a finished run, the agent watcher's scans, a Logbook ingest from the command line, the learning sync) runs at below-normal priority, on the efficiency cores when the CPU has them, so it never slows a reply. off = normal priority, any core.", None),
        ("behavior.tests_first", "choice:on,off", "Tests first (from the planner)", "on = when a build asks for tests, the planner writes them from the acceptance criteria before any code, and the builder codes against tests it may not edit (small builders often write wrong tests and then chase them). off = the builder writes its own tests.", None),
        ("behavior.stall_samples", "int", "Attempts when a step is stuck", "When a step keeps failing and no bigger model can take over in time, the fast model makes this many different attempts; each is tested and the best one is kept. 1 = off.", (1, 8)),
        ("behavior.chat_model", "choice:worker,role", "Chat answers with", "worker = the fast model on the graphics card answers chat questions in seconds. role = the chat's own role model (often a bigger planner: better answers, but on a small graphics card a reply can take minutes).", None),
        ("behavior.thinking", "choice:roles,plan_debug_only", "Reasoning", "roles = each role decides whether the model thinks before answering (15 of them do). plan_debug_only = only planning and debugging think; writing, reviewing and checking answer straight away, which saves tokens and time. Measure both on Models > Benchmarks before choosing.", None),
        ("behavior.sampling", "choice:roles,model_card", "Sampling", "roles = each role's own temperature. model_card = Qwen3 models use the settings their makers publish (thinking 0.6 / top-p 0.95 / top-k 20, otherwise 0.7 / 0.8 / 20). Measure both before choosing.", None),
        ("behavior.edit_style", "choice:patch,whole,auto", "Edit style", "patch = search/replace blocks; whole = write complete files under ~200 lines (small models apply these far more reliably); auto = whole under ~80 lines. Set by the harness profile.", None),
        ("behavior.fake_code", "choice:off,warn,fail", "Fake-code check", "Stubs, assertion-free tests, placeholder data in files a step changed. fail = the model must fix them; warn = report only.", None),
        ("behavior.max_shrink_pct", "int", "Largest allowed file shrink %", "A reply that would delete more than this share of an existing 25+ line file is refused (models often return only the part they changed). 0 = off.", (0, 95)),
        ("behavior.fast_tests", "choice:on,off", "Related tests first", "on = a fix loop runs the tests that belong to the changed files first and reports a failure at once; the whole suite still has to pass before a step is accepted. off = always the whole suite.", None),
        ("behavior.swap_aware", "choice:on,off", "Avoid model swaps on a small GPU", "on = when the judge is a different model and the GPU holds one at a time, the build flow judges all sprints together at the end, from the evidence saved at each sprint (one swap instead of two per sprint). The run report counts every swap and its loading time either way.", None),
        ("behavior.escalate", "choice:off,stalled", "Escalate when stuck", "stalled = after a fix attempt changes nothing, the step continues on the next larger model you have installed and tuned, then returns to its own. Never downloads, never remote.", None),
        ("behavior.self_protect", "choice:none,guard", "Files self-improvement may not edit", "none = the harness may change any of its own code, including the network guard and the gate; the full test suite and the backup/rollback are the only checks. guard = the network guard and the gate stay out of reach.", None),
        ("behavior.learn_from_runs", "choice:on,off", "Learn from your own runs", "on = a role sees one of its own past replies that passed the checks for a similar request, as a format reference (your successes become few-shot examples). off = none.", None),
        ("behavior.exclusive_gpu", "choice:auto,off", "Free the GPU before a run", "auto = models sitting in Ollama's memory that are not part of your team are unloaded before the first call (Ollama's own keep_alive 0; nothing is killed). off = leave them.", None),
        ("behavior.small_helpers", "choice:on,off", "Small helper on the CPU", "on = the small router model (on the processor) writes the after-failure notes, so the graphics card stays free for the next try. off = the reviewer model writes them.", None),
        ("behavior.ground_edits", "choice:on,off", "Read before editing (local tools)", "on = a line edit is refused until the model has looked at those lines in this run (read, view or a search hit), and the third identical read of unchanged lines is answered with a pointer instead of the same text. Today's agents did about 5 reads per edit; small models that edit blind miss.", None),
        ("behavior.routing", "choice:model,keywords", "Choosing the workflow", "model = the router model reads your request and picks the flow (keywords are only a hint and the fallback). keywords = rule-first, the model only breaks ties.", None),
        ("behavior.battery_saver", "choice:auto,off", "Battery saver", "auto = on a laptop running on battery, this run uses a shorter context and keep-alive and no sampled retries. Your saved settings are not changed.", None),
        ("behavior.git", "choice:off,commit", "Git checkpoint", "commit = after a successful run, commit the changes (creates the repository if needed)", None),
        ("behavior.mutation", "choice:on,off", "Mutation check (test flow)", "Measures tests by breaking the code on purpose", None),
        ("behavior.mutation_max", "int", "Mutations to try", "", (1, 50)),
        ("behavior.min_gain_pct", "int", "Required speed-up % (optimize)", "Smaller gains are rolled back", (1, 95)),
        ("behavior.bench_runs", "int", "Benchmark repetitions", "Median of N runs", (1, 25)),
        ("behavior.hunt_max_files", "int", "Files per bug hunt", "", (1, 20)),
        ("behavior.hunt_findings", "int", "Findings per bug hunt", "", (1, 20)),
        ("behavior.config_strict", "choice:high,medium,low", "Config scan fails at", "A Dockerfile/CI/config finding at or above this severity fails the check", None),
        ("behavior.network", "choice:offline,practice,open", "Network for code the Architect runs",
         "offline = loopback only (default: nothing the Architect runs can leave this machine); practice = loopback + the hostnames below only; open = unrestricted, your machine's normal network access. Applies to tests, benchmarks, model-written scripts, and `serve` -- entirely yours to change, in either direction.", None),
        ("behavior.net_allow", "list", "Allowed hostnames (practice mode)", "Comma separated, e.g. pypi.org, api.github.com", None),
        ("behavior.net_block_tools", "bool", "Also block network/scan/VCS binaries", "curl, nmap, ssh, git, etc. launched by code the Architect runs -- independent of the network mode above, since those open their own sockets a host allowlist can't police; turn off if you want them runnable too", None),
            ("behavior.recheck_before_retry", "bool", "Re-check before retrying", "When a check fails, it is run once more with nothing changed before the model is asked to try again. If it passes the second time, the check itself is unreliable (a flaky test), so the step is reported as 'not sure' rather than spending more attempts on a coin toss. Costs one extra check run per retry; saves model calls whenever a test is flaky.", None),
            ("behavior.merge_small_plans", "bool", "One sprint for one module", "When the request is one module and its tests, the planned sprints run as one. Every hand-off between sprints is a chance for a small model to rewrite a file and drop what the sprint before it finished; a single module does not need them. Off: the plan is followed as written.", None),
            ("behavior.keep_best_try", "bool", "Undo a try that made things worse", "Inside a step, a try that is clearly worse than the one before it (it broke the code, removed a function an earlier sprint built, or fewer tests pass while more fail) is undone before the next try, and the model is told so. Otherwise one bad try at the end can throw away the progress of all the tries before it.", None),
            ("behavior.record_live", "choice:on,off", "Record what the machine is doing", "on = while the hub runs, every 5 seconds one line with RAM, graphics memory, GPU temperature and power, the power source and the running model servers goes to hub_data/live.log (two files of about 2 MB, written straight to disk), so a freeze or a crash leaves evidence of what led up to it. off = nothing is recorded.", None),
            ("behavior.heavy_jobs", "choice:one,any", "Heavy jobs", "one = test suites, benchmarks, tuning and self-improvement run one at a time and below normal priority: a second one waits for the first, and the desktop stays usable (a laptop that ran four at once crashed). any = no waiting, normal priority.", None),
            ("behavior.postmortem", "choice:on,off", "Explain failures", "After a run fails, the model reads what happened (the checks that failed, the last feedback) and writes a short explanation in plain words next to the run (postmortem.md). When the queue retries the task, the retry is given that explanation. Costs one model call per failed run; never runs during a benchmark.", None),
            ("behavior.fold_failed_sprints", "bool", "Carry on past a failed sprint", "In unattended runs, a sprint that still fails after its retries hands its work to the next sprint instead of ending the build, so the later parts of the request are still attempted. The build only counts as complete if the last sprint passes. Off: the build stops at the first sprint that will not pass, so you can look at it.", None),
    ],
    "Retrieval": [
        ("retrieval.mode", "choice:hybrid,tfidf", "What the prompt shows", "hybrid = the file ranking fused with a full-text index of your functions (SQLite FTS5 inside the project; nothing to download, nothing on the GPU); tfidf = the file ranking only.", None),
        ("retrieval.chunk_lines", "int", "Chunk size for non-Python files (lines)", "Python is chunked per function/class.", (10, 400)),
        ("retrieval.max_chunks", "int", "Largest project to index (chunks)", "Above this the index stays off for that project.", (100, 200000)),
    ],
    "Server": [
        ("server.kind", "choice:auto,llama,ollama", "Model server", "auto = the resident llama-server when its program is installed (bin/llama-server), otherwise Ollama. llama = require it. ollama = never start llama-server.", None),
        ("server.binary", "text", "llama-server program", "Blank = look in the install's bin/ folder and on PATH.", None),
        ("server.port", "int", "First port for llama-server", "Each resident tier takes the next port (router, worker, heavy).", (1024, 65000)),
        ("server.kv", "choice:f16,q8_0,q4_0", "Cache precision (KV)", "q8_0 = the model's working memory for the conversation takes half the space with almost no loss (default); f16 = full size; q4_0 = a quarter, with a visible loss on code. Needs flash attention.", None),
        ("server.flash_attn", "bool", "Flash attention", "Faster and leaner attention on the graphics card; required for the cache precision above. Off only if a model misbehaves with it.", None),
        ("server.draft", "choice:auto,try,off", "Speculative decoding (draft model)", "auto = use the draft tier you set (never one whose tokenizer differs from the worker's); when none is set, the plan names an installed model with exactly the worker's tokenizer that fits beside it. try = use that model. off = never. Compare the tokens/s in the console's status strip with and without.", None),
        ("server.heavy_beside", "choice:auto,off", "Heavy model beside the worker", "auto = a mixture-of-experts heavy model stays loaded next to the worker, with its experts in RAM, so planning never waits for a model swap (measured on a 6 GB laptop: 14-16 tokens/s, switching in ~1 s instead of 15-20 s). off = the heavy model loads only when needed.", None),
        ("server.think_budget", "int", "Thinking budget (tokens)", "How many tokens a thinking model (the planner) may spend reasoning before it must answer. Measured here: without a limit the planner thought ~3000 tokens, 2-5 minutes, before a ~200-token plan. 0 = no limit.", (0, 32768)),
        ("server.idle_free_min", "int", "Free memory when idle (minutes)", "After this many minutes with no work, the app unloads its model servers and asks Ollama to unload its models, giving back graphics memory and RAM (seen: 13.7 GB of RAM held by an idle Ollama copy of the heavy model). The next task loads them again, about 5-8 seconds. 0 = keep everything loaded for the fastest first answer.", (0, 1440)),
        ("server.ollama_gpu", "choice:auto,allow", "Ollama models beside the worker", "auto = a model that Ollama answers (not one of our own model servers, for example the heavy model for the planner) stays entirely in RAM while our worker server holds the graphics card, so the worker keeps every layer on the card. allow = Ollama decides how much goes on the card.", None),
        ("server.fast_lane", "choice:off,on", "Fast lane for the worker", "on = the same worker model answers sooner by taking more of this computer: the heavy model no longer stays loaded beside it, every layer and its whole memory go on the graphics card (the window shrinks to the largest that fits, never below 6144 tokens), speculative decoding by n-gram lookup is on (measured here: 56 -> 306-343 tokens/s on edits), its process runs at above-normal priority on the performance cores, and its thinking budget drops to the fast-lane budget. Planning waits for the heavy model to load when a step escalates (15-20 s). Windows' power plan and other settings are never changed. off = as before.", None),
        ("server.fast_think_budget", "int", "Fast lane thinking budget (tokens)", "In the fast lane, how many tokens the worker may spend reasoning before it must answer (only when the worker thinks at all; it never gets more than the normal thinking budget). 0 = the normal budget.", (0, 32768)),
        ("server.make_room", "choice:auto,off", "Make room on the graphics card", "auto = when a model that does not fit beside the resident worker, but fits on the graphics card alone, is called (a 7B planner beside a 4B worker on a 6 GB card, say), the worker steps off the card for that call and comes back after, so that model runs fully on the card instead of partly from RAM. A model too big for the card even alone (a 14B on 6 GB) is left as it is: measured, it ran no faster with the card to itself. Costs a few seconds to stop and start the worker. off = the worker always stays.", None),
        ("server.cache_ram", "int", "Prompt cache in RAM (MiB)", "How much RAM each model server may keep for saved prompts (they make a repeated start of a prompt instant). 0 = by this machine's RAM: 1 GB per server on 16 GB, 2 GB on 32 GB, 4 GB above; the router keeps none. llama-server's own default is 8 GB per server.", (0, 65536)),
        ("storage.slot_cache_gb", "float", "Saved prompt memory on disk (GB)", "At the end of a run the worker's memory of a project's prompt is saved (0.2-0.4 GB a project), so reopening the project skips re-reading it. When these files together pass this size, the least recently used ones are deleted at the next save (the one just saved is always kept). Deleting one only costs that project a slower first answer. 0 = no limit.", (0, 1000)),
        ("storage.slot_max_age_days", "int", "Offer to remove prompt memory unused for (days)", "`python architect.py caches` lists saved prompt memory nobody has used for this long, and removes it only with --prune --yes. 0 = never counts as stale.", (0, 3650)),
        ("server.parallel", "int", "Parallel slots", "How many replies the worker can produce at once. With 2 or more, Best-of-N attempts are generated side by side instead of one after another: the shared prompt is read once, so N attempts take little more time than one. The cost is graphics memory: every extra slot keeps its own copy of the conversation memory for the whole window (about 0.6 GB per slot for an 8B or 4B Qwen3 at 8K with the q8_0 cache), and what no longer fits pushes model layers off the card, which slows every reply. `server plan` shows the numbers for your card; 1 = one at a time.", (1, 8)),
        ("server.ubatch", "int", "Prompt micro-batch", "Prompt tokens processed per step; smaller saves graphics memory, larger reads prompts faster.", (64, 2048)),
    ],
    "Server context": [
        ("server.ctx", "int", "Context the resident server starts with", "How much of a conversation each model can hold, in tokens. 0 means use the window your hardware profile measured as safe, which is the right answer on almost every machine. A bigger window costs graphics memory for every parallel conversation, so raising it can push layers off the GPU.", (0, 262144)),
        ("server.slots", "bool", "Keep the unchanging start of each prompt on disk", "Every step begins with the same instructions and project notes. Saving that part once means a later run skips reading it again, so the first answer after you open the app is as quick as the tenth. Costs a little disk, nothing else. Ignored if your model server does not offer it.", None),
        ("server.ngram", "choice:auto,off", "Guess repeated text ahead", "A code edit repeats most of the file it is editing, so the server can guess those words before the model confirms them, which is faster for no extra memory. auto = use it when the server offers it and no draft model is loaded. It never changes the answer, only how fast it arrives.", None),
    ],
    "Flows": [
        ("flows.engine", "choice:legacy,data,off", "Flow definitions", "legacy = the built-in Python flows run; flows/*.yaml only add flows no built-in provides. data = a flows/*.yaml definition replaces the built-in flow of the same name (flows as data). off = YAML files are ignored.", None),
            ("flows.budget_scale", "float", "Budget scale for every flow", "Each flow has a ceiling on model calls, tokens and seconds. On a slow processor-only machine the same work takes longer, so 2 doubles every allowance; 0.5 halves them for a quick look. It changes ceilings only, never what passes.", (0.1, 10)),
    ],
    "Supervisor (working on its own)": [
        ("supervisor.daily_runs", "int", "Runs per day, at most", "The supervisor stops starting new tasks once it has run this many today. 0 means no limit, which is only sensible on a machine nobody is waiting on.", (0, 10000)),
        ("supervisor.daily_minutes", "int", "Minutes per day, at most", "A ceiling on the total time the supervisor spends working each day. It finishes the task it is on, then stops.", (0, 1440)),
        ("supervisor.task_minutes", "int", "Minutes one task may take", "A task still running after this long is stopped and counted as failed, so one stuck task cannot hold up the whole queue.", (1, 600)),
        ("supervisor.max_attempts", "int", "Approaches to try per task", "When a task fails or ends 'not sure', it is tried again with a different flow (a bug fix that could not reproduce becomes a bug hunt, and so on). This is how many different approaches it gets before it is left for you.", (1, 6)),
    ],
    "Your own commands (hooks)": [
        ("hooks.enabled", "bool", "Run my hooks", "Hooks are commands you choose to run at fixed points in a run: a linter after every change, a formatter check, staging a passing result in git. A hook you mark as blocking fails the step when it exits non-zero, and the model is given its output and asked to fix it, exactly as if a test had failed.", None),
        ("hooks.trust_project", "bool", "Trust hooks that come with a project", "Off is the safe answer. A project's hook file arrives with its code, so cloning a repository could otherwise run its commands here the moment you opened it. Left off, the harness shows you what a new project wants to run and waits for `architect.py hooks --trust`.", None),
    ],
    "What it has learned (memory)": [
        ("memory.prompt_chars", "int", "Notes carried into each prompt (characters)", "How much of what the app has learned about this project -- its own description, the lessons that worked, the traps -- rides along at the start of every prompt. More is more context; less leaves more room for the code itself in a small window.", (0, 12000)),
        ("memory.top_lines", "int", "Lessons and traps shown at once", "Only the best-earned ones are shown: those that were in front of the model when a run passed rank first.", (0, 60)),
        ("memory.max_lines", "int", "Longest a notes file may grow", "Past this, the oldest lines that never helped are dropped first.", (20, 5000)),
        ("memory.prune_after", "int", "Drop a note after this many uses with no help", "A note that has been shown this many times and was never followed by a passing run is removed. Memory shrinks when it is not earning its place.", (1, 100)),
    ],
    "Benchmark (what counts as worse)": [
        ("eval.pass_rate_drop", "float", "Pass rate may drop by at most (points)", "When a change is compared with the last saved benchmark run, a bigger drop than this in any flow's pass rate is a regression: self-improvement is rolled back and a new model is not promoted.", (0, 100)),
        ("eval.tokens_p50_rise_pct", "float", "Typical tokens may rise by at most (%)", "Doing the same work with many more tokens is slower and costs more context, so it counts as worse too.", (0, 1000)),
        ("eval.secs_p95_rise_pct", "float", "Slowest runs may slow down by at most (%)", "Measured on the slowest runs, because those are the ones you wait on.", (0, 1000)),
    ],
    "Project notes (skills)": [
        ("skills.enabled", "bool", "Use skills", "A skill is one short Markdown page about one way of working, in skills/ or this project's .arch/skills/. It is put in front of the model only for the tasks it applies to, so it costs nothing on the tasks it does not.", None),
        ("skills.max", "int", "How many at once", "More than two or three crowds out the actual code in a small context window.", (0, 6)),
        ("skills.budget", "int", "Characters they may take", "The ceiling for all loaded skills together.", (0, 20000)),
        ("skills.min_score", "float", "How closely a skill must match", "A skill is loaded only when the request matches its triggers at least this well. A trigger phrase found as written scores 2; a shared word scores less. Lower it to load skills more often; raise it to load them only on a clear match.", (0.5, 10)),
    ],
    "Reply cache": [
        ("cache.responses", "bool", "Remember replies", "When a call fixes temperature at 0 the model would answer the same way again, so the answer is kept and reused: a retried run, a repeated task or a rerun of the same question costs nothing. Creative calls (any temperature above 0) are never stored. An eval never uses it, so a score always measures the models.", None),
        ("cache.max_rows", "int", "Replies to keep", "The oldest unused replies are dropped past this. About 10 KB each.", (0, 200000)),
        ("cache.max_days", "int", "Forget replies after (days)", "0 = keep them until the count limit above drops them.", (0, 3650)),
    ],
    "Online": [
        ("online.enabled", "bool", "Allow online features", "Off (default) = the Architect itself never opens a connection off this machine: no registry checks, no Hugging Face lookups, no update checks, no fonts from the web. On = `catalog --verify` and `optimize --online` may reach their public sources when you run them. Model downloads always ask first either way.", None),
        ("online.cloud_tier", "choice:off,escalation,chat,plan,all", "Use Claude", "off (default) = never. escalation = only when the local heavy model is also stuck on a step. chat = escalation, plus Studio chat answers come from Claude. plan = Claude does the thinking roles (planner, architect, judge, reviewer) and your local models write and fix the code; escalation still applies. all = every coding step uses Claude (the router stays local); costs the most. Needs online features on and your own key in the ANTHROPIC_API_KEY environment variable; your code for that step leaves this machine. Claude's replies are never used as training data.", None),
        ("limits.cloud_usd_per_run", "float", "Claude spend per run (USD)", "A run stops asking Claude once this much is spent. Checked before every request.", (0, 50)),
        ("limits.cloud_usd_per_day", "float", "Claude spend per day (USD)", "Across every Claude call on this machine (runs, chat, ultracode). Once today's total reaches this, Claude is refused until tomorrow. 0 = no Claude calls at all. Answers from the local reply cache cost nothing and are not counted.", (0, 500)),
        ("online.cloud_cache", "bool", "Remember Claude's replies", "On (default) = the exact same request to the same model at the same effort is answered from this machine for $0 (kept 30 days). Never used during eval or the self-tests. Never used as training data.", None),
        ("online.cloud_fast", "bool", "Claude fast mode", "On = Opus answers up to about 2.5x faster at twice the price ($8/$40 per million tokens on Opus 5.5). Other models ignore it. If fast mode is busy, the same call runs at normal speed.", None),
        ("online.cloud_cache_ttl", "choice:5m,1h", "Claude prompt-cache lifetime", "How long Anthropic keeps the shared prompt prefix. 5m (default) = a cache write costs 1.25x input. 1h = 2x input per write, but long pauses between calls still read the prefix at 0.1x.", None),
        ("online.ultra_worker", "text", "Ultracode worker model", "The three helpers (solver, critic, alternative), e.g. claude:claude-sonnet-5-5 (default). The lead stays on the chosen model.", None),
        ("online.ultra_skeptic", "text", "Ultracode skeptic model", "The two skeptics that try to refute the solver, e.g. claude:claude-haiku-5-5 (default, the cheapest).", None),
        ("online.cloud_model", "text", "Cloud model", "Which Claude model the last step uses, e.g. claude:claude-opus-5-5 (default) or claude:claude-sonnet-5-5 (half the price).", None),
        ("online.allow_pip", "bool", "Allow pip repairs", "Off (default) = `ready`/`doctor --fix` only report a missing pytest. On = they may run `pip install pytest` (reaches PyPI).", None),
    ],
    "Tests": [
        ("tests.command", "text", "Test command", "Blank = pytest (or unittest). Any stack: npm test, {python} -m pytest -q, go test ./... Only Python is covered by the offline guard.", None),
    ],
    "Files": [
        ("files.extensions", "list", "Editable file types", "Comma separated, with dots", None),
        ("files.ignored_dirs", "list", "Extra ignored folders", "", None),
        ("files.protected_dirs", "list", "Extra protected folders", "", None),
    ],
    "Connection": [
        ("ollama_host", "text", "Ollama host", "Blank = the OLLAMA_HOST environment variable, else 127.0.0.1:11434. A remote GPU box works too (e.g. 192.168.1.20:11434).", None),
        ("default_model", "text", "Default model", "Used for any role with nothing assigned and no fallback.", None),
    ],
}

# ----------------------------------------------------------------------------------------------- run options
# shown in "Options for this run" -- a focused subset of SETTINGS plus flow-specific knobs, all one-run overrides
RUN_OPTIONS = [
    ("limits.max_revisions", "int", "Fix attempts per step", "", (0, 20)),
    ("limits.max_sprints", "int", "Max sprints", "", (1, 20)),
    ("behavior.evaluator", "choice:final,each,off", "Strict judge", "", None),
    ("behavior.git", "choice:off,commit", "Git checkpoint after success", "", None),
    ("behavior.network", "choice:offline,practice,open", "Network for this run", "", None),
    ("behavior.mutation_max", "int", "Mutations to try (test flow)", "", (1, 50)),
    ("behavior.min_gain_pct", "int", "Required speed-up % (optimize)", "", (1, 95)),
    ("behavior.hunt_findings", "int", "Max findings (hunt)", "", (1, 20)),
    ("behavior.config_strict", "choice:high,medium,low", "Config scan fails at (config flow)", "", None),
]

# ----------------------------------------------------------------------------------------------- per-role / per-model overrides
# these map onto role_overrides[role] / model_overrides[model] = {think, sampling:{...}, num_predict, retries, system_extra, num_ctx}
OVERRIDE_FIELDS = [
    ("think", "choice:default,on,off", "Thinking", "On if the model supports it and the role asks for it; override either way.", None),
    ("sampling.temperature", "float", "Temperature", "", (0.0, 2.0)),
    ("sampling.top_p", "float", "Top-p", "", (0.0, 1.0)),
    ("sampling.top_k", "int", "Top-k", "", (0, 500)),
    ("sampling.repeat_penalty", "float", "Repeat penalty", "", (0.5, 3.0)),
    ("num_ctx", "int", "Context size (tokens)", "Overrides the global context size for this role/model.", (512, 262144)),
    ("num_predict", "int", "Max reply length (tokens)", "", (16, 32768)),
    ("seed", "int", "Seed", "", (0, 2 ** 31 - 1)),
    ("num_batch", "int", "Prompt batch size", "How many prompt tokens are processed at once. `tune --deep` measures this on your hardware and only sets it when it is clearly faster.", (32, 8192)),
    ("num_thread", "int", "CPU threads", "Threads used for the part of the model that runs on the CPU. `tune --deep` measures it when the CPU is involved.", (1, 256)),
    ("retries", "int", "Retries on a bad/empty reply", "", (0, 10)),
    ("system_extra", "text", "Extra system instructions", "Appended after the role's own system prompt.", None),
]


class ValidationError(ValueError):
    pass


def _clamp(v, rng):
    if rng:
        lo, hi = rng
        v = max(lo, min(hi, v))
    return v


def coerce(type_, raw, rng=None):
    """-> python value, or raises ValidationError. raw may already be the right python type (bool/int/list)."""
    if type_ == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValidationError(f"expected true/false, got {raw!r}")
    if type_ == "int":
        try:
            return _clamp(int(raw), rng)
        except (TypeError, ValueError):
            raise ValidationError(f"expected a whole number, got {raw!r}")
    if type_ == "float":
        try:
            return _clamp(float(raw), rng)
        except (TypeError, ValueError):
            raise ValidationError(f"expected a number, got {raw!r}")
    if type_ == "list":
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in str(raw).split(",") if x.strip()]
    if type_.startswith("choice:"):
        opts = type_[7:].split(",")
        s = str(raw).strip()
        if s not in opts:
            raise ValidationError(f"must be one of {', '.join(opts)}, got {raw!r}")
        return s
    return str(raw).strip()  # "text"


def _schema_by_path(section_map):
    out = {}
    for rows in section_map.values():
        for row in rows:
            out[row[0]] = row
    return out


_SETTINGS_BY_PATH = _schema_by_path(SETTINGS)
_RUNOPT_BY_PATH = {r[0]: r for r in RUN_OPTIONS}
_OVERRIDE_BY_PATH = {r[0]: r for r in OVERRIDE_FIELDS}


def validate_setting(path, raw):
    """-> coerced value, or raises ValidationError naming the path. Rows are (path, type, label, hint, range_or_None)."""
    row = _SETTINGS_BY_PATH.get(path) or _RUNOPT_BY_PATH.get(path)
    if not row:
        raise ValidationError(f"'{path}' is not a recognised setting")
    try:
        return coerce(row[1], raw, row[4])
    except ValidationError as e:
        raise ValidationError(f"{path}: {e}")


def validate_override(field, raw):
    row = _OVERRIDE_BY_PATH.get(field)
    if not row:
        raise ValidationError(f"'{field}' is not a recognised override")
    type_ = row[1]
    rng = row[4] if len(row) > 4 else None
    if type_.startswith("choice:") and field == "think":
        s = str(raw).strip()
        if s not in ("default", "on", "off"):
            raise ValidationError("think must be default/on/off")
        return None if s == "default" else s == "on"
    return coerce(type_, raw, rng)


def apply_override(overrides, field, raw):
    """Mutates `overrides` (a role_overrides[role] or model_overrides[model] dict) in place for one UI field,
    e.g. field='sampling.temperature'. Deleting on raw=="" / None."""
    if raw in ("", None):
        if "." in field:
            top, sub = field.split(".", 1)
            (overrides.get(top) or {}).pop(sub, None)
            if top in overrides and not overrides[top]:
                del overrides[top]
        else:
            overrides.pop(field, None)
        return overrides
    v = validate_override(field, raw)
    if "." in field:
        top, sub = field.split(".", 1)
        overrides.setdefault(top, {})[sub] = v
    elif v is None:
        overrides.pop(field, None)
    else:
        overrides[field] = v
    return overrides


def validate_settings_patch(flat):
    """flat: {dotted.path: raw_value} (e.g. from a JSON body or run options). -> {path: coerced}; raises
    ValidationError with ALL problems joined, naming every bad field at once rather than stopping at the first."""
    out, errors = {}, []
    for path, raw in flat.items():
        try:
            out[path] = validate_setting(path, raw)
        except ValidationError as e:
            errors.append(str(e))
    if errors:
        raise ValidationError("; ".join(errors))
    return out


def apply_preset(name):
    """name -> validated {dotted.path: coerced_value} for that bundle, same shape validate_settings_patch
    returns, so callers merge it into config the same way a manual settings save does. Raises KeyError for
    an unknown preset name, and ValidationError if a preset's own values ever drifted from the schema
    (selftest checks that they never do)."""
    if name not in PRESETS:
        raise KeyError(f"no such preset: {name!r} (have: {', '.join(PRESETS)})")
    return validate_settings_patch(PRESETS[name]["settings"])


PRESETS = {
    "strict-security": {
        "label": "Strict security",
        "blurb": "Lock code the Architect runs down to loopback, fail the config scan on medium findings, judge every sprint, and measure test quality with mutation. Slower, safer -- a good default for the security/network/pentest/config flows.",
        "settings": {
            "behavior.network": "offline",
            "behavior.net_block_tools": True,
            "behavior.config_strict": "medium",
            "behavior.evaluator": "each",
            "behavior.mutation": "on",
            "behavior.git": "commit",
        },
    },
    "fast-iteration": {
        "label": "Fast iteration",
        "blurb": "Skip the strict judge and mutation check, cap fix attempts low, and leave the network open -- for quick loops where you'll eyeball the diff yourself rather than wait on every proof.",
        "settings": {
            "behavior.evaluator": "off",
            "behavior.mutation": "off",
            "behavior.network": "open",
            "behavior.best_of_n": 1,
            "limits.max_revisions": 3,
        },
    },
    "thorough": {
        "label": "Thorough",
        "blurb": "Judge every sprint, try more fix attempts and more mutations, sample 3 attempts and keep the best, and require a bigger measured speed-up before an optimize flow accepts a change. Slowest, highest-confidence.",
        "settings": {
            "behavior.evaluator": "each",
            "behavior.mutation": "on",
            "behavior.mutation_max": 20,
            "behavior.best_of_n": 3,
            "behavior.min_gain_pct": 25,
            "behavior.bench_runs": 5,
            "limits.max_revisions": 8,
        },
    },
}


def list_presets():
    return [{"id": k, "label": v["label"], "blurb": v["blurb"], "settings": v["settings"]} for k, v in PRESETS.items()]


def all_schema_paths():
    return set(_SETTINGS_BY_PATH) | {"roles", "role_overrides", "model_overrides", "tiers"}


def is_known_setting(path):
    return path in _SETTINGS_BY_PATH or path in _RUNOPT_BY_PATH


def selftest_defaults_match(flat_defaults):
    """flat_defaults: arch_core.DEFAULTS flattened to dotted paths (as the hub already does). -> list of
    mismatches: schema paths missing from DEFAULTS, and DEFAULTS paths (outside roles/overrides) missing from
    the schema. Empty list means they agree."""
    problems = []
    schema_paths = set(_SETTINGS_BY_PATH)
    default_paths = {k for k in flat_defaults if not k.startswith(("roles", "role_overrides", "model_overrides", "tiers"))}
    for p in schema_paths - default_paths:
        problems.append(f"schema has '{p}' but arch_core.DEFAULTS does not")
    for p in default_paths - schema_paths:
        problems.append(f"arch_core.DEFAULTS has '{p}' but the schema does not")
    return problems
