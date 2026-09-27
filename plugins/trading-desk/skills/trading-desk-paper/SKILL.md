---
name: trading-desk-paper
description: Take a backtested strategy to Alpaca paper trading - replay validation, kill switches, state files, and the failure modes that only appear live. Use when wiring a live loop or debugging one.
version: 1.0.0
tags: [trading, alpaca, paper-trading, live]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

# Paper trading

A live loop is not a backtest with the dates removed. These are the parts that
only exist live.

## Validate by replay before trusting it

Run the live loop against a past date with `--replay YYYY-MM-DD` and check it
produces the same decisions the backtest did for that day. If replay and
backtest disagree, one of them is wrong and you do not yet know which.

This catches the whole class of bug where the live path computes a feature
differently from the backtest path. Share the feature code between them.

## A kill switch you can reach without a deploy

Check for a file on every cycle and exit if it exists:

```python
if (OUT / "STOP").exists():
    log("kill switch present, standing down")
    return
```

`touch state/STOP_<bot>` stops a bot from an ssh session in one command. You want
this before you need it.

## Write state the same way every bot does

Use `core/botstate.py`, which gives every bot two files:

- `state/<bot>.json`, a snapshot replaced **atomically** (tempfile then rename,
  so a reader never sees half a file)
- `state/<bot>.events.jsonl`, append-only, one JSON object per line

That is the whole contract with the dashboard. Do not invent a second format.
Bots may write other things into the state directory, so anything reading it must
identify a snapshot by its `bot` field rather than assuming every `.json` is one.

## Free data has sharp edges

- Alpaca's free tier serves **IEX**, not SIP, and blocks recent SIP data. IEX is
  a fraction of consolidated volume, so a volume feature computed on IEX is not
  the one you backtested. Know which feed produced your backtest.
- Alpaca cannot trade futures at all.
- A vendor SDK may hide paging and silently truncate. If a client returns
  suspiciously round result counts, call the REST endpoint directly and page it
  yourself, with backoff on 429.
- yfinance intraday history is short, roughly 60 days, so it cannot backtest an
  intraday strategy.

## Order mechanics

Send the stop and target as a bracket in the same request as the entry, so a
crash between placing the entry and placing the stop cannot leave a naked
position. Then reconcile open positions from the broker on startup rather than
from your own state file: the broker is the authority on what you own.

## What to watch on day one

- did the bot wake at the time you expected, in the timezone you expected
- did it find the candidates the backtest would have found
- is achieved risk per trade what you configured
- do fills land near the prices the backtest assumed, and if not, the friction
  formula in `trading-desk-method` tells you what that costs

Paper results are still hypothetical. Fills are simulated by the broker, and a
paper fill at the midpoint is not evidence you would have been filled there.
