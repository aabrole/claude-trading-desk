"""
features.py
===========
Indicator and session helpers shared by every strategy.

Two rules hold everywhere in this file:
  - Trailing only. Every value at bar t uses bars <= t. No centred windows,
    no shift(-1), nothing that peeks at the bar it is describing.
  - Session-aware. Intraday stats that roll across the overnight gap are
    measuring the gap, not the day.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window, min_periods=max(2, window // 2)).mean()


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def session_date(df: pd.DataFrame) -> pd.Series:
    """Calendar date of each bar, as the session key."""
    return pd.Series(df.index.normalize(), index=df.index)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    day = session_date(df)
    typical = df["vwap"] if "vwap" in df.columns else (df["high"] + df["low"] + df["close"]) / 3
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vv = df["volume"].groupby(day).cumsum()
    return pv / vv.replace(0, np.nan)


def minutes_from_open(df: pd.DataFrame, open_hour: int = 9, open_minute: int = 30) -> pd.Series:
    delta = df.index - df.index.normalize() - pd.Timedelta(hours=open_hour, minutes=open_minute)
    return pd.Series((delta.total_seconds() / 60).astype(int), index=df.index)


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Aggregate to a slower timeframe. `label='left', closed='left'` means a bar
    stamped 09:30 covers 09:30-09:45, so using it at 09:45 is not lookahead.
    """
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "vwap" in df.columns and "volume" in df.columns:
        out = df.resample(rule, label="left", closed="left").agg(agg)
        num = (df["vwap"] * df["volume"]).resample(rule, label="left", closed="left").sum()
        den = df["volume"].resample(rule, label="left", closed="left").sum()
        out["vwap"] = num / den.replace(0, np.nan)
    else:
        out = df.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


def align_higher_timeframe(fast: pd.DataFrame, slow: pd.Series, name: str) -> pd.Series:
    """
    Carry a higher-timeframe series onto a faster index without leaking.

    A 15-min bar stamped 09:30 is only COMPLETE at 09:45, so its value must not
    be visible to a 1-min bar before 09:45. We shift the slow series forward one
    of its own bars before reindexing, which is the whole trick.
    """
    shifted = slow.shift(1)
    shifted.name = name
    return shifted.reindex(fast.index, method="ffill")


def wick_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Upper/lower wick as a share of the bar's range — candlestick rejection, numerically."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    upper = df["high"] - df[["open", "close"]].max(axis=1)
    lower = df[["open", "close"]].min(axis=1) - df["low"]
    body = (df["close"] - df["open"]).abs()
    return pd.DataFrame({
        "upper_wick_pct": upper / rng,
        "lower_wick_pct": lower / rng,
        "body_pct": body / rng,
    }, index=df.index)


def rolling_time_of_day_mean(df: pd.DataFrame, col: str, days: int = 20) -> pd.Series:
    """
    Average of `col` for this same time of day over the previous `days` sessions.

    Volume at 09:30 is nothing like volume at 14:00, so comparing a bar to a flat
    session average makes every open look like a volume spike. This compares
    like with like, and shift(1) keeps today out of its own baseline.
    """
    tod = df.index.strftime("%H:%M")
    s = df[col]
    return s.groupby(tod).transform(
        lambda x: x.shift(1).rolling(days, min_periods=3).mean()
    )
