"""
data.py
=======
Intraday (or daily) bars from Alpaca's historical API, cached to disk.

Why Alpaca and not yfinance here: the SIP feed gives us consolidated-tape
bars for hundreds of symbols going back years, which is what we need to get
a sample size big enough to trust a win rate. yfinance caps intraday at 60
days, which is ~12 trading weeks — not enough to survive one bad regime.

Everything is returned in America/New_York time and filtered to the regular
session by default, because pre/post-market bars have garbage spreads and
would flatter an intraday mean-reversion backtest.
"""

from __future__ import annotations

import datetime as dt
import os
import pickle
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

ET = "America/New_York"
SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)

# Liquid, tight-spread names. Mean reversion needs a spread you can pay
# thousands of times without dying, so this list is deliberately boring.
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
    "TSLA", "AMD", "JPM", "XOM", "WMT", "UNH",
]


def _client() -> StockHistoricalDataClient:
    key = os.environ.get("ALPACA_PAPER_KEY", "")
    secret = os.environ.get("ALPACA_PAPER_SECRET", "")
    if not key or not secret:
        raise RuntimeError(
            "Set ALPACA_PAPER_KEY and ALPACA_PAPER_SECRET. "
            "Copy .env.example to .env and source it."
        )
    return StockHistoricalDataClient(key, secret)


def _timeframe(minutes: int) -> TimeFrame:
    """minutes=0 means daily bars."""
    if minutes == 0:
        return TimeFrame.Day
    return TimeFrame(minutes, TimeFrameUnit.Minute)


def _cache_path(symbol: str, minutes: int, start: str, end: str, feed: str) -> Path:
    tf = "1D" if minutes == 0 else f"{minutes}T"
    return CACHE_DIR / f"{symbol}_{tf}_{start}_{end}_{feed}.pkl"


def _to_session(df: pd.DataFrame, minutes: int, rth_only: bool) -> pd.DataFrame:
    """UTC -> ET, optionally drop everything outside the regular session."""
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
    if minutes > 0 and rth_only:
        t = df.index.time
        df = df[(t >= SESSION_OPEN) & (t < SESSION_CLOSE)]
    return df.sort_index()


def fetch_bars(
    symbol: str,
    start: str,
    end: str,
    minutes: int = 5,
    feed: str = "sip",
    rth_only: bool = True,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    One symbol, returned as a DataFrame indexed by ET timestamp with columns:
      open, high, low, close, volume, trade_count, vwap
    """
    path = _cache_path(symbol, minutes, start, end, feed)
    if use_cache and path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)

    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=_timeframe(minutes),
        start=pd.Timestamp(start, tz=ET).tz_convert("UTC").to_pydatetime(),
        end=pd.Timestamp(end, tz=ET).tz_convert("UTC").to_pydatetime(),
        feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
        adjustment="split",
    )
    raw = _client().get_stock_bars(req).df
    if raw.empty:
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "trade_count", "vwap"])
    else:
        df = raw.xs(symbol, level="symbol") if isinstance(raw.index, pd.MultiIndex) else raw
        df = _to_session(df, minutes, rth_only)

    with open(path, "wb") as f:
        pickle.dump(df, f)
    return df


def fetch_universe(
    symbols: Optional[List[str]] = None,
    start: str = "2023-01-01",
    end: Optional[str] = None,
    minutes: int = 5,
    feed: str = "sip",
    rth_only: bool = True,
    use_cache: bool = True,
    verbose: bool = True,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch every symbol, one request each so the cache is per-symbol and a
    failure on one name doesn't cost you the whole download.
    """
    symbols = symbols or DEFAULT_UNIVERSE
    end = end or dt.date.today().isoformat()
    out: Dict[str, pd.DataFrame] = {}

    for i, sym in enumerate(symbols, 1):
        cached = _cache_path(sym, minutes, start, end, feed).exists()
        try:
            df = fetch_bars(sym, start, end, minutes, feed, rth_only, use_cache)
        except Exception as e:
            if verbose:
                print(f"[data] {sym}: FAILED ({type(e).__name__}: {str(e)[:80]}) — skipping")
            continue
        if df.empty:
            if verbose:
                print(f"[data] {sym}: no bars returned — skipping")
            continue
        out[sym] = df
        if verbose:
            tag = "cache" if cached else "fetch"
            print(f"[data] {i}/{len(symbols)} {sym}: {len(df):,} bars "
                  f"({df.index.min().date()} -> {df.index.max().date()}) [{tag}]")

    if not out:
        raise RuntimeError("No data for any symbol. Check credentials, feed, and date range.")
    return out


if __name__ == "__main__":
    bars = fetch_universe(["SPY", "AAPL"], start="2024-01-01", end="2024-03-01", minutes=5)
    spy = bars["SPY"]
    print(spy.head(3))
    print(f"\nbars/day check: {len(spy) / spy.index.normalize().nunique():.1f} (expect ~78 for 5-min RTH)")
