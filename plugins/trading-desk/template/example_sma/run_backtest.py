#!/usr/bin/env python3
"""
run_backtest.py  --  backtest the example strategy across all three arms.

Three arms, always, and in this order:

  rules   take every signal the strategy produces. The strategy as advertised.
  gated   hand-written if-statements filter the same signals. The CONTROL.
  jev     the model filters the same signals.

The gated arm is the one people skip, and skipping it is what makes "AI improved
my strategy" unfalsifiable. All three arms must see an identical candidate set,
or the comparison means nothing.

Data comes from Yahoo via yfinance so this runs with no API key at all. Your own
strategy will probably want core/data.py, which reads Alpaca.

  python3 strategies/example_sma/run_backtest.py
  python3 strategies/example_sma/run_backtest.py --arms rules gated
  python3 strategies/example_sma/run_backtest.py --split 2023-01-01
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics as M                                              # noqa: E402
from decision import GateDecider, JevDecider, RuleDecider         # noqa: E402
from engine import Engine, EngineConfig                           # noqa: E402

from strategy import SMAConfig, SMACrossover                      # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"])
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--split", default=None,
                   help="train/test boundary, e.g. 2022-01-01. Report BOTH halves.")
    p.add_argument("--arms", nargs="+", default=["rules", "gated"],
                   choices=["rules", "gated", "jev"],
                   help="jev needs TYPESAFE_API_KEY and spends money")
    p.add_argument("--equity", type=float, default=10_000)
    p.add_argument("--risk-pct", type=float, default=0.005)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--fill", default="next_open", choices=["close", "next_open", "level"])
    p.add_argument("--jev-threshold", type=float, default=0.55)
    p.add_argument("--jev-offline", action="store_true")
    return p.parse_args()


def fetch(symbols, start, end) -> dict:
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("this example uses yfinance for keyless data: pip install yfinance")
    out = {}
    for sym in symbols:
        df = yf.download(sym, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            print("  %s: no data" % sym)
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]]
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        out[sym] = df
        print("  %s: %d daily bars, %s to %s"
              % (sym, len(df), df.index[0].date(), df.index[-1].date()))
    return out


def t_stat(trades) -> float:
    """
    t on mean R. summarize() does not report one, and it is the number that
    decides whether a positive average is worth anything: under about 2, a
    positive R per trade is indistinguishable from luck at this sample size.
    """
    import numpy as np
    rs = np.array([t.r_multiple for t in trades], dtype=float)
    if len(rs) < 2 or rs.std(ddof=1) == 0:
        return 0.0
    return float(rs.mean() / (rs.std(ddof=1) / np.sqrt(len(rs))))


def report(tag: str, res, equity: float) -> dict:
    m = M.summarize(res.trades, res.equity_curve, equity)
    if not res.trades:
        print("  %-7s no trades (every candidate was vetoed)" % tag)
        return m
    m["t_stat"] = round(t_stat(res.trades), 2)
    print("  %-7s %4d trades  %5.1f%% win  %+.3f R/trade  t=%+.2f  "
          "%+.1f%% total  %.1f%% maxDD  PF %.2f"
          % (tag, m["trades"], m.get("win_rate", 0), m.get("avg_R", 0),
             m["t_stat"], m.get("total_return_pct", 0),
             m.get("max_drawdown_pct", 0), m.get("profit_factor", 0)))
    return m


def main() -> None:
    args = parse_args()
    print("=== data ===")
    raw = fetch(args.symbols, args.start, args.end)
    if not raw:
        sys.exit("no data fetched")

    strat = SMACrossover(SMAConfig())
    plans, n = {}, 0
    print("\n=== signals ===")
    for sym, df in raw.items():
        plan = strat.prepare(df)
        k = int((plan["signal"] != "").sum())
        n += k
        plans[sym] = plan
        print("  %s: %d crossovers" % (sym, k))
    if n == 0:
        sys.exit("no candidates; nothing to test")

    ecfg = EngineConfig(
        starting_equity=args.equity, risk_pct=args.risk_pct,
        slippage_bps=args.slippage_bps, fill=args.fill,
        max_positions=len(args.symbols), max_trades_per_day=1,
        # Daily bars: never flatten intraday, and let a position run.
        flat_at_minute=10_000, max_bars_held=None,
    )

    print("\n=== arms ===")
    summary = {}
    for arm in args.arms:
        if arm == "rules":
            decider = RuleDecider()
        elif arm == "gated":
            decider = GateDecider(strat.gates())
        else:
            decider = JevDecider(
                prompt=strat.jev_prompt(), threshold=args.jev_threshold,
                offline=args.jev_offline,
                log_path=OUT / "decisions_jev.jsonl",
            )
        engine = Engine(ecfg)
        engine.set_feature_cols(strat.feature_cols)
        res = engine.run(plans, strat, decider, verbose=False)
        m = report(arm, res, args.equity)
        summary[arm] = m

        res.equity_curve.rename("equity").to_frame().to_csv(OUT / ("equity_%s.csv" % arm))
        if res.trades:
            res.trades_df().to_csv(OUT / ("trades_%s.csv" % arm), index=False)

        # Out of sample is not optional. A strategy that only works on the half
        # you developed it on is a description of that half.
        if args.split and res.trades:
            tdf = res.trades_df()
            tdf["entry_time"] = pd.to_datetime(tdf["entry_time"], utc=True)
            cut = pd.Timestamp(args.split, tz="UTC")
            for half, sel in (("train", tdf["entry_time"] < cut),
                              ("test", tdf["entry_time"] >= cut)):
                part = tdf[sel]
                if len(part) > 1:
                    print("    %-5s %4d trades  %+.3f R/trade"
                          % (half, len(part), part["R"].mean()))

    # The hero curve the dashboard animates. Whichever arm you would actually
    # run is the one that belongs here.
    hero = "gated" if "gated" in summary else args.arms[0]
    src = OUT / ("equity_%s.csv" % hero)
    if src.exists():
        (OUT / "equity.csv").write_text(src.read_text())
        print("\nequity.csv <- equity_%s.csv  (what the dashboard animates)" % hero)

    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print("wrote %s" % OUT)

    print("\nRead this before believing any of it:")
    print("  - is the gated arm as good as jev? then you did not need jev.")
    print("  - is the test half as good as the train half? if not, it is fitted.")
    print("  - a t-stat under 2 on R/trade is not evidence of an edge.")


if __name__ == "__main__":
    main()
