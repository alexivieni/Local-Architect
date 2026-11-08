# Architect

A local, offline agentic engineering team: 46 roles and 29 flows (every one a YAML file under `flows/`, `explore` being the tool loop; counted 2026-11-07), four model tiers on a resident local server (llama-server, Ollama as the fallback), run by the models on this machine. Python standard library only; no cloud
model on the default path (Claude is an optional tier behind `arch_cloud.allowed()`), no OpenAI-style endpoint. Code decides what passes (tests, AST checks, the verifier, the fake-code gate); a model's verdict is re-validated in code.

## Never break these (details: docs/claude/rules.md)
1. **Gates are the product.** Code decides what passes. Never remove or weaken a gate to make a run pass; make the run pass it.
2. **Three outcomes.** A check is True, False or None (could not run). None makes the verdict `not_sure`; never collapse it to False
   (`bool(gate_ok)` is the easy mistake) or to a pass.
3. **The person's things are untouchable.** Tests and tools never read-modify-write `architect_config.json`, `hub_data/`, `.arch/`,
   `bin/`, `portfolio.json` or `.env`: every suite sets a throwaway `ARCHITECT_HOME` and `ARCHITECT_DATA_DIR`. Never remove, hide or
   reassign installed models; downloads only after an explicit yes. Every write to settings has an undo.
4. **Offline by default.** Nothing remote on the default path; online features sit behind `online.enabled = false` and the app behaves
   the same with it off. Self-tests never call Claude (`arch_cloud.allowed()`).
5. **Claude-written content is never fine-tuning data** (retrieval, playbooks and statistics only). Not_sure is excluded both ways.
6. **No stubs, surgical edits.** Never replace working code with `pass`, `...`, TODO or dummy returns; never rewrite a module to change
   one function. `hub_ui.html`: no `font-size: ...px`, at most 5 sub-tabs per place, two byte-slices are guarded (rules.md).
7. **Prompts are prefix-stable**: the stable part first, `{feedback}` and failure text last; few-shot at the end of the user turn.
8. **Process identity is `arch_proc`.** Never `os.kill(pid, 0)` on Windows: it is TerminateProcess.
9. **Test with `py -3.12`** (on this laptop `py -3` is 3.15 without pytest; `architect.py doctor` says which Python runs the tests).

Before handing off: `py -3.12 tools/ci_parallel.py` (a suite that fails only under load is rerun alone and listed apart),
`py -3.12 architect.py flows --validate`, `py -3.12 tools/doc_counts.py`, `py -3.12 tools/swallow_audit.py` (no new `except Exception: pass`), `py -3.12 arch_lessons.py`, then `self-verify --write`.

## How to run
```bash
python architect.py ready            # first run: repair, model, tune, optimize, prove, edit-format check, smoke test
python architect.py run "goal"       # one task; the router model picks the flow
python architect.py livetest         # does it work with MY models? real tasks, judged by code
python architect.py fit              # weights + KV cache per team model against this GPU; what a swap costs
python architect.py index --query "add throttling"   # keyword vs vector vs fused ranking for one request
python architect.py start              # detect the machine, start the resident server when installed, open the hub
python architect.py server plan        # which model serves which tier, on which port, with what on the GPU (plain words + numbers)
python architect.py passport           # every installed model: known facts + what was measured here (works with Ollama stopped: it reads the manifests)
python architect.py explain --workload edits   # model options for this machine, plain words first
python architect.py server fetch --yes # the one-time download of llama-server (the only online step; asks first)
python architect.py eval --quick       # 10 fixed tasks per flow with your models: pass rate, tokens, latency; --fake = the oracle, no GPU
python architect.py eval --quick --label candidate --compare baseline   # the regression gate (exit 1 on a regression)
python architect.py port               # which models THIS machine should use for each tier, and why (--apply to save)
python architect.py port --pack me.zip # take your settings, notes, skills, commands and training data to another computer
python architect.py commands           # the saved requests: /review, /fix, /secure, /explain, /speedup, /testgen, /cleanup
python architect.py run "/fix paginate() returns page 0 rows for page 1"
python architect.py hooks --example    # your own commands at fixed points in a run
python architect.py receipt --open     # the last run as one HTML page you can keep
python architect.py dataset            # verified work -> SFT, preference pairs, distillation samples, tool trajectories
python architect.py promote-model --tier worker --model my-worker --compare baseline
python architect.py selftest --suite full
python architect_hub.py              # the hub (or START_HUB.bat)
```

## Where the rest is (read the page before working in its area)
| Page | What it holds |
|---|---|
| `docs/claude/layout.md` | Layout (the real tree): every module and what it owns; The one screen (Console, events, keys) |
| `docs/claude/conventions.md` | Conventions (new flow, role, setting, gate, budgets, evidence tiers, prefix-stable prompts); Self-improvement; Local model guidance |
| `docs/claude/rules.md` | Three outcomes; Gates are the product; A role you assign wins over a tier; One settings file; Settings safety; The self-tests own their data folder; Cross-Platform Compliance (anti-stub, guarded hub slices) |
| `docs/claude/small-models.md` | What makes a small model finish a build: DEF blocks, interface first, BestState, prompt fitting, wire_contract, escalation, heavy jobs, ensure(wanted), MoE split, drafts, speculative decoding, role resolution, machine audit, Make room, GPU dropout, launcher, crash recorder, Docker/WSL, cleanup, ollama_gpu, caches, triage |
| `docs/claude/claude-tools.md` | Rules learned building the Claude tools (never in self-tests, full window, agents own files, never training data); Transfer pack |
| `docs/claude/decisions.md` | Architecture review 2026-11-07 (arch_proc, one writer per tier, rollback snapshots, three outcomes reach the run, hub epochs); Your versions (custom layer); End-to-end check 2026-11-08; v12 Breakthroughs; Tech-debt Phase 1 (doc counts, CI LOAD reruns, Ctrl+C for servers, patient meta writes); Phase 2 (browser smoke, swallow ratchet, engine extensions) |

A comment in the code that cites "CLAUDE.md, <section>" means the section of that name in the page above.

## Open issues (known, not fixed)
- The escalation fallback ignores `role_hosts`: when an escalated model gives no answer, `Engine.call` goes back to the role's own
  model on THIS machine (`host=None`) even when that role normally runs on another machine (small-models.md).
