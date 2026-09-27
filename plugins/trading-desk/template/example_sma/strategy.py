"""
strategy.py  --  the example strategy: a moving-average crossover on SPY.

This exists so the pipeline runs end to end the moment you clone the kit. It is
deliberately one of the most-published rules in trading, which means it is
almost certainly not an edge, and that is the point: you should watch the
harness report an honest "no edge" before you trust it to report a yes.

Replace this file with your own logic. The contract the rest of the harness
needs from a strategy object is small:

  feature_cols                 the columns Jev and the gates may read
  prepare(df)   -> DataFrame   add indicators and a `signal` column
  snapshot(...)  -> Snapshot   describe one candidate in words and numbers
  gates()       -> [Gate]      hand-written filters: the control arm
  jev_prompt()  -> JevPrompt   what to ask the model

`prepare` must produce these columns, because the engine reads them directly:

  signal              'long' | 'short' | ''
  stop, target        absolute prices for the proposed trade
  minutes_from_open   used for the end-of-day flatten

The one rule that matters more than any other in here: every column must be
computable from data available AT that bar. A single indicator that peeks one bar
ahead will produce a beautiful equity curve and a worthless strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from contracts import Action, Snapshot
from decision import Gate, JevPrompt

FEATURE_COLS = [
    "fast_slope_atr",     # slope of the fast average over 5 bars, in ATRs
    "atr_pct",            # volatility as a share of price
    "dist_slow_atr",      # distance from the slow average, in ATRs
    "trend_align",        # +1 when the crossover agrees with the longer trend
    "bars_since_cross",   # how stale the signal is
]


@dataclass
class SMAConfig:
    fast: int = 20
    slow: int = 50
    trend: int = 200
    atr_len: int = 14
    stop_atr: float = 1.5
    target_atr: float = 3.0     # 1:2 on the risk
    allow_shorts: bool = True


class SMACrossover:
    """Long when the fast average crosses above the slow one, short on the reverse."""

    def __init__(self, cfg: Optional[SMAConfig] = None):
        self.cfg = cfg or SMAConfig()
        self.feature_cols = FEATURE_COLS

    # ------------------------------------------------------------------ prepare

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        c = self.cfg
        out = df.copy()

        out["sma_fast"] = out["close"].rolling(c.fast).mean()
        out["sma_slow"] = out["close"].rolling(c.slow).mean()
        out["sma_trend"] = out["close"].rolling(c.trend).mean()

        # True range, then Wilder-ish ATR via a simple rolling mean. shift(1) on
        # the previous close is what keeps this causal.
        prev_close = out["close"].shift(1)
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        out["atr"] = tr.rolling(c.atr_len).mean()
        out["atr_pct"] = out["atr"] / out["close"] * 100.0

        above = out["sma_fast"] > out["sma_slow"]
        crossed_up = above & ~above.shift(1, fill_value=False)
        crossed_dn = (~above) & above.shift(1, fill_value=False)

        # NOT the gap between the averages: at a crossover they are equal by
        # definition, so any "minimum separation" filter vetoes every signal.
        # The slope of the fast average is the thing that says whether this is a
        # real turn or chop.
        out["fast_slope_atr"] = (out["sma_fast"] - out["sma_fast"].shift(5)) / out["atr"]
        out["dist_slow_atr"] = (out["close"] - out["sma_slow"]) / out["atr"]
        out["trend_align"] = np.where(
            crossed_up, np.where(out["close"] > out["sma_trend"], 1.0, -1.0),
            np.where(crossed_dn, np.where(out["close"] < out["sma_trend"], 1.0, -1.0), 0.0))

        grp = (crossed_up | crossed_dn).cumsum()
        out["bars_since_cross"] = out.groupby(grp).cumcount().astype(float)

        signal = pd.Series("", index=out.index, dtype=object)
        signal[crossed_up] = "long"
        if c.allow_shorts:
            signal[crossed_dn] = "short"
        out["signal"] = signal

        # An entry needs a stop and a target before it is a trade.
        long_leg = out["signal"] == "long"
        short_leg = out["signal"] == "short"
        out["stop"] = np.nan
        out["target"] = np.nan
        out.loc[long_leg, "stop"] = out.loc[long_leg, "close"] - c.stop_atr * out.loc[long_leg, "atr"]
        out.loc[long_leg, "target"] = out.loc[long_leg, "close"] + c.target_atr * out.loc[long_leg, "atr"]
        out.loc[short_leg, "stop"] = out.loc[short_leg, "close"] + c.stop_atr * out.loc[short_leg, "atr"]
        out.loc[short_leg, "target"] = out.loc[short_leg, "close"] - c.target_atr * out.loc[short_leg, "atr"]

        # Daily bars: one "minute from open" per bar is enough for the engine's
        # end-of-day logic, which never fires on a daily series.
        out["minutes_from_open"] = 0.0

        # A candidate with an incomplete context is dropped rather than sent on
        # with zeros. A fabricated feature is worse than a missing trade: it
        # becomes a fact the model reasons from.
        incomplete = out[FEATURE_COLS + ["stop", "target"]].isna().any(axis=1)
        out.loc[incomplete & (out["signal"] != ""), "signal"] = ""
        return out

    # ----------------------------------------------------------------- snapshot

    def snapshot(self, symbol: str, ts: pd.Timestamp, row: pd.Series) -> Snapshot:
        feats = {c: (float(row[c]) if pd.notna(row.get(c)) else 0.0) for c in self.feature_cols}
        side = Action.ENTER_LONG if row["signal"] == "long" else Action.ENTER_SHORT
        direction = "above" if row["signal"] == "long" else "below"
        trend_word = ("with the 200-day trend" if feats["trend_align"] > 0
                      else "against the 200-day trend")
        lines = [
            f"{symbol}: the {self.cfg.fast}-day average just crossed {direction} "
            f"the {self.cfg.slow}-day, {trend_word}.",
            f"The fast average has moved {feats['fast_slope_atr']:+.2f} ATR over five sessions.",
            f"Price sits {feats['dist_slow_atr']:+.2f} ATR from the slow average.",
            f"Volatility is {feats['atr_pct']:.2f}% of price.",
        ]
        return Snapshot(
            symbol=symbol, timestamp=ts, price=float(row["close"]), proposed=side,
            features=feats, context_lines=lines,
            stop=float(row["stop"]), target=float(row["target"]),
        )

    # -------------------------------------------------------------------- gates

    def gates(self, require_trend: bool = True, min_slope_atr: float = 0.05,
              max_dist_atr: float = 2.5, max_atr_pct: float = 6.0) -> List[Gate]:
        """
        The control arm: the same judgement calls written as if-statements.

        This arm is not optional. Without it, "the model improved the strategy"
        cannot be distinguished from "any filter at all improved the strategy",
        and the second explanation is usually the right one. If the gates match
        the model, the honest conclusion is that you did not need the model here.
        """
        def trend_gate(s: Snapshot) -> Optional[str]:
            if require_trend and s.f("trend_align") < 0:
                return "counter_trend"
            return None

        def slope_gate(s: Snapshot) -> Optional[str]:
            # The fast average must be moving the way we are about to trade. A
            # crossover on a flat average is chop and will cross back.
            slope = s.f("fast_slope_atr")
            wanted = 1.0 if s.proposed == Action.ENTER_LONG else -1.0
            if slope * wanted < min_slope_atr:
                return "average_not_turning"
            return None

        def extension_gate(s: Snapshot) -> Optional[str]:
            # Entering far from the slow average puts the stop a long way behind.
            if abs(s.f("dist_slow_atr")) > max_dist_atr:
                return "too_extended"
            return None

        def vol_gate(s: Snapshot) -> Optional[str]:
            if s.f("atr_pct") > max_atr_pct:
                return "volatility_too_high"
            return None

        return [trend_gate, slope_gate, extension_gate, vol_gate]

    # --------------------------------------------------------------- jev prompt

    def jev_prompt(self) -> JevPrompt:
        return JevPrompt(
            entry_instructions=(
                "A moving-average crossover has just triggered on a daily chart. "
                "The strategy has already decided the direction; you are only "
                "deciding whether this particular crossover is worth taking. "
                "Most crossovers in a sideways market reverse within days. Take "
                "the trade when the crossover is confirmed by the longer trend "
                "and the averages are separating, stand aside when it looks like "
                "chop or price has already run too far from the slow average."
            ),
            entry_criteria={
                "take_it": "this crossover is worth trading",
                "stand_aside": "this looks like chop or a late entry",
            },
            # Recorded, never acted on. Costs nothing extra and lets you check
            # afterwards whether a stated risk actually predicted the outcome.
            extra_questions={
                "whipsaw_risk": {
                    "type": "score",
                    "instructions": "How likely is it that these averages cross back "
                                    "within the next ten sessions?",
                    "criteria": ["very unlikely", "unlikely", "even", "likely", "very likely"],
                },
            },
        )
