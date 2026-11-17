Repair this trading strategy so the audit passes, changing only the strategy file.

The contract: the module defines signals(bars) and returns a list with exactly one target position per bar, each in [-1, 1] (None means flat). `bars` is a list of
dicts with keys t, open, high, low, close, volume. The signal for bar i may use bars 0..i and nothing after them. The kernel fills at the NEXT bar's open and charges
fees and slippage, so do not model fills or costs.

Typical causes of the findings below:
- look-ahead: a series shifted backwards (shift(-1), c[i+1]), a mean/std/quantile of the WHOLE series used to scale or threshold, a value read from the last bar to
  decide an earlier one. Rebuild the signal from bars up to and including bar i only.
- a result that does not survive costs or out-of-sample data: do not tune to the audit; trade less often or on larger moves, and say why in a comment.
Do NOT make the strategy stop trading to pass: a strategy with no trades is rejected. Keep a module-level PARAMS dict for tunable numbers. Never edit the data.

FINDINGS:
{findings}

STRATEGY:
{context}

{feedback}
