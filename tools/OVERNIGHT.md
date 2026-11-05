# The overnight run

`tools/overnight.py` runs one unattended night on this machine. It uses your local models, never changes your settings, and costs $0
unless you pass `--with-claude`. In the morning, read `hub_data/learning/morning_<date>.md`.

## What it does, in order

1. **Benchmarks.** It runs the `distributed` and `harness` eval families, then the representative set (`eval --set representative`, the
   same 17 tasks every night, compared task by task with the last run). Each one goes through the normal `architect.py eval` command,
   one at a time, at below-normal priority. For the whole night it holds the heavy-job lock (`hub_data/heavy.lock`), so a test suite, a
   tune or a second benchmark waits instead of sharing the GPU. A family that is not on this install yet is skipped, and the note
   says so. Evals cannot use Claude: `arch_cloud` refuses during eval.
2. **Training export.** This is `arch_dataset` with near-duplicates dropped (`dedupe`) and samples below the quality floor left out
   (`--min-quality`, default 0.5). It writes to `hub_data/datasets/`. The note counts the samples added since the last night
   (`hub_data/learning/overnight_state.json`).
3. **Logbook.** `arch_learn.ingest()` reads everything new, then the playbooks are rewritten (`hub_data/memory/playbooks.md`).
4. **Lessons.** `arch_overseer.lessons()` adds to `hub_data/learning/LESSONS.md` when it has something new to say.
5. **Archive.** If Settings > Storage > Archive folder is set, it copies eval reports, the export, the playbooks, the lessons and any runs
   whose earlier copy was missed into `<archive>/architect_archive/_overnight/`. It only copies: nothing is moved or deleted, files the
   archive already holds at the same size are not copied again, and a missing (unplugged) drive is listed as a problem.
6. **Morning note.** It records what ran, the pass rates, the new training samples, the cost (local = $0, plus Claude only with
   `--with-claude`) and any problems.

The night's full console output is kept in `hub_data/learning/overnight_<date>.log`.

## Limits and stopping

- `--hours 7` is the default time limit. An eval that would run past it is stopped, and the evals after it are skipped. The last 15
  minutes are kept for steps 2-6, which always run.
- **Stop tonight:** create `hub_data\OVERNIGHT_STOP`, for example with `type nul > hub_data\OVERNIGHT_STOP`. The current eval is ended,
  the remaining steps are skipped, and the note is still written. The file is removed at the end, so tomorrow's night runs normally. A
  stop file left over from before the night started is ignored.
- **Pause every night:** create `hub_data\overnight.pause`. Each night then does nothing until you delete it.
- `hub_data\STOP` (the app's own stop file) still stops any run between model calls, as it always has.

## Try it first

```bat
tools\overnight.bat --dry-run
tools\overnight.bat --hours 1 --quick
```

The first shows what would run, and runs and writes nothing. The second is a short real night: the first 10 tasks of each family.

Other options: `--families distributed,harness` (which families run before the representative set), `--min-quality 0.5`,
`--python <exe>`, and `--with-claude --claude-usd 0.10`.

`--with-claude` makes one extra call: Claude reads the note and lists what to look at first. The call goes through `arch_cloud`'s own
gate, so it needs Settings > Online features turned on and stays within the spend caps. It is also capped per night by `--claude-usd`.
When the gate says no, nothing is sent, and the note says why.

## Schedule it with Windows Task Scheduler

The tool never changes system settings itself. To run it every night, create the task yourself, either way:

**Command line** (paste into a normal Command Prompt; change the folder to where Architect lives):

```bat
schtasks /Create /TN "Architect overnight" /SC DAILY /ST 01:00 /TR "\"C:\Users\alexi\Downloads\architect_v12\architect\tools\overnight.bat\" --hours 6"
```

This runs only while you are logged in, which is enough on a laptop left on overnight. To remove it:
`schtasks /Delete /TN "Architect overnight" /F`. To run it now as a test: `schtasks /Run /TN "Architect overnight"`.

**Or by hand:** open Task Scheduler and choose Create Basic Task. Pick Daily at 01:00. For the action, choose Start a program, with
Program `C:\Users\alexi\Downloads\architect_v12\architect\tools\overnight.bat`, Arguments `--hours 6`, and Start in
`C:\Users\alexi\Downloads\architect_v12\architect`. Then, under Properties:
- Conditions: tick "Wake the computer to run this task" if you want that. On a laptop, untick "Start only if on AC power" only if you
  really mean to benchmark on battery.
- Settings: set "Stop the task if it runs longer than" to a little more than `--hours` (for example 7 hours for `--hours 6`).
  overnight.py stops itself first; this is a backstop.

`overnight.bat` finds Python the same way `START_HERE.bat` does. It then asks `start.py`'s `python_for_tests()` for a Python that has
pytest, because the evals run your projects' tests. On this laptop that is 3.12, not the 3.15 the `py` launcher picks. It never pauses
for input, so it is safe to run unattended. The exit code is 0 when every benchmark produced a report, and 1 when one could not. Task
Scheduler shows it as "Last Run Result".

## Before the first night

- The resident server or Ollama must answer, or every eval reports "needs a model server" (listed under Problems). Running
  `architect.py start` at logon, or leaving the hub open, covers this.
- Check it end to end without a GPU: `python tools/check_overnight.py` should end with `ALL N OVERNIGHT CHECKS PASSED`. It uses a
  throwaway data folder, stand-in eval children and a fake Claude, so nothing real is touched.
