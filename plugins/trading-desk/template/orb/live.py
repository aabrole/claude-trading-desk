#!/usr/bin/env python3
"""
live.py
=======
Runs the validated ORB strategy against Alpaca paper trading.

It imports `ORBStrategy` and `JevDecider` directly from the backtest. Nothing is
reimplemented here, because a second copy of the logic drifts from the one you
measured and then your backtest no longer describes what is running.

  python3 live.py --once --dry-run      # one cycle, no orders, safe any time
  python3 live.py --dry-run             # full session, logs intended orders only
  python3 live.py                       # submits paper orders

Safety, all on by default:
  - paper=True is hardcoded. There is no code path to the live endpoint.
  - a kill switch file (out/STOP) halts new entries without killing the process
  - a daily loss limit flattens and stands down
  - bracket orders, so the stop and target exist at the broker even if this dies
  - refuses any order whose stop is on the wrong side of the fill
  - flattens everything before the close

Every decision and fill is logged in the backtest's schema so live results can be
reconciled against the backtest's distribution. Realised slippage versus the 1 bp
assumed is the number most likely to invalidate the whole thing, and it is
measurable from the first fill.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpaca.trading.client import TradingClient                      # noqa: E402
from alpaca.trading.requests import (MarketOrderRequest, StopLossRequest,   # noqa: E402
                                     TakeProfitRequest)
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce   # noqa: E402

from contracts import Action, ENTRIES                                # noqa: E402
from data import fetch_universe                                      # noqa: E402
from decision import JevDecider, RuleDecider                         # noqa: E402
from strategy import ORBConfig, ORBStrategy                          # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
KILL_SWITCH = OUT / "STOP"
STATE_PATH = OUT / "live_state.json"
ET = "America/New_York"


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def log(msg: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def jlog(path: Path, rec: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


# ----------------------------------------------------------------- state

class State:
    """
    Survives restarts so a crash mid-session cannot double-enter. Keyed by
    session date, and reset automatically when the date rolls over.
    """

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.data = {"date": "", "entered": [], "start_equity": None, "halted": False}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except json.JSONDecodeError:
                pass

    def roll(self, today: str, equity: float) -> None:
        if self.data.get("date") != today:
            self.data = {"date": today, "entered": [], "start_equity": equity, "halted": False}
            self.save()

    def has_entered(self, symbol: str) -> bool:
        return symbol in self.data["entered"]

    def mark(self, symbol: str) -> None:
        self.data["entered"].append(symbol)
        self.save()

    def halt(self) -> None:
        self.data["halted"] = True
        self.save()

    @property
    def halted(self) -> bool:
        return bool(self.data.get("halted"))

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))


# ----------------------------------------------------------------- broker

class Broker:
    def __init__(self, dry_run: bool = False):
        key = os.environ.get("ALPACA_PAPER_KEY", "")
        secret = os.environ.get("ALPACA_PAPER_SECRET", "")
        if not key or not secret:
            raise SystemExit("ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET not set")
        # paper=True is hardcoded. This is the safety lock.
        self.client = TradingClient(key, secret, paper=True)
        self.dry_run = dry_run

    def account(self) -> dict:
        a = self.client.get_account()
        return {"equity": float(a.equity), "cash": float(a.cash),
                "buying_power": float(a.buying_power), "multiplier": float(a.multiplier)}

    def positions(self) -> Dict[str, float]:
        return {p.symbol: float(p.qty) for p in self.client.get_all_positions()}

    def is_open(self) -> bool:
        return bool(self.client.get_clock().is_open)

    def submit_bracket(self, symbol: str, side: int, qty: int,
                       stop: float, target: float) -> Optional[dict]:
        req = MarketOrderRequest(
            symbol=symbol, qty=qty,
            side=OrderSide.BUY if side > 0 else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,        # everything cancels at the close
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
        )
        if self.dry_run:
            log(f"  DRY RUN would submit: {'BUY' if side > 0 else 'SELL'} {qty} {symbol} "
                f"stop {stop:.2f} target {target:.2f}")
            return None
        o = self.client.submit_order(req)
        return {"id": str(o.id), "symbol": symbol, "qty": qty,
                "side": "buy" if side > 0 else "sell", "status": str(o.status)}

    def flatten_all(self) -> None:
        pos = self.positions()
        if not pos:
            return
        log(f"flattening {len(pos)} position(s): {list(pos)}")
        if self.dry_run:
            return
        self.client.cancel_orders()
        self.client.close_all_positions(cancel_orders=True)


# ----------------------------------------------------------------- runner

class LiveRunner:
    def __init__(self, args):
        self.args = args
        self.cfg = ORBConfig(or_minutes=args.or_minutes, exec_minutes=args.exec_minutes,
                             rr=args.rr, last_entry_min=args.last_entry_min)
        self.strat = ORBStrategy(self.cfg)
        self.broker = Broker(dry_run=args.dry_run)
        self.state = State()
        self.decisions = OUT / "live_decisions.jsonl"
        self.fills = OUT / "live_fills.jsonl"

        if args.decider == "jev":
            self.decider = JevDecider(
                prompt=self.strat.jev_prompt(), threshold=args.jev_threshold,
                log_path=OUT / "live_jev_stream.jsonl",
            )
        else:
            self.decider = RuleDecider()

    # -- data ------------------------------------------------------

    def bars(self, as_of: Optional[dt.date] = None) -> Dict[str, pd.DataFrame]:
        """
        Always a fresh pull. The disk cache is keyed by date range and would
        happily hand back a stale frame mid-session, which is exactly how a live
        bot ends up trading yesterday's setup.

        Feed note: the backtest used SIP, but a basic Alpaca subscription refuses
        SIP data from the last ~15 minutes, which is precisely the bar an intraday
        breakout needs. IEX is free and real time. Measured over 120 days, IEX
        prices track SIP to under 1 bp and 87-90% of signals land on the same bar
        with 100% direction agreement, at the cost of stop levels differing by
        1-3% of risk. That is the tracking error you accept by not paying for SIP.
        """
        ref = as_of or dt.date.today()
        start = (ref - dt.timedelta(days=self.args.warmup_days)).isoformat()
        return fetch_universe(symbols=self.args.symbols, start=start,
                              end=(ref + dt.timedelta(days=1)).isoformat(),
                              minutes=self.args.exec_minutes, feed=self.args.feed,
                              use_cache=bool(as_of), verbose=False)

    # -- sizing ----------------------------------------------------

    def size(self, equity: float, price: float, stop: float) -> int:
        risk_per_share = abs(price - stop)
        if risk_per_share <= 0:
            return 0
        by_risk = (equity * self.args.risk_pct) / risk_per_share
        by_notional = (equity * self.args.max_notional_pct) / price
        return int(max(0, min(by_risk, by_notional)))

    # -- one cycle -------------------------------------------------

    def cycle(self, now_override: Optional[pd.Timestamp] = None,
              bars_override: Optional[Dict[str, pd.DataFrame]] = None) -> None:
        acct = self.broker.account()
        now_et = now_override or pd.Timestamp.now(tz=ET)
        today = now_et.strftime("%Y-%m-%d")
        self.state.roll(today, acct["equity"])

        # --- guardrails, checked every cycle before anything else -----
        if KILL_SWITCH.exists():
            log("KILL SWITCH present (out/STOP). No new entries.")
            return
        if self.state.halted:
            log("halted for the day by the loss limit. No new entries.")
            return

        start_eq = self.state.data.get("start_equity") or acct["equity"]
        dd = (acct["equity"] / start_eq - 1) * 100
        if dd <= -self.args.daily_loss_pct:
            log(f"DAILY LOSS LIMIT hit ({dd:+.2f}% vs limit -{self.args.daily_loss_pct}%). "
                f"Flattening and standing down.")
            self.broker.flatten_all()
            self.state.halt()
            return

        held = self.broker.positions()
        bars = bars_override if bars_override is not None else self.bars()
        mfo = (now_et - now_et.normalize() - pd.Timedelta(hours=9, minutes=30)).total_seconds() / 60

        # --- flatten before the close ---------------------------------
        if mfo >= self.args.flat_at_minute and held:
            self.broker.flatten_all()
            return

        if now_override is None:
            log(f"cycle: equity ${acct['equity']:,.2f} ({dd:+.2f}% today)  "
                f"bp ${acct['buying_power']:,.0f}  positions {len(held)}  "
                f"{mfo:.0f} min from open")

        for sym, df in bars.items():
            if sym in held or self.state.has_entered(sym):
                continue
            if len(held) >= self.args.max_positions:
                log(f"  {sym}: at max positions ({self.args.max_positions}), skipping")
                continue

            plan = self.strat.prepare(df)
            today_bars = plan[plan.index.normalize() == pd.Timestamp(today, tz=ET)]
            if now_override is not None:
                # Never let the simulated clock see a bar that has not closed yet.
                today_bars = today_bars[today_bars.index < now_override]
            if today_bars.empty:
                continue
            last = today_bars.iloc[-1]
            age_min = (now_et - today_bars.index[-1]).total_seconds() / 60
            if age_min > self.args.exec_minutes * 3:
                log(f"  {sym}: latest bar is {age_min:.0f} min old, data feed is lagging. Skipping.")
                continue
            if not str(last.get("signal") or ""):
                continue

            snap = self.strat.snapshot(sym, today_bars.index[-1], last)
            dec = self.decider.decide(snap)
            rec = {"ts": str(today_bars.index[-1]), "symbol": sym, "signal": str(last["signal"]),
                   "price": float(last["close"]), "stop": float(last["stop"]),
                   "target": float(last["target"]), "action": dec.action.value,
                   "prob": dec.probabilities.get(dec.action.value, dec.confidence),
                   "probabilities": dec.probabilities, "note": dec.note, "aux": dec.aux}

            if dec.action not in ENTRIES:
                log(f"  {sym}: {last['signal']} breakout -> STAND ASIDE "
                    f"({dec.note or 'below threshold'})")
                jlog(self.decisions, {**rec, "submitted": False})
                continue

            side = 1 if str(last["signal"]) == "long" else -1
            price, stop, target = float(last["close"]), float(last["stop"]), float(last["target"])

            # A stop on the wrong side of the entry means something is wrong upstream.
            if (side > 0 and stop >= price) or (side < 0 and stop <= price):
                log(f"  {sym}: REFUSED, stop {stop:.2f} is on the wrong side of {price:.2f}")
                jlog(self.decisions, {**rec, "submitted": False, "refused": "bad_stop"})
                continue

            qty = self.size(acct["equity"], price, stop)
            if qty < 1:
                log(f"  {sym}: size rounds to 0 shares, skipping")
                jlog(self.decisions, {**rec, "submitted": False, "refused": "zero_size"})
                continue
            if qty * price > acct["buying_power"]:
                log(f"  {sym}: ${qty * price:,.0f} exceeds buying power, skipping")
                jlog(self.decisions, {**rec, "submitted": False, "refused": "buying_power"})
                continue

            log(f"  {sym}: {last['signal'].upper()} approved p={rec['prob']:.2f} -> "
                f"{qty} shares @ ~{price:.2f}, stop {stop:.2f}, target {target:.2f} "
                f"(risk ${abs(price - stop) * qty:.0f})")
            try:
                order = self.broker.submit_bracket(sym, side, qty, stop, target)
            except Exception as e:
                log(f"  {sym}: ORDER REJECTED {type(e).__name__}: {str(e)[:140]}")
                jlog(self.decisions, {**rec, "submitted": False, "error": str(e)[:200]})
                continue

            self.state.mark(sym)
            jlog(self.decisions, {**rec, "submitted": True, "qty": qty, "order": order})
            # signal_close vs actual fill is how you measure real slippage
            jlog(self.fills, {"ts": str(today_bars.index[-1]), "symbol": sym,
                              "side": "long" if side > 0 else "short", "qty": qty,
                              "signal_close": price, "stop": stop, "target": target,
                              "order": order})

    # -- loop ------------------------------------------------------

    def replay(self, day: str) -> None:
        """
        Walk one past session through the LIVE code path, bar by bar, submitting
        nothing. This is how you check that live.py and the backtest agree: any
        difference here is a bug in one of them, not a market condition.
        """
        self.broker.dry_run = True
        ref = dt.date.fromisoformat(day)
        bars = self.bars(as_of=ref)
        day_ts = pd.Timestamp(day, tz=ET)
        stamps = sorted({t for df in bars.values()
                         for t in df.index if t.normalize() == day_ts})
        if not stamps:
            log(f"no bars for {day} (market holiday or weekend?)")
            return
        log(f"replaying {day}: {len(stamps)} bars, {len(bars)} symbols, orders disabled")
        for ts in stamps:
            # the clock stands just after this bar closed
            self.cycle(now_override=ts + pd.Timedelta(minutes=self.args.exec_minutes),
                       bars_override=bars)
        log("replay complete")

    def run(self) -> None:
        if self.args.replay:
            self.replay(self.args.replay)
            return
        if self.args.once:
            self.cycle()
            return
        log(f"live runner started. decider={self.args.decider} "
            f"dry_run={self.args.dry_run} symbols={' '.join(self.args.symbols)}")
        log(f"kill switch: touch {KILL_SWITCH}")
        while True:
            try:
                if not self.broker.is_open():
                    clock = self.broker.client.get_clock()
                    wait = max(30.0, (clock.next_open - dt.datetime.now(dt.timezone.utc)).total_seconds())
                    log(f"market closed. next open {clock.next_open}. sleeping {wait/60:.0f} min")
                    time.sleep(min(wait, 3600))
                    continue
                self.cycle()
            except KeyboardInterrupt:
                log("interrupted. open positions are left alone; "
                    "brackets are at the broker. Use --flatten to close.")
                return
            except Exception as e:
                log(f"cycle error {type(e).__name__}: {str(e)[:160]}")
            # wake a few seconds after the next bar boundary so the bar is complete
            now = dt.datetime.now(dt.timezone.utc)
            m = self.args.exec_minutes
            nxt = (now.replace(second=0, microsecond=0)
                   + dt.timedelta(minutes=m - (now.minute % m)))
            time.sleep(max(5.0, (nxt - now).total_seconds() + self.args.bar_buffer_sec))


def parse_args():
    p = argparse.ArgumentParser(description="Paper-trade the ORB strategy on Alpaca.")
    p.add_argument("--symbols", nargs="+",
                   default=["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMD"])
    p.add_argument("--decider", default="jev", choices=["jev", "rules"])
    p.add_argument("--jev-threshold", type=float, default=0.30,
                   help="0.30 is what the shadow calibration supported, not 0.55")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--exec-minutes", type=int, default=5)
    p.add_argument("--rr", type=float, default=2.0)
    p.add_argument("--last-entry-min", type=int, default=135)
    p.add_argument("--flat-at-minute", type=int, default=385)
    p.add_argument("--risk-pct", type=float, default=0.005)
    p.add_argument("--max-notional-pct", type=float, default=1.0)
    p.add_argument("--max-positions", type=int, default=3)
    p.add_argument("--daily-loss-pct", type=float, default=2.0)
    p.add_argument("--feed", default="iex", choices=["iex", "sip"],
                   help="iex is free and real time. sip matches the backtest but a basic "
                        "subscription blocks the most recent 15 minutes, which is the bar "
                        "this strategy trades on.")
    p.add_argument("--warmup-days", type=int, default=60)
    p.add_argument("--bar-buffer-sec", type=float, default=20.0)
    p.add_argument("--once", action="store_true", help="one cycle then exit")
    p.add_argument("--replay", default=None, metavar="YYYY-MM-DD",
                   help="walk a past session through the live code path, no orders")
    p.add_argument("--dry-run", action="store_true", help="log intended orders, submit nothing")
    p.add_argument("--flatten", action="store_true", help="close everything and exit")
    return p.parse_args()


def main() -> None:
    load_env()
    args = parse_args()
    if args.flatten:
        Broker(dry_run=False).flatten_all()
        return
    LiveRunner(args).run()


if __name__ == "__main__":
    main()
