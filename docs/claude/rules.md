# Rules in full

The rules the index lists, with their reasons and the incidents behind them. Moved out of CLAUDE.md on 2026-11-08, word for word except the corrections noted in the commit.

## Three outcomes, never two
A check result is `True` (ran and passed), `False` (ran and failed) or `None` (**could not run**). `Engine.mark` keeps all three on disk.
`None` is not a failure: it is excluded from training in both directions, and it makes the run's verdict `not_sure`. Anything that
collapses it to `False` — `bool(gate_ok)` is the easy mistake — teaches the model that unverifiable work is bad work.

## Gates are the product
Unlike a generic harness, Architect's gates (tests, verifier, fake-code gate, shrink guard, question detector, missing-import check) are the point: a small
local model is only trustworthy because code checks its work. Do not remove them to make a run pass; make the run pass the checks.

## A role you assign wins over a tier
A YAML step's `tier:` is the default for that work. A model the person assigned to the role by name wins over it (`_pin_tier`), except
`heavy`, which is an explicit escalation. Before this was fixed, every data-flow step silently overrode the person's role assignments.

## One settings file
Inside the Architect, `model_hub.read_config/write_config` must target `arch_core.CONFIG_FILE`. `model_hub` now follows it automatically when
`arch_core` is loaded; code that writes settings should still set `mh.CONFIG_FILE = core.CONFIG_FILE` at the point of use. Writing to
`hub_config.json` instead made `port --apply` and `promote-model` change a file nothing reads.

## Settings safety: tests isolate ARCHITECT_HOME
A test or tool must never read-modify-write the real `architect_config.json`. Every suite sets a throwaway `ARCHITECT_HOME` **and** `ARCHITECT_DATA_DIR` before importing `arch_core`; anything that calls `write_config`, `port --apply`, `tune`, `select --apply` or `promote-model` in a test does so inside that folder. Back up before any manual config edit.

## The self-tests own their data folder
Every suite runs against a throwaway `ARCHITECT_DATA_DIR` (`architect.py::_selftest_suites`, and `arch_selftest.run()` for a standalone
run). The suites drive real `Engine` calls with the scripted stand-in `fake:7b` and mark the replies accepted, which is exactly how
`hub_data/ledger.json` is written — without isolation the install learned that a test fixture was "a past reply of yours that passed the
checks" and fed it into real prompts, and the suites stopped being repeatable. A suite that scripts a model must also set
`ARCHITECT_NO_CACHE=1`: a scripted fake answers the same prompt differently on successive calls, which a real temperature-0 model never
does, so `arch_cache` correctly collapses the two and the script falls out of step.

## Cross-Platform Compliance: Antigravity <-> Claude Code Anti-Stub & Non-Destruction Protocol
When switching between **Google Antigravity**, **Claude Code**, and **Local-Architect**, every agent MUST obey these non-negotiable rules so no platform ever corrupts or stubs out work created on another:
1. **Zero Stubs / Zero Placeholders (`arch_fakecode.py` Gate):** Never replace working functions, classes, or UI sections with `pass`, `...`, `TODO`, `FIXME`, `NotImplementedError`, hardcoded dummy returns, or `// rest of code here`. `python architect.py fakecode` and `arch_fakecode.scan()` must report **0 gate-level findings**.
2. **Surgical Edits Only (Shrink Guard & `DEF:` Blocks):** Never truncate or rewrite whole modules when changing a function. Existing functions, exports, and comments must be preserved. In `hub_ui.html`, two byte-slices are explicitly guarded by `arch_selftest_cachespage.py` (`/* ===== Caches (Models):` to `/* ---- Benchmarks:`) and `arch_selftest_costmodel.py` (`/* ===== Cost map (Models):` to `function benchPage(){`) — never edit inside those slices, never use `font-size:...px` in `hub_ui.html` (use `rem` or `var(--t-*)`), and keep every `SIDEBAR` place at **<= 5 sub-tabs** (`arch_selftest_hubcalm.py`).
3. **Strict Config & Data Isolation:** Never read-modify-write the workspace's `architect_config.json`, `hub_data/`, `.arch/`, `bin/`, `portfolio.json`, or `.env` during tests or verification. Always set throwaway `ARCHITECT_HOME` and `ARCHITECT_DATA_DIR` in temp directories, and delete `architect/hub_data` if `tools/ci_parallel.py` writes a CI summary file there.
4. **Verify Lessons & Suites Before Handing Off:** Every architectural rule in `data/lessons.json` is verified by `py -3.12 arch_lessons.py` (`0 problems`) and mirrored in `docs/PRINCIPLES.md`. Run `arch_lessons.py`, `arch_selftest_finops.py`, `arch_selftest_hubui.py`, `arch_selftest_hubcalm.py`, `arch_selftest_a11y.py`, and `arch_hub_check.py` before concluding any cross-platform session.
