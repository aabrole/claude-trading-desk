#!/usr/bin/env python3
"""
sweep.py
========
Parameter search with an out-of-sample split baked in, because a sweep without
one is just a machine for finding coincidences.

Every configuration is scored twice: on the TRAIN period and on the TEST period
it never influenced. A config only counts as evidence if it works in both. The
`consistent` column is the only one worth reading.

  python3 sweep.py --symbols SPY QQQ IWM AAPL MSFT NVDA TSLA AMD \
                   --start 2023-01-01 --split 2025-03-01 --end 2026-09-01

Jev is deliberately not in this sweep. Searching a grid with a paid model would
cost thousands of calls to answer a question free arms can answer: does any
configuration of this strategy have an edge before costs at all?
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics as M                                    # noqa: E402
from data import fetch_universe                        # noqa: E402
from decision import GateDecider, RuleDecider          # noqa: E402
from engine import Engine, EngineConfig                # noqa: E402
from strategy import ORBConfig, ORBStrategy            # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def load_env(path: Path = ROOT / ".env") -> None:
    import os
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def trend_only_gate(strat):
    """The one gate the data actually supported: skip counter-trend breakouts."""
    def gate(s):
        return "counter_trend" if s.f("trend_align") < 0 else None
    return [gate]


def score(plans, strat, decider, ecfg, equity):
    eng = Engine(ecfg)
    eng.set_feature_cols(strat.feature_cols)
    res = eng.run(plans, strat, decider, verbose=False)
    m = M.summarize(res.trades, res.equity_curve, equity,
                multiplier=ecfg.instrument.multiplier)
    return m


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+",
                    default=["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMD"])
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--split", default="2025-03-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--warmup-days", type=int, default=45)
    ap.add_argument("--equity", type=float, default=25_000.0)
    ap.add_argument("--risk-pct", type=float, default=0.005)
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--or-minutes", nargs="+", type=int, default=[5, 15])
    ap.add_argument("--rr", nargs="+", type=float, default=[1.0, 1.5, 2.0, 3.0])
    ap.add_argument("--variants", nargs="+", default=["base", "retest", "fakeout"])
    args = ap.parse_args()

    warm = (pd.Timestamp(args.start) - pd.Timedelta(days=args.warmup_days)).date().isoformat()
    bars = fetch_universe(symbols=args.symbols, start=warm, end=args.end, minutes=5)

    tz = "America/New_York"
    t0 = pd.Timestamp(args.start, tz=tz)
    tsplit = pd.Timestamp(args.split, tz=tz)

    ecfg = EngineConfig(starting_equity=args.equity, risk_pct=args.risk_pct,
                        slippage_bps=args.slippage_bps, max_positions=3)

    rows = []
    combos = list(itertools.product(args.or_minutes, args.rr, args.variants))
    print(f"[sweep] {len(combos)} strategy configs x 2 decision arms\n")

    for i, (orm, rr, variant) in enumerate(combos, 1):
        cfg = ORBConfig(or_minutes=orm, rr=rr,
                        require_retest=(variant == "retest"),
                        fakeout_reentry=(variant == "fakeout"))
        strat = ORBStrategy(cfg)

        train, test = {}, {}
        n_sig = 0
        for sym, df in bars.items():
            plan = strat.prepare(df)
            plan = plan[plan.index >= t0]
            n_sig += int((plan["signal"] != "").sum())
            tr = plan[plan.index < tsplit]
            te = plan[plan.index >= tsplit]
            if not tr.empty:
                train[sym] = tr
            if not te.empty:
                test[sym] = te

        for arm in ("rules", "trend_only"):
            def mk():
                return RuleDecider() if arm == "rules" else GateDecider(trend_only_gate(strat))
            mtr = score(train, strat, mk(), ecfg, args.equity) if train else {}
            mte = score(test, strat, mk(), ecfg, args.equity) if test else {}
            if not mtr.get("trades") or not mte.get("trades"):
                continue
            rows.append({
                "or_min": orm, "rr": rr, "variant": variant, "arm": arm,
                "signals": n_sig,
                "tr_n": mtr["trades"], "tr_wr": mtr["win_rate"],
                "tr_pre": mtr["pnl_before_costs_$"], "tr_net": mtr["net_pnl_$"],
                "tr_R": mtr["sum_R"],
                "te_n": mte["trades"], "te_wr": mte["win_rate"],
                "te_pre": mte["pnl_before_costs_$"], "te_net": mte["net_pnl_$"],
                "te_R": mte["sum_R"],
            })
        print(f"[sweep] {i}/{len(combos)}  or={orm} rr={rr} {variant}: {n_sig} signals")

    df = pd.DataFrame(rows)
    if df.empty:
        print("no configs produced trades in both periods")
        return

    # The only column that matters: positive before costs in BOTH periods.
    df["consistent"] = (df.tr_pre > 0) & (df.te_pre > 0)
    df["net_both"] = (df.tr_net > 0) & (df.te_net > 0)
    df = df.sort_values(["consistent", "te_pre"], ascending=[False, False])

    path = OUT / "sweep.csv"
    df.to_csv(path, index=False)
    print(f"\n{'=' * 110}")
    print(df.to_string(index=False))
    print(f"{'=' * 110}")
    print(f"\nconfigs profitable BEFORE costs in both periods: {int(df.consistent.sum())} / {len(df)}")
    print(f"configs profitable AFTER costs in both periods:  {int(df.net_both.sum())} / {len(df)}")
    print(f"\n-> {path}")


if __name__ == "__main__":
    main()
