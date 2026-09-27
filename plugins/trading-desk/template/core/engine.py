"""
engine.py
=========
Portfolio backtest executor. Strategies propose, Deciders approve, this executes.

The things that separate this from the backtest in most tutorials:

1. No same-bar lookahead. A signal detected on the bar closing at 09:45 is
   filled at 09:45's close at the earliest, and stops are only checked from the
   NEXT bar. A backtest that fills at a price it needed the completed bar to
   know is reporting fiction.

2. Friction is modelled, per side: slippage in basis points, plus the SEC
   Section 31 fee and FINRA TAF that land on every sale. Commissions are zero
   at Alpaca, which is exactly why people forget the rest of it exists.

3. Ambiguous bars are counted, not hidden. When one bar's range covers both the
   stop and the target, nobody can know which came first without tick data. We
   resolve pessimistically (stop first) and report how often it mattered. If
   that count is large, the result is noise no matter how good it looks.

4. Risk-based sizing. A 1:2 strategy is meaningless unless every trade risks the
   same fraction of equity, so size comes from stop distance, not a flat "1 unit".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from contracts import Action, Decision, ENTRIES, Snapshot, Trade
from decision import Decider

# Regulatory costs that apply to SALES only (2025 rates). Equities only;
# futures pay exchange and clearing fees inside the per-contract commission.
SEC_FEE_RATE = 27.80 / 1_000_000      # per dollar of principal sold
TAF_PER_SHARE = 0.000166              # FINRA trading activity fee
TAF_CAP = 8.30


@dataclass
class Instrument:
    """
    What is being traded, so the engine can cost it correctly.

    Equities slip in basis points of price and pay regulatory fees on sales.
    Futures slip in ticks, carry a contract multiplier, pay a flat commission
    per contract per side, and are sized against margin rather than notional
    (one MES contract controls ~$30,000 of index at ~$1,500 of margin, so a
    notional cap would refuse every trade).
    """
    kind: str = "equity"               # equity | future
    multiplier: float = 1.0            # dollars per point. MES 5, ES 50
    tick_size: float = 0.01
    slippage_ticks: float = 0.0        # futures: used when > 0
    commission_per_unit: float = 0.0   # per share or per contract, per side
    margin_per_unit: float = 0.0       # futures: intraday margin per contract
    reg_fees: bool = True              # SEC 31 + FINRA TAF (equities only)


MES = Instrument(kind="future", multiplier=5.0, tick_size=0.25, slippage_ticks=1.0,
                 commission_per_unit=0.62, margin_per_unit=1500.0, reg_fees=False)
MNQ = Instrument(kind="future", multiplier=2.0, tick_size=0.25, slippage_ticks=1.0,
                 commission_per_unit=0.62, margin_per_unit=2000.0, reg_fees=False)
M2K = Instrument(kind="future", multiplier=5.0, tick_size=0.10, slippage_ticks=1.0,
                 commission_per_unit=0.62, margin_per_unit=800.0, reg_fees=False)
MYM = Instrument(kind="future", multiplier=0.5, tick_size=1.0, slippage_ticks=1.0,
                 commission_per_unit=0.62, margin_per_unit=800.0, reg_fees=False)
ES = Instrument(kind="future", multiplier=50.0, tick_size=0.25, slippage_ticks=1.0,
                commission_per_unit=2.50, margin_per_unit=15000.0, reg_fees=False)


@dataclass
class EngineConfig:
    starting_equity: float = 10_000.0
    risk_pct: float = 0.005            # fraction of equity risked per trade (0.5%)
    max_notional_pct: float = 1.00     # cap any single position at this share of equity
    max_positions: int = 3
    max_trades_per_day: int = 1        # per symbol
    slippage_bps: float = 1.0          # per side, adverse
    commission_per_share: float = 0.0
    commission_per_trade: float = 0.0
    # close      fill at the signal bar's close (a market order fired on close)
    # next_open  fill at the next bar's open (most conservative)
    # level      fill at a price the plan names in `entry_level`, which models a
    #            resting stop or limit order placed in advance. The level is known
    #            before the session, so this is not lookahead, but the entry bar
    #            MUST then be checked for the stop too: price that trades through
    #            your entry can trade through your stop in the same bar.
    fill: str = "close"
    ambiguous: str = "stop_first"       # stop_first | target_first
    flat_at_minute: int = 385           # minutes from 09:30; 385 = 15:55, flatten before the close
    max_bars_held: Optional[int] = None
    allow_shorts: bool = True
    instrument: Instrument = field(default_factory=Instrument)
    max_margin_pct: float = 0.30       # futures: cap margin used per position
    # One contract is the smallest tradeable unit. When the risk budget buys less
    # than one, a real trader takes one and accepts the larger risk, or stands
    # aside. Silently dropping the trade instead biases the sample toward
    # narrow-range sessions, which is exactly the kind of selection that makes a
    # backtest describe a different strategy than the one you would run.
    allow_min_one_unit: bool = True
    hard_max_risk_pct: float = 0.02    # ...but never risk more than this on one trade
    # A 2:1 setup can be re-attempted after a stop: -1R then +2R still nets +1R.
    # Only ever after a STOP, never after a target or a time exit, otherwise this
    # is not a re-entry rule, it is just trading the same level repeatedly.
    reentry_after_stop_only: bool = True


@dataclass
class BacktestResult:
    trades: List[Trade]
    equity_curve: pd.Series
    config: EngineConfig
    decider_stats: Dict = field(default_factory=dict)
    diagnostics: Dict = field(default_factory=dict)

    def trades_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        rows = []
        for t in self.trades:
            row = {
                "symbol": t.symbol,
                "side": "long" if t.side > 0 else "short",
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "entry": round(t.entry_price, 4),
                "exit": round(t.exit_price, 4) if t.exit_price else None,
                "shares": t.shares,
                "stop": round(t.stop, 4),
                "target": round(t.target, 4),
                "reason": t.exit_reason,
                "ideal_pnl": round(t.ideal_pnl, 2),
                "gross_pnl": round(t.gross_pnl, 2),
                "fees": round(t.fees, 4),
                "slippage": round(t.slippage_cost, 2),
                "net_pnl": round(t.net_pnl, 2),
                "R": round(t.r_multiple, 3),
                "hold_min": round(t.hold_minutes, 1),
                "prob": round(t.entry_prob, 3),
                "note": t.decision_note,
            }
            row.update({k: v for k, v in t.features.items()})
            rows.append(row)
        return pd.DataFrame(rows)


class _Open:
    """An open position being tracked bar by bar."""
    __slots__ = ("trade", "bars_held", "entry_slip")

    def __init__(self, trade: Trade, entry_slip: float):
        self.trade = trade
        self.bars_held = 0
        self.entry_slip = entry_slip


def _slip(price: float, is_buy: bool, bps: float,
          inst: Optional[Instrument] = None) -> float:
    """Move the fill against us: whole ticks for futures, basis points otherwise."""
    if inst is not None and inst.slippage_ticks > 0:
        offset = inst.slippage_ticks * inst.tick_size
        return price + offset if is_buy else price - offset
    factor = 1 + (bps / 10_000) * (1 if is_buy else -1)
    return price * factor


def _sale_fees(shares: int, price: float) -> float:
    principal = shares * price
    return principal * SEC_FEE_RATE + min(shares * TAF_PER_SHARE, TAF_CAP)


class Engine:
    def __init__(self, config: Optional[EngineConfig] = None):
        self.cfg = config or EngineConfig()

    # ---------------------------------------------------------- sizing

    def _size(self, equity: float, price: float, stop: float) -> int:
        inst = self.cfg.instrument
        risk_per_unit = abs(price - stop) * inst.multiplier
        if risk_per_unit <= 0:
            return 0
        by_risk = (equity * self.cfg.risk_pct) / risk_per_unit
        if inst.kind == "future":
            # Margin, not notional. A notional cap would reject every contract.
            cap = ((equity * self.cfg.max_margin_pct) / inst.margin_per_unit
                   if inst.margin_per_unit > 0 else by_risk)
        else:
            cap = (equity * self.cfg.max_notional_pct) / price
        units = int(max(0, np.floor(min(by_risk, cap))))
        if units == 0 and self.cfg.allow_min_one_unit and cap >= 1:
            # take one unit only if that stays inside the hard risk ceiling
            if risk_per_unit <= equity * self.cfg.hard_max_risk_pct:
                units = 1
        return units

    # ---------------------------------------------------------- exits

    def _check_exit(self, pos: _Open, high: float, low: float) -> Optional[Tuple[float, str]]:
        """Did this bar take us out? Returns (exit_price_before_slippage, reason)."""
        t = pos.trade
        if t.side > 0:
            hit_stop, hit_target = low <= t.stop, high >= t.target
        else:
            hit_stop, hit_target = high >= t.stop, low <= t.target

        if hit_stop and hit_target:
            if self.cfg.ambiguous == "target_first":
                return t.target, "TARGET*"
            return t.stop, "STOP*"          # '*' marks an ambiguous bar
        if hit_stop:
            return t.stop, "STOP"
        if hit_target:
            return t.target, "TARGET"
        return None

    def _close(self, pos: _Open, raw_exit: float, ts: pd.Timestamp, reason: str) -> Trade:
        t = pos.trade
        inst = self.cfg.instrument
        is_buy_to_close = t.side < 0
        if reason.startswith("TARGET"):
            fill = raw_exit           # resting limit: fills at the price or better
        else:
            fill = _slip(raw_exit, is_buy_to_close, self.cfg.slippage_bps, inst)

        t.exit_time, t.exit_price, t.exit_reason = ts, fill, reason
        t.raw_exit = raw_exit
        t.bars_held = pos.bars_held
        mult = inst.multiplier
        t.gross_pnl = t.side * (fill - t.entry_price) * t.shares * mult
        # P&L if every fill had been perfect. net = ideal - slippage - fees, exactly.
        t.ideal_pnl = t.side * (raw_exit - t.raw_entry) * t.shares * mult

        exit_slip = abs(fill - raw_exit) * t.shares * mult
        t.slippage_cost = pos.entry_slip + exit_slip

        fees = self.cfg.commission_per_trade * 2 + self.cfg.commission_per_share * t.shares * 2
        fees += inst.commission_per_unit * t.shares * 2
        if inst.reg_fees:
            # The sell side pays regulatory fees: the exit for a long, the entry for a short.
            fees += _sale_fees(t.shares, fill if t.side > 0 else t.entry_price)
        t.fees = fees
        t.net_pnl = t.gross_pnl - fees

        risk = abs(t.entry_price - t.stop) * t.shares * mult
        t.r_multiple = (t.net_pnl / risk) if risk > 0 else 0.0
        return t

    # ---------------------------------------------------------- main

    def run(
        self,
        plans: Dict[str, pd.DataFrame],
        strategy,
        decider: Decider,
        verbose: bool = True,
    ) -> BacktestResult:
        """
        `plans` maps symbol -> DataFrame carrying OHLCV plus the strategy's
        columns: signal ('long'/'short'/''), stop, target, minutes_from_open,
        and whatever features the strategy declared.

        The hot path reads numpy arrays, not pandas rows. A `.loc` lookup per
        symbol per bar costs ~50us, which is invisible in one backtest and fatal
        across a parameter sweep. Pandas rows are built only on the rare bars
        that actually carry a signal.
        """
        cfg = self.cfg
        equity = cfg.starting_equity
        trades: List[Trade] = []
        open_pos: Dict[str, _Open] = {}
        pending: Dict[str, pd.Series] = {}
        day_count: Dict[Tuple[str, pd.Timestamp], int] = {}
        side_count: Dict[Tuple[str, pd.Timestamp, int], int] = {}
        last_exit: Dict[Tuple[str, pd.Timestamp, int], str] = {}
        equity_points: List[Tuple[pd.Timestamp, float]] = []

        diag = {
            "candidates": 0, "approved": 0, "rejected": 0,
            "skipped_no_size": 0, "skipped_max_positions": 0,
            "skipped_day_limit": 0, "ambiguous_bars": 0, "shorts_blocked": 0,
        }

        syms = list(plans)
        arr = {}
        for sym in syms:
            df = plans[sym]
            arr[sym] = {
                "ts": df.index,
                "open": df["open"].to_numpy(dtype=float),
                "high": df["high"].to_numpy(dtype=float),
                "low": df["low"].to_numpy(dtype=float),
                "close": df["close"].to_numpy(dtype=float),
                "mfo": df["minutes_from_open"].to_numpy(dtype=float),
                "sig": df["signal"].to_numpy(dtype=object),
                "df": df,
            }

        # One merged, time-ordered event stream. A stable sort keeps symbols in
        # their original order when two bars share a timestamp.
        keys, sidx, bidx = [], [], []
        for si, sym in enumerate(syms):
            n = len(arr[sym]["ts"])
            keys.append(arr[sym]["ts"].asi8)
            sidx.append(np.full(n, si, dtype=np.int32))
            bidx.append(np.arange(n, dtype=np.int64))
        if not keys:
            return BacktestResult([], pd.Series(dtype=float), cfg, decider.stats(), diag)
        keys = np.concatenate(keys)
        sidx = np.concatenate(sidx)
        bidx = np.concatenate(bidx)
        order = np.argsort(keys, kind="stable")

        if verbose:
            print(f"[engine] {len(order):,} bar events across {len(syms)} symbols")

        last_day = None
        for e in order:
            si = sidx[e]
            i = bidx[e]
            sym = syms[si]
            a = arr[sym]
            ts = a["ts"][i]
            day = ts.normalize()

            if last_day is not None and day != last_day:
                equity_points.append((last_day, equity))
            last_day = day

            o, h, lo_, c = a["open"][i], a["high"][i], a["low"][i], a["close"][i]
            mfo = a["mfo"][i]

            # --- 1. fill a pending entry at this bar's open -------------------
            if sym in pending and sym not in open_pos:
                sig_row = pending.pop(sym)
                opened = self._try_open(sym, sig_row, float(o), ts, equity,
                                        open_pos, day_count, day, diag)
                if opened is not None:
                    open_pos[sym] = opened

            # --- 2. manage an open position -----------------------------------
            if sym in open_pos:
                pos = open_pos[sym]
                # A position opened on THIS bar's close cannot be stopped on it.
                if pos.trade.entry_time != ts or cfg.fill in ("next_open", "level"):
                    pos.bars_held += 1
                    hit = self._check_exit(pos, float(h), float(lo_))
                    if hit:
                        raw, reason = hit
                        if reason.endswith("*"):
                            diag["ambiguous_bars"] += 1
                        t = self._close(pos, raw, ts, reason)
                        equity += t.net_pnl
                        trades.append(t)
                        last_exit[(sym, day, t.side)] = reason
                        del open_pos[sym]
                        continue

                timed_out = (cfg.max_bars_held is not None
                             and pos.bars_held >= cfg.max_bars_held)
                if mfo >= cfg.flat_at_minute or timed_out:
                    reason = "TIME" if timed_out else "EOD"
                    t = self._close(pos, float(c), ts, reason)
                    equity += t.net_pnl
                    trades.append(t)
                    last_exit[(sym, day, t.side)] = reason
                    del open_pos[sym]
                    continue

            # --- 3. consider a new candidate ----------------------------------
            sig = a["sig"][i]
            if sig and sym not in open_pos and sym not in pending:
                diag["candidates"] += 1
                row = a["df"].iloc[i]          # cold path: signals are rare
                snap = strategy.snapshot(sym, ts, row)
                dec = decider.decide(snap)
                if dec.action in ENTRIES:
                    diag["approved"] += 1
                    row = row.copy()
                    row["_prob"] = dec.probabilities.get(dec.action.value, dec.confidence)
                    row["_note"] = dec.note
                    row["_aux"] = dec.aux
                    if cfg.fill == "next_open":
                        pending[sym] = row
                    else:
                        raw = (float(row["entry_level"]) if cfg.fill == "level"
                               and "entry_level" in row and pd.notna(row["entry_level"])
                               else float(c))
                        opened = self._try_open(sym, row, raw, ts, equity,
                                                open_pos, day_count, day, diag,
                                                last_exit, side_count)
                        if opened is not None:
                            open_pos[sym] = opened
                            if cfg.fill == "level":
                                # The bar that filled you can also stop you out.
                                hit = self._check_exit(opened, float(h), float(lo_))
                                if hit:
                                    raw_x, reason = hit
                                    if reason.endswith("*"):
                                        diag["ambiguous_bars"] += 1
                                    t = self._close(opened, raw_x, ts, reason)
                                    equity += t.net_pnl
                                    trades.append(t)
                                    del open_pos[sym]
                else:
                    diag["rejected"] += 1

        if last_day is not None:
            equity_points.append((last_day, equity))

        curve = pd.Series(dict(equity_points)).sort_index()
        diag["open_at_end"] = len(open_pos)
        return BacktestResult(
            trades=trades, equity_curve=curve, config=cfg,
            decider_stats=decider.stats(), diagnostics=diag,
        )

    # ---------------------------------------------------------- open

    def _try_open(self, sym, row, raw_price, ts, equity, open_pos,
                  day_count, day, diag, last_exit=None, side_count=None) -> Optional[_Open]:
        cfg = self.cfg
        side = 1 if str(row["signal"]) == "long" else -1
        side_count = side_count if side_count is not None else {}
        last_exit = last_exit if last_exit is not None else {}
        # Re-entry is a per-SIDE concept: a long after a short is a first attempt.
        attempt = side_count.get((sym, day, side), 0) + 1
        if attempt > 1 and cfg.reentry_after_stop_only:
            prev = last_exit.get((sym, day, side))
            if prev is None or not prev.startswith("STOP"):
                diag["skipped_not_stopped"] = diag.get("skipped_not_stopped", 0) + 1
                return None

        if side < 0 and not cfg.allow_shorts:
            diag["shorts_blocked"] += 1
            return None
        if len(open_pos) >= cfg.max_positions:
            diag["skipped_max_positions"] += 1
            return None
        if day_count.get((sym, day), 0) >= cfg.max_trades_per_day:
            diag["skipped_day_limit"] += 1
            return None

        inst = cfg.instrument
        stop, target = float(row["stop"]), float(row["target"])
        fill = _slip(raw_price, is_buy=(side > 0), bps=cfg.slippage_bps, inst=inst)

        # Slippage can push the fill through its own stop. That trade is void.
        if (side > 0 and fill <= stop) or (side < 0 and fill >= stop):
            diag["skipped_no_size"] += 1
            return None

        shares = self._size(equity, fill, stop)
        if shares < 1:
            diag["skipped_no_size"] += 1
            return None

        day_count[(sym, day)] = day_count.get((sym, day), 0) + 1
        side_count[(sym, day, side)] = attempt
        feats = {k: float(v) for k, v in row.items()
                 if k in getattr(self, "_feature_cols", set()) and pd.notna(v)}
        aux = row.get("_aux") or {}
        feats.update({k: float(v) for k, v in aux.items()})

        trade = Trade(
            symbol=sym, side=side, entry_time=ts, entry_price=fill, shares=shares,
            stop=stop, target=target, raw_entry=raw_price,
            entry_prob=float(row.get("_prob", 1.0)),
            decision_note=str(row.get("_note", "")), features=feats,
        )
        trade.features["attempt"] = float(attempt)
        entry_slip = abs(fill - raw_price) * shares * inst.multiplier
        return _Open(trade, entry_slip)

    def set_feature_cols(self, cols) -> None:
        self._feature_cols = set(cols)
