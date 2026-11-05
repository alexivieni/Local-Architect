# Test strategy: from fakes to this machine

Architect's claims are about a real laptop (RTX 4050 6 GB, 32 GB RAM, Windows 11) running real local models. Most of our checks
run against a scripted fake model, which proves the harness logic but not that the optimisations work. This page sets out the three
layers, what each one proves and what it does not, the gaps that remain, and the targets.

## The pyramid

| Layer | What runs | Time | Proves | Does NOT prove |
|---|---|---|---|---|
| **Unit**: `selftest --suite core/all/full` (`arch_selftest_*.py`) | Engine, flows, gates, server plan math, caches and settings, all against `fake:7b`, scripted HTTP stubs and temp data dirs | minutes | Control flow, three-outcome gates, plan/fit arithmetic, parsers, budget enforcement, undo paths, cleanup's "is it ours" rules | That a real model follows the formats; real speeds; that llama-server accepts the flags; Windows process/VRAM behaviour |
| **Integration**: `arch_hub_check`, `crew --fake`, `select --fake/--plan`, `eval --fake` (oracle), `flows --validate`, `self-verify` | The real CLI and hub endpoints end to end, with the oracle standing in for the model | minutes | Wiring between modules, YAML flows and agents load and run, eval judges accept the reference and reject the wrong solution, the install matches its manifest | The model's quality, GPU placement, any measured number |
| **Live**: `tools/verify_machine.py --gpu` | Real llama-server and Ollama on this machine, fixed prompts, read-only crew, one eval task | 20-60 min | Placement (ngl == layers), decode tok/s, n-gram speculation gain, prefix reuse, slot save/restore, keep_alive unload, cleanup on exit, edit format, crew/eval on a real model, telemetry written | Behaviour on other GPUs and drivers; long-run stability; statistical quality (one eval task is a smoke test, not a benchmark) |

`tools/verify_machine.py --cpu-only` is the bridge. It runs layers 1 and 2 against the **real install** (real settings, real
passports, the real binary's caps, read-only Ollama `/api/tags` and `/api/ps`) without loading a model, so it is safe to run while a
GPU queue is busy. Every check answers PASS, FAIL or NOT_SURE. NOT_SURE means the check could not decide (a dependency was missing,
or the endpoint did not report a number) and never counts as a pass, the same rule as the gates. Reports are written to
`hub_data/verify/<stamp>.json` and `.md`. The tool's own logic is tested with fakes in `arch_selftest_verifymachine.py`.

## Coverage gaps (no live test before this tool; the GPU mode now covers the ones marked *)
- n-gram speculation speed on a real edit prompt (claimed 56 -> 306-343 tok/s; measured once by hand)*
- prefix reuse on a real llama-server: `cache_n` / `prompt_n` across retries of the same prompt*
- slot save/restore (`slot_io`) against a server started with `--slot-save-path`*
- Ollama idle unload (`keep_alive`) and the embedding model's `keep_alive 0`* (the second is not yet covered)
- cleanup on exit: `cleanup --yes` leaves nothing of ours and leaves foreign Ollama models alone*
- telemetry events present for a real run*
- crew on a real model (`explain_squad`, read-only)*
- one real eval task through the live transport*
- edit-format probe for the configured worker*
- placement: worker `ngl` equals the layer count when the plan says it fits*
- `select --screen` (a real model screen: GPU minutes; still manual)
- MoE expert offload (`--n-cpu-moe`, `heavy_beside`) speed and switching time
- draft-model / MTP speculation (`server.draft = try`)
- make-room / step-aside and come-back under a real Ollama load
- GPU dropout detection (`_gpu_lost`) on the no-MUX laptop
- crash-restart watchdog with a killed server
- hub pages in a real browser (only `arch_hub_check` and the occasional Playwright pass)
- the heavy lock under two real concurrent heavy jobs
- long overnight runs: memory growth, log rolling, the `live.log` recorder

## Targets
- Unit + integration: `selftest --suite full` clean apart from the listed known flake (the security suite's Windows port-8000 /
  SQLite-lock cases), on every change. `verify_machine --cpu-only`: no FAIL.
- Live, on this machine, after any change to the server, transport, cache or cleanup code and at least weekly:
  - placement PASS (all worker layers on the card for the default worker);
  - decode >= 25 tok/s median for the default 4B-8B worker (the tool's hard floor is 5 tok/s, which only catches a CPU fallback);
  - n-gram on/off >= 1.5x on the edit prompt;
  - prefix reuse: retry 3 has `cache_n` > 0.9 x the prompt length;
  - slot save and restore both true; keep_alive unload within 30 s; cleanup leaves zero of our processes and every foreign model;
  - crew, eval and edit-format probe PASS; telemetry has events for the run.
- Any NOT_SURE in the live layer is a missing measurement and gets a ticket, never a silent pass.
- Next tools to add: `select --screen` on two small models, MoE heavy speed, draft speculation, a browser pass over the four hub tabs.

## How to run
```
py -3.12 tools/verify_machine.py --cpu-only               # safe at any time
py -3.12 tools/verify_machine.py --cpu-only --selftest-suite core   # faster
py -3.12 tools/verify_machine.py --gpu                    # in the GPU queue only; always ends with cleanup
py -3.12 tools/verify_machine.py --list
py -3.12 arch_selftest_verifymachine.py                   # the tool's own tests (fakes)
```
