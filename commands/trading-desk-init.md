---
description: Scaffold a new trading workspace from the kit, with the harness, dashboard and deploy stack in place
---

Set up a trading workspace for the user in the current directory.

1. Confirm the target directory with the user if it is not empty.
2. Copy the plugin's `template/` into place: `core/`, `dashboard/`, `deploy/`,
   and the `orb/` example strategy. Keep the layout exactly as it is, because
   each strategy resolves `core/` as `../core` and moving directories breaks
   those imports.
3. Create `.env` from `deploy/.env.example` and list which keys are needed for
   what. Never invent or guess a key value. Never print a key the user gives you
   back into a file the user might commit, and check `.gitignore` covers `.env`.
4. Run `git init` and make a first commit if the directory is not already a repo.
5. Verify the harness imports cleanly:
   `python3 -c "import sys; sys.path.insert(0,'core'); import engine, decision, contracts, botstate"`
6. Tell the user the three things they can do next, shortest first:
   - `python3 example_sma/run_backtest.py` needs no API key at all
   - `python3 orb/run_backtest.py --arms rules gated` needs Alpaca (free)
   - `python3 dashboard/server.py` opens the desk

Read the `trading-desk-method` skill before reporting any backtest result.
