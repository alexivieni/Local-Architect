# Concepts for Architect: making any PC work well, and putting every spare resource to use

*Written 2026-11-04. Sources are linked inline. "Low-end" means an 8 GB laptop with no GPU. "Workstation" means a machine with 12 GB or more of VRAM, possibly several GPUs.*

## 1. The core idea

A slow PC can still finish good work if three things are true:

- **The work survives interruptions.** If the laptop sleeps, runs out of memory or gets killed, the run picks up where it stopped.
- **The work is cut into small checked steps.** A small model fills in one step, and a code gate checks it.
- **The harness schedules hardware like a cluster.** GPU, CPU, RAM, disk and LAN machines are each treated as resources.

Most ideas below come from three places: durable workflow engines, agent research, and cluster schedulers.

---

## 2. Agentic end-to-end concepts

### 2.1 Durable execution (Temporal, Restate)
Every step is written to a journal before its result is used: model call, tool call, gate verdict. After a crash, finished steps are replayed from the journal and nothing is re-generated ([Temporal workflows](https://docs.temporal.io/workflows), [Restate](https://docs.restate.dev/concepts/durable_execution)).
- **Architect:** a new `journal.py` module keeps an append-only JSONL file per run, plus a SQLite index. The flow runner checks the journal before each step. The hub's Runs page gets a "Resume" button.
- **Low-end:** this matters most here. A 40-minute CPU run that dies at minute 35 costs about 1 minute to resume, not 40.
- **Workstation:** runs are shorter, so the gain is safe experiments. You can kill a run, change a setting and resume it.

### 2.2 Idempotency keys (Stripe) plus WAL discipline (ARIES)
Each step that changes something (file write, commit, test run) gets a key: `hash(step_id, inputs)`. On replay, a completed key returns its cached result and does not run the action again ([Stripe idempotency](https://stripe.com/blog/idempotency), [ARIES](https://dl.acm.org/doi/10.1145/128765.128770)).
- **Architect:** each tool in the tool registry is marked `idempotent: true/false`. Tools marked false must be journaled before they run.

### 2.3 Checkpointed graph with human interrupts and time travel (LangGraph, Replit)
State is saved after every node. The user can pause, edit the state, and fork the run from any checkpoint ([LangGraph persistence](https://langchain-ai.github.io/langgraph/concepts/persistence/), [human-in-the-loop](https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/)). Replit also links every checkpoint to a workspace snapshot ([Replit checkpoints](https://docs.replit.com/replitai/checkpoints-and-rollbacks)).
- **Architect:** YAML flows gain `interrupt: before|after` on a node. Each checkpoint creates a git commit in a scratch ref, so rewinding the state also rewinds the files. The hub shows a timeline with "re-run from here with tier X".
- **Low-end:** you fix one bad step by hand. You don't pay for a full re-run.
- **Workstation:** you can A/B test tiers from the same checkpoint.

### 2.4 One event stream (OpenHands)
Every action and every observation becomes a typed event in one log. The agent, the UI and replay all read from that same log ([OpenHands paper](https://arxiv.org/abs/2407.16741)).
- **Architect:** the journal, the hub and the eval harness share one event schema. The hub becomes a pure viewer. Evals can replay real runs.

### 2.5 Agent-Computer Interface (SWE-agent)
Give the model small tools it can handle: a file viewer that shows 100 lines at a time, a search with capped results, and an edit tool that rejects any edit that breaks parsing ([SWE-agent](https://arxiv.org/abs/2405.15793)).
- **Architect:** add a `tools/aci.py` with `view(path, line)`, `search(q, max=20)` and `edit(range, text)`. The edit tool runs a `py_compile`/`ast` check before saving.
- **Low-end:** a big win. 3-8B models fail often on raw shells and on whole-file rewrites.
- **Workstation:** still lowers token use and the number of retries.

### 2.6 Code as action (smolagents, CodeAct)
The model writes a short Python snippet that calls tools, in place of a series of JSON tool calls. This took about 30% fewer steps in the papers ([smolagents](https://huggingface.co/blog/smolagents), [CodeAct](https://arxiv.org/abs/2402.01030)).
- **Architect:** an optional `action_mode: code` per role. Snippets run in a restricted subprocess that can only call whitelisted tool functions.
- **Low-end:** fewer turns means fewer slow CPU inferences.
- **Honest limit:** sandboxing stdlib Python is hard. Use a separate process with timeouts and no network. Do not rely on `exec` with filtered globals.

### 2.7 Skill library (Voyager)
Save programs that passed their checks, with a description and an embedding. Retrieve them later and combine them ([Voyager](https://arxiv.org/abs/2305.16291)).
- **Architect:** a new `skills/` store. Any flow fragment or solution that passes its gate is saved with a summary. Retrieval uses SQLite FTS5, with optional embeddings.
- **Low-end:** a 3B model can reuse what a 14B model, or an earlier overnight run, already solved.
- **Workstation:** builds up a library that can be shared with your weaker machines.

### 2.8 Reflection memory (Reflexion)
After a failed attempt, the model writes a short lesson. The lesson is added to the next attempt ([Reflexion](https://arxiv.org/abs/2303.11366)).
- **Architect:** when a gate fails, the cheapest tier writes 2-3 lines. They are saved by task type in `memory/lessons.jsonl` and added to retries and to future runs of the same type.

### 2.9 Prompts compiled against a metric (DSPy)
You declare what each step takes in and puts out. An optimizer then tunes the instructions and few-shot examples against a metric ([DSPy](https://dspy.ai/), [paper](https://arxiv.org/abs/2310.03714)).
- **Architect:** the existing eval harness is the metric. A nightly job tunes prompts for each pair of hardware class and model (cpu8, gpu6, ...). The results are saved as `prompts/<class>/<role>.yaml`.
- **Low-end:** runs slowly, overnight. That is fine.
- **Workstation:** finishes in an hour and can tune for the low-end classes too.

### 2.10 Typed artifacts between roles (MetaGPT, ChatDev, CrewAI)
Roles pass structured documents along a chain (spec, then design, then code, then tests) and don't chat freely ([MetaGPT](https://arxiv.org/abs/2308.00352)). ChatDev limits each phase to two roles and short context, and asks a clarifying question before acting ([ChatDev](https://arxiv.org/abs/2307.07924)). CrewAI separates deterministic Flows from autonomous Crews ([CrewAI Flows](https://docs.crewai.com/concepts/flows)).
- **Architect:** each flow step declares an `output_schema`, and a gate validates it. Autonomous loops are allowed only when `hardware_class >= gpu12`. Otherwise the step stays a fixed sequence.
- **Low-end:** small models fill in a schema much more reliably than they hold a conversation.

### 2.11 Background agents in worktrees (Cursor, Codex, Devin)
Long jobs run in their own isolated checkout and end with a diff for review ([Cursor background agent](https://docs.cursor.com/background-agent), [Codex](https://openai.com/index/introducing-codex/)).
- **Architect:** `architect run --background` creates a `git worktree`. The hub's "Inbox" page lists diffs ready for review in the morning.
- **Low-end:** slow speed stops mattering for latency. You queue the job at night and review it at 8 am.

### 2.12 Plan files, compaction and subagents (Claude Code, Devin)
The plan and project memory live in files, outside the context. History is compacted, and subagents report back condensed results ([context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), [subagents](https://docs.anthropic.com/en/docs/claude-code/sub-agents), [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks)).
- **Architect:** each run gets a `PLAN.md` and a `NOTES.md`. Compaction kicks in at 60% of context. A cheap-tier "reader" subagent returns summaries of at most 300 tokens. Hooks are just code gates around tool calls.
- **Low-end:** this is what makes 4-8k context windows usable.

### 2.13 Approval modes and sandbox first (Codex CLI)
Three modes: suggest, auto-edit and full-auto. Network is off by default ([codex](https://github.com/openai/codex)).
- **Architect:** a single setting `autonomy`, with default `auto-edit`. Gates and tests run in a restricted subprocess.

---

## 3. Distributed-systems concepts

### 3.1 Bin-packing placer with requests and limits (Borg, Kubernetes)
Each model declares what it needs: VRAM for weights, KV memory at a given context, RAM and threads. A placer fits models into GPU first, then RAM, then mmap from disk ([Borg](https://research.google/pubs/large-scale-cluster-management-at-google-with-borg/), [K8s resources](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)).
- **Architect:** `placer.py` replaces the fixed tier tables. Hardware classes act as node labels.
- **Low-end:** it picks smaller quants and keeps one model resident.
- **Workstation:** it packs several tiers at once across GPUs.

### 3.2 Priority and preemption (K8s PriorityClass)
Interactive roles have high priority. Indexing, evals and summarizing have low priority, and can be paused at a checkpoint or unloaded from the GPU ([preemption](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/)). Durable execution (2.1) is what makes preempting safe.

### 3.3 Actors plus a content-addressed store (Ray, Git, Bazel)
Each model server is a long-lived actor with its own queue. Large artifacts (repo maps, diffs, embeddings) are stored once by SHA-256 and passed by reference ([Ray actors](https://docs.ray.io/en/latest/ray-core/actors.html), [git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects), [Bazel CAS](https://bazel.build/remote/caching)).
- **Architect:** a `cas/` directory. Prompt prefixes are hashed and matched to llama.cpp saved slots, so identical prefixes reuse their KV cache.

### 3.4 Work stealing and fan-out/fan-in (Cilk, Tokio, MapReduce)
Small tasks (lint, summarize one file, generate tests) go into per-worker queues, and idle workers steal from busy ones ([Blumofe & Leiserson](https://dl.acm.org/doi/10.1145/324133.324234), [MapReduce](https://research.google/pubs/mapreduce-simplified-data-processing-on-large-clusters/)).
- **Architect:** a `map:` step in flow YAML. Map jobs run on the cheapest tier and the reduce step runs on the strong tier.
- **Low-end:** map jobs run one after another on the CPU.
- **Workstation:** map jobs run in parallel `-np` slots. The flow YAML is the same either way.

### 3.5 Backpressure, circuit breakers, timeouts and heartbeats
- **Backpressure:** bounded queues per model. When a queue is full, the step degrades (shorter context, smaller model) instead of piling up requests until the machine swaps ([Reactive Streams](https://www.reactive-streams.org/)).
- **Circuit breakers:** each backend (llama.cpp, Ollama, RPC peer) is wrapped in a breaker. After N failures it trips, and calls go to the next tier. A half-open probe restores it later ([Fowler](https://martinfowler.com/bliki/CircuitBreaker.html), [Resilience4j](https://resilience4j.readme.io/docs/circuitbreaker)).
- **Leases and heartbeats:** long inferences send heartbeats. A dead process or a sleeping laptop lets its lease expire, and the job is rescheduled ([Chubby](https://research.google/pubs/the-chubby-lock-service-for-loosely-coupled-distributed-systems/), [Temporal activities](https://docs.temporal.io/activities), [etcd leases](https://etcd.io/docs/v3.5/learning/api/#lease-api)).

### 3.6 Graceful degradation ladder (Google SRE)
When resources run short, step down in this order: shorter context, then a smaller model, then fewer parallel jobs, then the cheapest gate path. Never fail without saying why. The hub shows the current level ([SRE overload](https://sre.google/sre-book/handling-overload/)).

### 3.7 Cascade routing and speculative decoding (FrugalGPT, llama.cpp)
Try the cheap model first and escalate only when a gate fails ([FrugalGPT](https://arxiv.org/abs/2305.05176)). Architect's code gates make a better escalation signal than an LLM judge. Speculative decoding uses a small draft model to speed up the big one ([llama.cpp speculative](https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md)). On a CPU, the gain depends on how often the draft's tokens are accepted, so measure it per machine before turning it on.

### 3.8 Observability and chaos testing
- **Observability:** one OpenTelemetry-style trace per run, with spans per step and model call: tokens, tok/s, TTFT, queue wait and VRAM ([OTel traces](https://opentelemetry.io/docs/concepts/signals/traces/), [GenAI conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)). Exported to local JSONL, with no collector. The hub draws a waterfall, and the placer uses these numbers to tune itself.
- **Chaos testing:** an eval mode that kills llama-server, injects timeouts and returns malformed JSON. It then checks that runs resume and breakers trip ([Chaos Monkey](https://netflix.github.io/chaosmonkey/), [Jepsen](https://jepsen.io/)). This shows the harness holds up on flaky low-end PCs.

### 3.9 Offline-first sync between PCs (Dynamo, CRDTs)
Run history, lessons, skills and eval results are append-only logs. They merge as grow-only sets, with last-writer-wins for settings ([CRDT](https://crdt.tech/)). Consistent hashing sends the same repo to the same LAN node, so its caches stay warm ([Dynamo](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf)).

---

## 4. "Any PC" product and design concepts

- **Hardware probe on first run (LM Studio, Jan, GPT4All):** detect RAM, free VRAM, the GPU vendor, the available backend (CUDA, Vulkan or CPU with AVX2/AVX512) and whether the machine is on battery. Then pick a class: cpu8, cpu16, igpu, gpu6 or gpu12+. Show a "will it fit" badge on each model ([LM Studio docs](https://lmstudio.ai/docs), [Jan docs](https://jan.ai/docs)).
- **Vulkan as the universal fallback:** ship CPU, Vulkan and CUDA builds of llama-server. Community tests report Intel Xe iGPUs at about 8-13 tok/s on 2-3B models ([llama.cpp #10879](https://github.com/ggml-org/llama.cpp/discussions/10879)). The install self-test parses llama-server's startup device line. If no GPU is found, Architect falls back to CPU and says why ([#24474](https://github.com/ggml-org/llama.cpp/issues/24474)).
- **Portable install without admin rights (llamafile, KoboldCpp):** an embeddable Python zip plus prebuilt binaries, so no installer is needed ([embeddable Python](https://docs.python.org/3/using/windows.html#the-embeddable-package), [llamafile](https://github.com/Mozilla-Ocho/llamafile), [KoboldCpp](https://github.com/LostRuins/koboldcpp)).
- **Layer offload based on free VRAM** at load time, with a safety margin. Re-plan when another app takes VRAM ([Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.md)).
- **Model budget table:** 8 GB CPU gets 1.5-3B at Q4. 16 GB gets 7-8B at Q4. 6 GB VRAM gets 7-9B at Q4 with some spill to CPU. 12 GB+ gets 14B. Use a q8_0 KV cache on low-RAM classes.
- **Power awareness (Windows EcoQoS):** read the power state with ctypes `GetSystemPowerStatus`. On battery, use background mode: fewer threads and below-normal priority ([QoS](https://learn.microsoft.com/windows/win32/procthread/quality-of-service)).
- **Hub design:**
  - A Linear-style Ctrl+K command palette with optimistic updates ([Linear](https://linear.app/now/how-we-redesigned-the-linear-ui)).
  - Geist-style neutral grays with a mono font for logs ([Geist](https://vercel.com/geist/introduction)).
  - Radix and shadcn accessibility patterns: focus rings, ARIA roles, reduced motion, contrast at WCAG AA ([Radix](https://www.radix-ui.com/primitives/docs/overview/accessibility)).
  - Apple HIG progressive disclosure: advanced tier settings stay behind "Advanced" ([HIG](https://developer.apple.com/design/human-interface-guidelines/)).
  - Everything inline and offline: no CDN, system fonts or an embedded woff2 subset.

---

## 5. Every spare resource helps (with honest limits)

| Resource | Best use | Honest limit |
|---|---|---|
| **Idle RAM (~20 GB on a 32 GB laptop)** | Keep the next likely models' GGUF files warm in the OS page cache, so a model swap takes seconds. Keep llama-server saved KV slots and prompt caches in RAM. Keep a second small model resident (draft or cheap tier). Put test scratch and SQLite temp files on RAM. | Prefer the page cache over a fixed RAM disk, because the OS takes page cache back under pressure ([tmpfs](https://www.kernel.org/doc/html/latest/filesystems/tmpfs.html), [vmtouch](https://hoytech.com/vmtouch/)). Use `--mlock` only for the active model, and only when RAM is plentiful. On 8 GB boxes, skip warming entirely. Compressed memory helps with harness overhead, but GGUF weights barely compress ([zram](https://docs.kernel.org/admin-guide/blockdev/zram.html)). |
| **Spare 32 GB SD card (18 MB/s sequential, 0.8 ms random reads, own USB bus)** | A cold, append-only tier: rotated journal and trace archives, eval history, model backups, the skill-library and FTS index (the random reads are fine for lookups), and a portable "Architect on a card" bundle to set up another PC offline. Its own bus means it doesn't compete with the SSD. | Never use it for live weights: a 4 GB model takes about 4 minutes to load at 18 MB/s. Never use it for swap, because it would thrash and wear the card out. ReadyBoost does nothing on an SSD system and appears to be gone from recent Windows 11 builds ([ReadyBoost](https://en.wikipedia.org/wiki/ReadyBoost)). Write in large sequential batches, and keep the live journal on the SSD, flushing to the card. |
| **iGPU** | Run a 1.5-3B worker model through Vulkan (summarize, classify, lint explanations) while the dGPU or CPU runs the main model. Pin it with `GGML_VK_VISIBLE_DEVICES`. | It shares RAM bandwidth with the CPU, so running both at full load slows each one down. Measure throughput before keeping it on. |
| **E-cores / spare threads** | Gates, tests, indexing, embeddings and tokenizing, at low priority. | Putting llama.cpp decode threads on E-cores often makes it slower. Keep inference on P-cores and set the thread count to the number of P-cores. |
| **LAN machines** | An old laptop joins as an actor worker: it runs map jobs, evals, or a cheap tier over HTTP. Optionally, llama.cpp `rpc-server` lends memory for a model that is too big for one box ([PR 16441](https://github.com/ggml-org/llama.cpp/pull/16441), [benchmarks](https://github.com/kjaiswal/llama-cpp-distributed-benchmarks)). | RPC speeds up prefill but makes decode slower unless the network is fast, so use it for batch jobs, not chat. Workers must authenticate, and LAN-only should be the default. |
| **Idle time** | Overnight background worktree jobs, DSPy-style prompt tuning per class, rebuilding the skill index, page-cache warming, and chaos evals. | Pause on battery, on heat or when the user becomes active. Preemption (3.2) together with durable execution makes stopping safe. |

---

## 6. The 10 highest-value additions to the build plan

1. **Durable journal and resume** (`journal.py` with idempotency keys). It underpins preemption, background runs, replay and chaos tests. This is the biggest single win for weak PCs.
2. **ACI tool set**: windowed view, capped search, and edits that are checked for syntax before saving. It gives the largest quality gain for 3-8B models.
3. **Hardware probe plus placer**: free-VRAM layer offload, choosing between CPU, Vulkan and CUDA, the self-test, and "will it fit" badges. This is what lets Architect run well on any PC.
4. **Cascade escalation driven by gates, plus the degradation ladder**: cheap tier first, step up on gate failure, and step down under resource pressure, with the current level shown in the hub.
5. **Circuit breakers, heartbeats and bounded queues** around every backend. They keep runs from hanging and from swapping on 8 GB machines.
6. **Checkpoint timeline in the hub**: rewind, edit and re-run from a node with a different tier, with the workspace snapshot tied to each checkpoint.
7. **Background worktree runs and an Inbox page.** Slow hardware then costs no waiting time.
8. **Memory tiering manager**: page-cache warming and KV slot save/restore in idle RAM, a prompt-prefix cache backed by the CAS, and the SD card as a cold archive and skill-index tier.
9. **Skill library and reflection lessons.** Past successes and failures make small models better over time, with no fine-tuning.
10. **One event schema and local traces**, plus an idle-time optimizer (DSPy-style prompt tuning per hardware class) and a chaos eval mode. Together they prove and tune behavior on each machine class.

**Later:** the LAN worker protocol with CRDT sync (build it on items 1, 5 and 10), code-as-action mode, and the portable "Architect on a card" bundle.