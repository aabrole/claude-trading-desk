---
description: Add a new strategy to the harness, wired for all three test arms
---

Create a new strategy in this workspace. Ask the user what the rule is if they
have not said.

1. Load the `trading-desk-strategy` skill and follow its contract.
2. Create `<name>/` at the workspace root, next to `core/`, never nested deeper:
   strategies resolve `core/` as `../core`.
3. Write `<name>/strategy.py` with `feature_cols`, `prepare`, `snapshot`,
   `gates` and `jev_prompt`. Copy `orb/strategy.py` as the model.
4. Write `<name>/run_backtest.py` running the `rules` and `gated` arms. Do not
   add the model arm yet.
5. Beside every indicator, state in a comment why it is causal at that bar.
6. Run it. Then check, and report honestly:
   - does the gated arm produce a sane number of trades? Zero means a gate can
     never pass, which is a bug in the gate, not a result.
   - t-stat on mean R, both halves separately, gross next to net.
7. Write an audit script that re-verifies every trade in the output CSV against
   the rules as written, and report `N/N`.

Do not add the model arm until the first two arms are understood. Do not tune
parameters to make a number positive, and say so if the result is that the
strategy does not work.
