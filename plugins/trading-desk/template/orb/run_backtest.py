#!/usr/bin/env python3
"""
run_backtest.py
===============
Runs the ORB strategy through up to three decision layers over the same data
and prints them side by side.

  python3 run_backtest.py --symbols SPY QQQ --start 2024-01-01 --arms rules gated
  python3 run_backtest.py --arms rules gated jev --max-jev-calls 500

The candidate set is identical across arms by construction: the strategy finds
the breakouts, and the arms only differ in which ones they agree to take.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics as M                                    # noqa: E402
from data import DEFAULT_UNIVERSE, fetch_universe      # noqa: E402
from decision import GateDecider, JevDecider, RuleDecider  # noqa: E402
from engine import Engine, EngineConfig                # noqa: E402
from strategy import ORBConfig, ORBStrategy            # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the ORB strategy across decision layers.")
    p.add_argument("--symbols", nargs="+", default=None,
                   help=f"default: {' '.join(DEFAULT_UNIVERSE)}")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--arms", nargs="+", default=["rules", "gated"],
                   choices=["rules", "gated", "jev"])

    g = p.add_argument_group("strategy")
    g.add_argument("--or-minutes", type=int, default=15)
    g.add_argument("--exec-minutes", type=int, default=5)
    g.add_argument("--rr", type=float, default=2.0)
    g.add_argument("--min-atr-mult", type=float, default=0.5)
    g.add_argument("--max-atr-mult", type=float, default=2.0)
    g.add_argument("--last-entry-min", type=int, default=135)
    g.add_argument("--require-retest", action="store_true")
    g.add_argument("--fakeout-reentry", action="store_true")

    e = p.add_argument_group("execution")
    e.add_argument("--equity", type=float, default=10_000.0)
    e.add_argument("--risk-pct", type=float, default=0.005)
    e.add_argument("--max-positions", type=int, default=3)
    e.add_argument("--slippage-bps", type=float, default=1.0)
    e.add_argument("--fill", default="close", choices=["close", "next_open"])
    e.add_argument("--ambiguous", default="stop_first", choices=["stop_first", "target_first"])
    e.add_argument("--no-shorts", action="store_true")
    e.add_argument("--fetch-start", default=None,
                   help="data window to load, instead of start-minus-warmup. Lets you "
                        "reuse a wider cached range without hitting the API.")
    e.add_argument("--fetch-end", default=None,
                   help="upper bound of the data window (default: --end)")
    e.add_argument("--warmup-days", type=int, default=45,
                   help="extra calendar days fetched before --start so the 200 EMA "
                        "and long ATR are warm on the first tradable session")

    j = p.add_argument_group("jev")
    j.add_argument("--jev-threshold", type=float, default=0.55)
    j.add_argument("--jev-offline", action="store_true", help="use cached decisions only")
    j.add_argument("--max-jev-calls", type=int, default=2000,
                   help="abort before running up an unexpected bill")
    j.add_argument("--jev-fallback", default="wait", choices=["wait", "rule"])
    j.add_argument("--prefetch-workers", type=int, default=5,
                   help="concurrent Jev calls. 0 disables prefetch (sequential). "
                        "Rate limit is 1,200/min, so ~5 workers is the ceiling.")

    p.add_argument("--tag", default="", help="suffix for output filenames")
    return p.parse_args()


def build_plans(args) -> dict:
    warm_start = args.fetch_start or (
        pd.Timestamp(args.start) - pd.Timedelta(days=args.warmup_days)).date().isoformat()
    bars = fetch_universe(
        symbols=args.symbols, start=warm_start, end=args.fetch_end or args.end,
        minutes=args.exec_minutes,
    )
    cfg = ORBConfig(
        or_minutes=args.or_minutes, exec_minutes=args.exec_minutes, rr=args.rr,
        min_atr_mult=args.min_atr_mult, max_atr_mult=args.max_atr_mult,
        last_entry_min=args.last_entry_min,
        require_retest=args.require_retest, fakeout_reentry=args.fakeout_reentry,
    )
    strat = ORBStrategy(cfg)
    plans = {}
    total_signals = 0
    trade_from = pd.Timestamp(args.start, tz="America/New_York")
    trade_to = pd.Timestamp(args.end, tz="America/New_York") if args.end else None
    for sym, df in bars.items():
        plan = strat.prepare(df)
        # Warm-up and out-of-window bars built the indicators; they are not tradable.
        plan = plan[plan.index >= trade_from]
        if trade_to is not None:
            plan = plan[plan.index <= trade_to]
        n = int((plan["signal"] != "").sum())
        total_signals += n
        plans[sym] = plan
        print(f"[prep] {sym}: {n} breakout candidates over {plan.index.normalize().nunique()} sessions")
    print(f"[prep] {total_signals} candidates total\n")
    return plans, strat, total_signals


def main() -> None:
    load_env(ROOT / ".env")
    args = parse_args()
    plans, strat, n_candidates = build_plans(args)

    ecfg = EngineConfig(
        starting_equity=args.equity, risk_pct=args.risk_pct,
        max_positions=args.max_positions, slippage_bps=args.slippage_bps,
        fill=args.fill, ambiguous=args.ambiguous, allow_shorts=not args.no_shorts,
    )

    results, all_trades = {}, {}
    for arm in args.arms:
        if arm == "rules":
            decider = RuleDecider()
        elif arm == "gated":
            decider = GateDecider(strat.gates())
        else:
            if n_candidates > args.max_jev_calls and not args.jev_offline:
                print(f"[jev] {n_candidates} candidates exceeds --max-jev-calls "
                      f"({args.max_jev_calls}). Narrow the run or raise the cap.")
                continue
            decider = JevDecider(
                prompt=strat.jev_prompt(), threshold=args.jev_threshold,
                offline=args.jev_offline, fallback=args.jev_fallback,
                log_path=OUT / f"decisions_jev{'_' + args.tag if args.tag else ''}.jsonl",
            )

        print(f"=== arm: {arm} ===")
        if arm == "jev" and args.prefetch_workers > 0 and not args.jev_offline:
            snaps = []
            for sym, plan in plans.items():
                sig = plan[plan["signal"] != ""]
                for ts, row in sig.iterrows():
                    snaps.append(strat.snapshot(sym, ts, row))
            decider.prefetch(snaps, workers=args.prefetch_workers)

        engine = Engine(ecfg)
        engine.set_feature_cols(strat.feature_cols)
        res = engine.run(plans, strat, decider, verbose=False)
        m = M.summarize(res.trades, res.equity_curve, args.equity,
                        multiplier=ecfg.instrument.multiplier)
        m["_candidates"] = res.diagnostics.get("candidates", 0)
        m["_approved"] = res.diagnostics.get("approved", 0)
        results[arm] = m
        all_trades[arm] = res.trades_df()
        res.equity_curve.to_csv(OUT / f"equity_{arm}{'_' + args.tag if args.tag else ''}.csv",
                                header=["equity"])

        print(f"  candidates {res.diagnostics['candidates']}, "
              f"approved {res.diagnostics['approved']}, "
              f"rejected {res.diagnostics['rejected']}, "
              f"ambiguous bars {res.diagnostics['ambiguous_bars']}")
        if res.decider_stats:
            print(f"  decider: {json.dumps(res.decider_stats)}")
        print()

    if not results:
        print("No arms completed.")
        return

    tag = f"_{args.tag}" if args.tag else ""
    with open(OUT / f"config{tag}.json", "w") as f:
        json.dump(vars(args), f, indent=2, default=str)
    print("=" * 78)
    print(M.compare(results).to_string())
    print("=" * 78)

    for arm, df in all_trades.items():
        if df.empty:
            continue
        path = OUT / f"trades_{arm}{tag}.csv"
        df.to_csv(path, index=False)
        print(f"\n[{arm}] {len(df)} trades -> {path}")
        print(f"[{arm}] exit reasons: {results[arm]['exit_reasons']}")

    with open(OUT / f"summary{tag}.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nsummary -> {OUT / f'summary{tag}.json'}")


if __name__ == "__main__":
    main()
