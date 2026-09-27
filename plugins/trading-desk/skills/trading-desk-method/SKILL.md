---
name: trading-desk-method
description: The testing discipline for any trading strategy - three arms, out-of-sample splits, friction, and the specific ways a backtest lies. Use this before believing any backtest result, yours or anyone else's.
version: 1.0.0
tags: [trading, backtesting, validation, statistics]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

# The method

A backtest's default state is wrong in a way that flatters the strategy. Every
rule here exists because it caught a specific bug that made results look better
than they were. Apply them before believing a number, including your own.

## Three arms, always

Run every strategy three ways over an identical candidate set:

| Arm | What it is |
| --- | --- |
| `rules` | Take every signal. The strategy as advertised. |
| `gated` | Hand-written if-statements filter the signals. **The control.** |
| `jev` / model | The model filters the same signals. |

The `gated` arm is the one everyone skips, and skipping it makes "the AI
improved my strategy" unfalsifiable. Without a control you cannot separate *this
model has judgement* from *any filter at all would have helped*, and the second
explanation is usually the right one.

If the gated arm matches the model arm, the honest conclusion is that you did not
need the model. Report that. It is a more interesting result than a vague win.

All three arms must see the **same candidates**. If the model arm sees different
signals than the rules arm, you are comparing two strategies, not measuring a
filter.

## The number that decides it

A positive average return per trade means nothing on its own. Compute a t-stat on
mean R:

```
t = mean(R) / (std(R) / sqrt(n))
```

Under about 2, you have not shown an edge. Say so plainly. Most strategies die
here and that is the correct outcome.

Also report, every time:
- trades (a 40-trade result is a story, not evidence)
- win rate AND average R (either alone is misleading)
- max drawdown
- **train and test halves separately**

## Out of sample is not optional

Split by date, develop on the first half, and report both. A strategy that only
works on the half you developed on is a description of that half.

The test half being *better* is not automatically good news either. It usually
means the regime changed in your favour, so check whether the effect is
concentrated in one period.

## Friction decides more strategies than signals do

Model slippage and fees per side, and report `pnl_before_costs` next to net. For
a stop-based strategy:

```
friction_R ~= 2 * slippage_bps * price / stop_distance
```

A tight stop multiplies friction. Many strategies are positive gross and dead
net, which is the single most common reason a good-looking backtest loses money
live. Always report both, never just net and never just gross.

## Restriction of range

If you measure a score's ranking power only among trades that already passed a
threshold, you attenuate it toward zero and will wrongly conclude the score is
useless. Measure ranking power across the **full** candidate set.

This is not a small effect. A score can be strongly significant over all
candidates and show nothing at all inside the accepted band.

## Bugs to check for by name

Every one of these was found in a working system, and every one made the results
look better:

1. **A module shadowing a stdlib name.** `types.py` next to your code produces
   baffling circular imports. Never name a file after a stdlib module.
2. **Slippage counted twice.** Compute `ideal_pnl` from unslipped prices, then
   assert `net == ideal - slippage - fees` exactly.
3. **NaN rendered into a fact.** `"+0.00 ATR from the 200 EMA"` from a NaN is a
   fabricated input the model then reasons from. Drop candidates with incomplete
   context and warm up indicators before the trading window.
4. **A position cap silently shrinking risk.** Report achieved risk per trade,
   not just the configured number.
5. **A config variant silently identical to the base.** If two arms produce the
   same number of signals, verify they are actually different.
6. **Sizing that drops trades.** When the risk budget buys less than one unit,
   silently skipping the trade biases the sample toward low-volatility sessions.
   Take one unit under a hard risk ceiling, or record the skip.
7. **Risk in points, not currency.** For futures, multiply by contract size.
   Forgetting understates risk by the multiplier.
8. **Session boundaries by calendar midnight.** Futures trade 18:00 to 17:00 ET.
   Grouping by midnight corrupts every prior-session level.
9. **A close exactly on a level.** Decide whether "through" is inclusive, and
   apply it in one place only.
10. **Audit tolerance too tight.** A trade log rounded to 4dp will fail a 1e-6
    comparison on every level ending in 5. Use 1e-3.

## Mechanical audit

After any change to a strategy, re-verify every trade in the log against the
rules as written, and report `N/N satisfied`. Write the audit as a separate
script that reads the trade CSV, not as an assertion inside the strategy: the
point is to check the strategy, so it cannot be the thing doing the checking.

## What to say when it does not work

Most strategies do not work. Report the negative result with the same care as a
positive one: sample size, both halves, the t-stat, and what specifically killed
it. A clear no is a deliverable. Quietly tuning until the number turns positive
is how you produce a strategy that only ever worked on paper.
