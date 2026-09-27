"""
metrics.py
==========
Turning a trade list into the numbers that decide whether a strategy lives.

Deliberately reports gross AND net side by side. The gap between them is the
fee trap, and on a strategy that trades every morning it is usually the whole
story.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from contracts import Trade

TRADING_DAYS = 252


def summarize(trades: List[Trade], equity_curve: pd.Series,
              starting_equity: float, multiplier: float = 1.0) -> Dict:
    if not trades:
        return {"trades": 0, "note": "no trades taken"}

    net = np.array([t.net_pnl for t in trades])
    gross = np.array([t.gross_pnl for t in trades])
    ideal = np.array([t.ideal_pnl for t in trades])
    fees = np.array([t.fees for t in trades])
    slip = np.array([t.slippage_cost for t in trades])
    rs = np.array([t.r_multiple for t in trades])
    holds = np.array([t.hold_minutes for t in trades])

    wins, losses = net[net > 0], net[net < 0]
    gross_profit, gross_loss = wins.sum(), abs(losses.sum())
    ideal_profit = ideal[ideal > 0].sum()

    final = float(equity_curve.iloc[-1]) if len(equity_curve) else starting_equity + net.sum()
    total_return = (final / starting_equity - 1) * 100

    # Drawdown on the daily equity curve.
    peak = equity_curve.cummax()
    dd = (equity_curve - peak) / peak
    max_dd = float(dd.min() * 100) if len(dd) else 0.0

    daily = equity_curve.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)) if daily.std() > 0 else 0.0
    downside = daily[daily < 0].std()
    sortino = float(daily.mean() / downside * np.sqrt(TRADING_DAYS)) if downside and downside > 0 else 0.0

    n_days = max(1, len(equity_curve))
    years = n_days / TRADING_DAYS
    cagr = ((final / starting_equity) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else 0.0

    reasons = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()

    # multiplier matters for futures: 8.9 points on one MES contract is $44.50,
    # not $8.90. Without it this metric silently understates risk 5x.
    risk_dollars = np.array([abs(t.entry_price - t.stop) * t.shares * multiplier
                             for t in trades])
    target_hits = sum(1 for t in trades if t.exit_reason.startswith("TARGET"))
    stop_hits = sum(1 for t in trades if t.exit_reason.startswith("STOP"))
    friction = float(fees.sum() + slip.sum())

    return {
        "trades": len(trades),
        "win_rate": round(float((net > 0).mean() * 100), 1),
        "target_hit_rate": round(target_hits / len(trades) * 100, 1),
        "stop_out_rate": round(stop_hits / len(trades) * 100, 1),
        "sum_R": round(float(rs.sum()), 2),
        "avg_R": round(float(rs.mean()), 3),
        "expectancy_$": round(float(net.mean()), 2),
        "profit_factor": round(float(gross_profit / gross_loss), 2) if gross_loss > 0 else float("inf"),
        "avg_win_$": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss_$": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "pnl_before_costs_$": round(float(ideal.sum()), 2),
        "fees_$": round(float(fees.sum()), 2),
        "slippage_$": round(float(slip.sum()), 2),
        "net_pnl_$": round(float(net.sum()), 2),
        "friction_$": round(friction, 2),
        "friction_per_trade_$": round(friction / len(trades), 3),
        # Friction as a share of what the strategy actually WON. Comparing it to
        # net or to a negative gross produces a meaningless percentage.
        # Friction measured against the winning trades' pre-cost P&L: the honest
        # denominator. Measuring it against a net or negative number is nonsense.
        "friction_pct_of_ideal_profit": (round(friction / ideal_profit * 100, 1)
                                         if ideal_profit > 0 else None),
        "avg_risk_pct_of_equity": round(float(risk_dollars.mean()) / starting_equity * 100, 3),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "avg_hold_min": round(float(holds.mean()), 1),
        "trades_per_day": round(len(trades) / n_days, 2),
        "best_$": round(float(net.max()), 2),
        "worst_$": round(float(net.min()), 2),
        "exit_reasons": reasons,
    }


HEADLINE = ["trades", "win_rate", "target_hit_rate", "stop_out_rate",
            "sum_R", "avg_R", "expectancy_$", "profit_factor",
            "total_return_pct", "max_drawdown_pct", "sharpe",
            "pnl_before_costs_$", "slippage_$", "fees_$", "net_pnl_$",
            "friction_$", "friction_pct_of_ideal_profit",
            "avg_risk_pct_of_equity", "avg_hold_min"]


def compare(results: Dict[str, Dict]) -> pd.DataFrame:
    """Arms side by side, one column each, so the A/B reads at a glance."""
    rows = {}
    for name, m in results.items():
        rows[name] = {k: m.get(k, "-") for k in HEADLINE}
    return pd.DataFrame(rows)


def by_bucket(trades_df: pd.DataFrame, col: str, bins=None, labels=None) -> pd.DataFrame:
    """
    Win rate and expectancy sliced by any feature. This is the diagnosis tool:
    it tells you WHERE the strategy bleeds, not just that it bled.
    """
    if trades_df.empty or col not in trades_df.columns:
        return pd.DataFrame()
    df = trades_df.copy()
    key = df[col] if bins is None else pd.cut(df[col], bins=bins, labels=labels)
    g = df.groupby(key, observed=True).agg(
        trades=("net_pnl", "count"),
        win_rate=("net_pnl", lambda x: round((x > 0).mean() * 100, 1)),
        avg_R=("R", "mean"),
        net_pnl=("net_pnl", "sum"),
    ).round(2)
    return g
