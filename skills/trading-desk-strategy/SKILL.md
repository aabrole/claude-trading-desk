---
name: trading-desk-strategy
description: Add your own strategy to the trading harness - the strategy object contract, the plan DataFrame the engine reads, and the causality rules that keep a backtest honest. Use when writing or reviewing a strategy file.
version: 1.0.0
tags: [trading, strategy, backtesting]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

# Writing a strategy

The harness runs any object with this surface. Copy `orb/strategy.py` and replace
the logic.

```python
class MyStrategy:
    feature_cols: list[str]                      # what gates and the model may read
    def prepare(self, df) -> pd.DataFrame        # add indicators and a signal column
    def snapshot(self, sym, ts, row) -> Snapshot # describe one candidate
    def gates(self) -> list[Gate]                # hand-written filters: the control arm
    def jev_prompt(self) -> JevPrompt            # what to ask the model
```

## The plan DataFrame

`prepare` returns the input bars plus these columns, which the engine reads
directly:

| Column | Meaning |
| --- | --- |
| `signal` | `'long'`, `'short'`, or `''` |
| `stop`, `target` | absolute prices for the proposed trade |
| `minutes_from_open` | used for the end-of-day flatten |
| your `feature_cols` | numbers the gates and the model can read |

## The one rule that matters most

**Every column must be computable from data available at that bar.** One
indicator that peeks a single bar ahead produces a beautiful equity curve and a
worthless strategy.

Concretely:
- `shift(1)` before comparing to a previous close
- a rolling window ending at the current bar, never centred
- a level from the prior session, never from the session you are trading
- if you group by day, verify the boundary matches the instrument's real session

Write the causality assumption as a comment next to each indicator. When you
revisit it in a month, that comment is the only thing standing between you and a
lookahead bug.

## Drop incomplete candidates

If any feature is NaN, drop the candidate. Do not fill with zero.

A zero becomes a *fact* in the context you hand the model ("0.00 ATR from the
200 EMA"), and a fabricated fact is worse than a missing trade. Warm up
indicators for enough bars before the window you trade.

## Gates are the control arm, so write them properly

Write the judgement calls you would make by eye as if-statements returning a
veto reason:

```python
def slope_gate(s: Snapshot) -> Optional[str]:
    if s.f("fast_slope_atr") < 0.05:
        return "average_not_turning"
    return None
```

Return a short stable string, because the harness counts vetoes by reason and
that breakdown tells you which filter is doing the work.

**Check each gate can actually pass.** A gate that vetoes every candidate looks
like a strategy with no trades. A real example: filtering a moving-average
crossover on "minimum separation between the averages" vetoes every signal,
because at a crossover the averages are equal by definition.

If the arm reports zero trades, the gate is broken, not the strategy.

## Then run it

```bash
python3 <strategy>/run_backtest.py --arms rules gated
```

Read the result against `trading-desk-method`: three arms, both halves, a t-stat,
gross next to net. Add the model arm only once the first two make sense.
