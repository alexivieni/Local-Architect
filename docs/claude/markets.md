# Markets pack (strategy audits)

Added 2026-11-17 (slice M1). A pack of roles, flows and gates for people who write trading strategies. It builds on the same rule as the rest of
Architect: **code decides what passes**. It audits and repairs strategy CODE; it is not a trader and gives no advice. Every file carries a
`markets_` prefix (or `bt_`/`backtest_`), so the pack can be lifted out later.

## What it will never do
- **No live orders, no exchange keys.** There is no execution code anywhere in the pack. Data comes from a CSV the person supplies.
- **No prediction.** The roles are told never to predict returns or say a strategy will make money; every report says it is not financial advice.
- **Offline.** Nothing here touches the network; the strategy under test runs in its own process under the harness's offline guard (`core.run_cmd`).
  Fetching data (a later, online-gated step) stays behind `online.enabled`.
- **No LLM as a price signal.** A model that labels news inside a backtest leaks what it learned in training into the past.

## The strategy contract
A Python file defining `signals(bars) -> list`: one target position per bar in [-1, 1] (None or NaN = flat). `bars` is a list of dicts
`{t, open, high, low, close, volume}`. The signal for bar *i* may use bars 0..*i* only. Optional module-level `PARAMS = {"name": number}` lets the
sensitivity gate nudge each by +-20% (`signals(bars, **params)` must then accept them). The kernel (`markets_kernel.py`) fixes the timing and the
costs, not the strategy: the target decided on bar *i*'s close is filled at bar *i+1*'s open and held to *i+2*'s open, and each unit of position change
pays (fee + slippage) bps. Costs of zero are refused.

## The gates (`markets_gates.py`, registered in `arch_gates.GATES`)
| gate | what code checks | outcomes |
|---|---|---|
| `bt_data` | price file: enough bars, ordered timestamps, no impossible bars, gaps reported | no file is True with a note that later checks cannot run |
| `bt_load` | the file loads, defines `signals`, returns one value per bar in range | a missing library is None; a broken strategy is False |
| `bt_leak` | **truncation invariance**: signals on a prefix equal the same bars' signals on the full data (5 cuts); two separate runs agree | leak or unrepeatable is False; runs on synthetic bars when no price file is given and says so |
| `bt_costs` | net return after costs is positive and the strategy traded | needs the price file, else None |
| `bt_oos` | the last 30% is profitable after costs with at least 20 trades | too few trades is None |
| `bt_trials` | deflated Sharpe (Bailey and Lopez de Prado) against the variants in `.arch/markets/trials.jsonl`; same strategy + data is one trial | needs the price file, else None |
| `bt_sensitivity` | +-20% on each PARAMS entry keeps at least 75% of variants profitable | no PARAMS is a labelled "not applicable" pass |
| `bt_scan` / `bt_fewer` | all of the above before a repair and again after it | see below |

Defaults live in `markets_gates.DEFAULTS` and can be overridden per step or by a `markets` section in the config (`fee_bps`, `slippage_bps`,
`oos_share`, `min_trades`, `dsr_min`, ...). They are placeholders: set what you really pay. A value that is not a number or is out of range (`oos_share` 5, `fee_bps` "abc") is refused by name and the gate answers
"could not check" with that reason, never a silent default. There is no Settings page for them yet (slice M2). The trial ledger skips damaged lines but keeps the good ones (forgetting trials would flatter the result).

## Defects versus verdicts (the rule that keeps a repair honest)
`load` and `leak` are **defects in the code**; `costs`, `oos`, `trials` and `sensitivity` are **verdicts about the idea**. `backtest_fix` shows the model
only the defects, and `bt_fewer` accepts a repair only if it has fewer defects, introduced no look-ahead, still trades (at least half its old trades)
and changed only the strategy file (the price data is evidence and is protected). Tuning code until the verdict gates pass would be fitting the audit,
so they are reported after the repair and never used as its target. Removing look-ahead often reveals that a strategy had no edge: that is the
answer, and the flow's verdict says fail.

## Flows and roles
`backtest_audit` (read-only: audit, plain-words explanation by `backtest_auditor`, `docs/backtest_audit.md`) and `backtest_fix` (audit, `quant_dev`
repairs the file, same audit again, `docs/backtest_fix.md`). `market_data_engineer` exists for the data flows of slice M2. Verdicts: pass only if
nothing failed and nothing could not be checked; any gate that could not run makes it not_sure. Routing keywords: backtest / trading strategy /
look-ahead / sharpe go to the audit; "fix ... look-ahead" goes to the fix flow.

## Tests and evals
`arch_selftest_markets.py` (real worker processes, a scripted model). Eval families `backtest_audit` and `backtest_fix` (fixtures in
`evals/families/_markets_bank.py`; the judge `bt_gate` in `arch_eval.py` re-runs a gate on the finished project and, for fixes, requires the strategy to
still trade). The fixtures are a seeded random walk: "clean" means "does not look ahead", never "makes money".

## Not built yet (slices M2 to M4)
The `backtest`, `datafeed` and `scrape` flows, the Markets place in the hub and its on/off switch, Settings for the defaults, risk and paper-trading
simulation, and the chain flows. Until M2 the new roles show on the Roles page like any other.
