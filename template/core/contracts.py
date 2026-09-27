"""
types.py
========
The contract between a strategy, a decision layer, and the engine.

A strategy's only job is to propose candidates and describe the situation.
It never decides whether to trade — that belongs to a Decider — and it never
executes — that belongs to the engine. Keeping those three apart is what lets
us run the same strategy against rules, hand-written gates, and Jev, and know
the only thing that changed was the decision layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import pandas as pd


class Action(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    HOLD = "hold"
    WAIT = "wait"


ENTRIES = (Action.ENTER_LONG, Action.ENTER_SHORT)


@dataclass
class Snapshot:
    """
    Everything known at one bar, for one symbol, with nothing from the future.

    `features` are the numbers gates read. `context_lines` are the same facts in
    plain English, which is what Jev reads. Both are built from the same row, so
    neither decision layer gets an information advantage over the other.
    """
    symbol: str
    timestamp: pd.Timestamp
    price: float
    proposed: Action
    features: Dict[str, float] = field(default_factory=dict)
    context_lines: List[str] = field(default_factory=list)
    stop: Optional[float] = None
    target: Optional[float] = None
    position: int = 0
    entry_price: Optional[float] = None
    unrealized_bps: Optional[float] = None
    bars_held: int = 0

    def f(self, name: str, default: float = 0.0) -> float:
        v = self.features.get(name)
        return default if v is None or pd.isna(v) else float(v)

    @property
    def risk_per_share(self) -> float:
        if self.stop is None:
            return 0.0
        return abs(self.price - self.stop)


@dataclass
class Decision:
    action: Action
    probabilities: Dict[str, float] = field(default_factory=dict)
    confidence: float = 1.0
    source: str = "rules"
    latency_ms: float = 0.0
    cached: bool = False
    note: str = ""
    aux: Dict[str, float] = field(default_factory=dict)   # extra Jev answers, e.g. fakeout_risk


@dataclass
class Trade:
    symbol: str
    side: int                     # +1 long, -1 short
    entry_time: pd.Timestamp
    entry_price: float            # fill price, after slippage
    shares: int
    stop: float
    target: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    raw_entry: float = 0.0        # fill price BEFORE slippage
    raw_exit: float = 0.0
    ideal_pnl: float = 0.0        # P&L at unslipped prices, before any cost
    gross_pnl: float = 0.0        # P&L at actual fills (slippage already inside)
    fees: float = 0.0
    slippage_cost: float = 0.0
    net_pnl: float = 0.0
    r_multiple: float = 0.0
    bars_held: int = 0
    decision_note: str = ""
    entry_prob: float = 1.0
    features: Dict[str, float] = field(default_factory=dict)

    @property
    def hold_minutes(self) -> float:
        if self.exit_time is None:
            return 0.0
        return (self.exit_time - self.entry_time).total_seconds() / 60
