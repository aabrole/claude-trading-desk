"""
strategy.py — Opening Range Breakout
====================================
Built to the spec the two source videos describe, with every disputed choice
turned into a parameter instead of an opinion:

  range          high/low of the first `or_minutes` after 09:30 ET
  size filter    range must be between min/max multiples of ATR, else no trade
                 that day (a dead range has nothing to break, a huge one puts
                 the stop too far away to pay for itself)
  confirmation   the PREVIOUS bar closed inside the range and THIS bar closed
                 outside it. Checking only the breakout bar's open misses days
                 that gap straight through the range.
  entry          close of the confirming bar
  stop           the opposite edge of the opening range
  target         stop distance x `rr`
  window         no entries after `last_entry_min` (11:45 ET by default)
  one and done   a single attempt per symbol per day

Note on the ATR period: the bot video uses 1344, described as "14 days of
15-minute candles". That is 14 x 96, which is right for a 24-hour forex or
futures instrument. A US equity session has 26 fifteen-minute bars, so 14 days
is 364. We derive it from the session length rather than copying the constant.

Variants, off by default so the baseline stays honest:
  require_retest    enter only after price returns to the broken edge, then
                    resumes. The "I ONLY trade it if it RETESTS" rule.
  fakeout_reentry   when a breakout closes back inside the range, take the
                    OPPOSITE side. The critique video's proposed fix.

Nothing in here filters by trend. That belongs to the decision layer, which is
the entire experiment: same candidates, three different ways of choosing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import features as F
from contracts import Action, Snapshot
from decision import JevPrompt

RTH_MINUTES = 390


@dataclass
class ORBConfig:
    or_minutes: int = 15          # length of the opening range
    exec_minutes: int = 5         # bar size we detect breakouts and fill on
    rr: float = 2.0               # target = rr x risk
    atr_days: int = 14
    min_atr_mult: float = 0.5     # range must be at least this many ATRs
    max_atr_mult: float = 2.0     # ...and no more than this many
    last_entry_min: int = 135     # 11:45 ET
    require_retest: bool = False
    fakeout_reentry: bool = False
    ema_fast: int = 50
    ema_slow: int = 200


FEATURE_COLS = [
    "or_size_atr", "extension_atr", "trend_align", "ema_fast_dist_atr",
    "ema_slow_dist_atr", "vol_ratio_tod", "upper_wick_pct", "lower_wick_pct",
    "body_pct", "touches_before_break", "overnight_gap_atr", "trend_atr",
    "minutes_from_open", "atr_pct",
    # Structure: where price sits relative to levels the whole market watches.
    # A breakout into yesterday's high or a round number is where moves stall.
    "dist_pdh_atr", "dist_pdl_atr", "or_inside_prior_range",
    "round_above_atr", "round_below_atr", "room_to_target_atr",
]


class ORBStrategy:
    name = "orb"

    def __init__(self, cfg: Optional[ORBConfig] = None):
        self.cfg = cfg or ORBConfig()
        self.feature_cols = FEATURE_COLS

    # ------------------------------------------------------------ prep

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """OHLCV bars in, a plan with signal/stop/target/features out."""
        cfg = self.cfg
        out = df.copy()
        out["minutes_from_open"] = F.minutes_from_open(out)

        # --- higher timeframe context, carried forward without leaking -------
        htf = F.resample(out, f"{cfg.or_minutes}min")
        bars_per_day = max(1, RTH_MINUTES // cfg.or_minutes)
        atr_period = max(14, cfg.atr_days * bars_per_day)

        htf_atr = F.atr(htf, atr_period)
        htf_ema_f = F.ema(htf["close"], cfg.ema_fast)
        htf_ema_s = F.ema(htf["close"], cfg.ema_slow)

        out["atr"] = F.align_higher_timeframe(out, htf_atr, "atr")
        out["ema_fast"] = F.align_higher_timeframe(out, htf_ema_f, "ema_fast")
        out["ema_slow"] = F.align_higher_timeframe(out, htf_ema_s, "ema_slow")
        out["atr_pct"] = out["atr"] / out["close"]

        # --- opening range, per session -------------------------------------
        day = out.index.normalize()
        in_range = (out["minutes_from_open"] >= 0) & (out["minutes_from_open"] < cfg.or_minutes)
        rng = out[in_range].groupby(out.index[in_range].normalize())
        or_high = rng["high"].max()
        or_low = rng["low"].min()
        out["or_high"] = pd.Series(day, index=out.index).map(or_high)
        out["or_low"] = pd.Series(day, index=out.index).map(or_low)
        out["or_size"] = out["or_high"] - out["or_low"]
        out["or_size_atr"] = out["or_size"] / out["atr"]

        # --- candle shape and participation ---------------------------------
        out = out.join(F.wick_ratios(out))
        tod_vol = F.rolling_time_of_day_mean(out, "volume", days=20)
        out["vol_ratio_tod"] = out["volume"] / tod_vol.replace(0, np.nan)

        # --- trend alignment -------------------------------------------------
        above_fast = out["close"] > out["ema_fast"]
        above_slow = out["close"] > out["ema_slow"]
        stack_up = out["ema_fast"] > out["ema_slow"]
        # NaN comparisons are all False, which would read as "fully bearish".
        # Until both EMAs exist there is no trend to report.
        have_emas = out["ema_fast"].notna() & out["ema_slow"].notna()
        out["trend_state"] = np.select(
            [~have_emas,
             above_fast & above_slow & stack_up,
             (~above_fast) & (~above_slow) & (~stack_up)],
            [np.nan, 1, -1], default=0,
        )
        out["ema_fast_dist_atr"] = (out["close"] - out["ema_fast"]) / out["atr"]
        out["ema_slow_dist_atr"] = (out["close"] - out["ema_slow"]) / out["atr"]

        # --- session position and overnight gap ------------------------------
        sess_open = out.groupby(day)["open"].transform("first")
        out["trend_atr"] = (out["close"] - sess_open) / out["atr"]
        prev_close = out.groupby(day)["close"].last().shift(1)
        out["overnight_gap_atr"] = (
            (sess_open - pd.Series(day, index=out.index).map(prev_close)) / out["atr"]
        )

        # --- prior-session levels --------------------------------------------
        day_s = pd.Series(day, index=out.index)
        pdh = out.groupby(day)["high"].max().shift(1)
        pdl = out.groupby(day)["low"].min().shift(1)
        out["prior_day_high"] = day_s.map(pdh)
        out["prior_day_low"] = day_s.map(pdl)
        out["dist_pdh_atr"] = (out["close"] - out["prior_day_high"]) / out["atr"]
        out["dist_pdl_atr"] = (out["close"] - out["prior_day_low"]) / out["atr"]
        out["or_inside_prior_range"] = (
            (out["or_high"] <= out["prior_day_high"]) & (out["or_low"] >= out["prior_day_low"])
        ).astype(float)

        # --- round-number barriers -------------------------------------------
        # Price clusters at whole numbers, and a round level sitting between your
        # entry and your target is where a breakout runs out of buyers.
        step = np.where(out["close"] >= 200, 5.0, np.where(out["close"] >= 20, 1.0, 0.5))
        nxt_up = np.ceil(out["close"] / step) * step
        nxt_dn = np.floor(out["close"] / step) * step
        out["round_above_atr"] = (nxt_up - out["close"]) / out["atr"]
        out["round_below_atr"] = (out["close"] - nxt_dn) / out["atr"]

        # --- breakout detection, per session ---------------------------------
        return self._detect(out)

    # ------------------------------------------------------- detection

    def _detect(self, out: pd.DataFrame) -> pd.DataFrame:
        cfg = self.cfg
        n = len(out)
        signal = np.array([""] * n, dtype=object)
        stop = np.full(n, np.nan)
        target = np.full(n, np.nan)
        extension = np.full(n, np.nan)
        touches = np.zeros(n)
        trend_align = np.zeros(n)
        room = np.full(n, np.nan)

        cols = {c: out[c].to_numpy() for c in
                ["high", "low", "close", "minutes_from_open", "or_high", "or_low",
                 "or_size_atr", "trend_state", "atr", "ema_slow", "vol_ratio_tod"]}
        day_key = out.index.normalize().to_numpy()
        day_starts = np.flatnonzero(np.r_[True, day_key[1:] != day_key[:-1]])
        day_bounds = list(zip(day_starts, np.r_[day_starts[1:], n]))

        for lo, hi in day_bounds:
            oh, ol = cols["or_high"][lo], cols["or_low"][lo]
            size_atr = cols["or_size_atr"][lo]
            if not np.isfinite(oh) or not np.isfinite(ol) or oh <= ol:
                continue
            # Range-size filter: skip the whole day if it fails.
            if not np.isfinite(size_atr) or not (cfg.min_atr_mult <= size_atr <= cfg.max_atr_mult):
                continue

            broke = 0                 # 0 none yet, +1 up, -1 down
            armed_retest = False
            fired = False
            touch_count = 0
            ext_high = -np.inf        # furthest the failed breakout ran
            ext_low = np.inf

            for i in range(lo, hi):
                if fired:
                    break
                mfo = cols["minutes_from_open"][i]
                if mfo < cfg.or_minutes:          # range still forming
                    continue
                if mfo > cfg.last_entry_min:      # outside the entry window
                    break

                c = cols["close"][i]
                prev_c = cols["close"][i - 1] if i > lo else c
                if cols["high"][i] >= oh or cols["low"][i] <= ol:
                    touch_count += 1

                prev_inside = ol <= prev_c <= oh
                closed_above, closed_below = c > oh, c < ol

                if broke == 0:
                    # Gap-safe confirmation: previous close inside, this one outside.
                    if prev_inside and closed_above:
                        broke = 1
                    elif prev_inside and closed_below:
                        broke = -1
                    else:
                        continue
                    ext_high, ext_low = cols["high"][i], cols["low"][i]

                    if cfg.require_retest:
                        armed_retest = True      # wait for a pullback to the edge
                        continue
                    if cfg.fakeout_reentry:
                        continue                 # wait for this breakout to FAIL
                    direction, edge, other = ("long", oh, ol) if broke == 1 else ("short", ol, oh)

                elif cfg.require_retest and armed_retest:
                    # Retest = price comes back to the broken edge, then closes
                    # back out in the breakout direction.
                    if broke == 1:
                        touched = cols["low"][i] <= oh
                        if touched and c > oh:
                            direction, edge, other = "long", oh, ol
                        else:
                            if c < ol:           # failed through the far side
                                broke, armed_retest = 0, False
                            continue
                    else:
                        touched = cols["high"][i] >= ol
                        if touched and c < ol:
                            direction, edge, other = "short", ol, oh
                        else:
                            if c > oh:
                                broke, armed_retest = 0, False
                            continue

                elif cfg.fakeout_reentry:
                    ext_high = max(ext_high, cols["high"][i])
                    ext_low = min(ext_low, cols["low"][i])
                    # The breakout closed back inside the range: fade it.
                    if not (ol <= c <= oh):
                        continue
                    if broke == 1:
                        # Upside break failed. Short it, stopping above the high
                        # the failed move made rather than at the range edge.
                        direction, edge, other = "short", oh, float(ext_high)
                    else:
                        direction, edge, other = "long", ol, float(ext_low)
                else:
                    continue

                # Every trade must carry real context. A missing ATR, EMA or
                # volume baseline would otherwise be rendered as 0.00 and read
                # as a fact by the decision layer.
                if not (np.isfinite(cols["atr"][i]) and np.isfinite(cols["ema_slow"][i])
                        and np.isfinite(cols["trend_state"][i])
                        and np.isfinite(cols["vol_ratio_tod"][i])):
                    continue

                signal[i] = direction
                stop[i] = other
                risk = abs(c - other)
                if risk <= 0:
                    signal[i] = ""
                    continue
                target[i] = c + cfg.rr * risk if direction == "long" else c - cfg.rr * risk
                extension[i] = abs(c - edge) / out["atr"].to_numpy()[i]
                room[i] = (cfg.rr * risk) / out["atr"].to_numpy()[i]
                touches[i] = touch_count
                want = 1 if direction == "long" else -1
                trend_align[i] = 1.0 if cols["trend_state"][i] == want else (
                    -1.0 if cols["trend_state"][i] == -want else 0.0)
                fired = True

        out["signal"] = signal
        out["stop"] = stop
        out["target"] = target
        out["extension_atr"] = extension
        out["touches_before_break"] = touches
        out["trend_align"] = trend_align
        out["room_to_target_atr"] = room
        return out

    # -------------------------------------------------------- snapshot

    def snapshot(self, symbol: str, ts: pd.Timestamp, row: pd.Series) -> Snapshot:
        side = str(row["signal"])
        proposed = Action.ENTER_LONG if side == "long" else Action.ENTER_SHORT
        feats = {c: (float(row[c]) if pd.notna(row.get(c)) else 0.0) for c in self.feature_cols}

        def g(k: str, default: float = 0.0) -> float:
            v = row.get(k)
            return default if v is None or pd.isna(v) else float(v)

        trend_word = {1: "aligned with the higher-timeframe trend",
                      -1: "against the higher-timeframe trend",
                      0: "in a mixed or undecided trend"}[int(g("trend_align"))]
        lines = [
            f"Symbol: {symbol}",
            f"Time: {ts.strftime('%Y-%m-%d %H:%M')} ET, {int(g('minutes_from_open'))} minutes after the open",
            f"Price: {float(row['close']):.2f}",
            f"Opening range ({self.cfg.or_minutes} min): high {g('or_high'):.2f}, low {g('or_low'):.2f}, "
            f"size {g('or_size_atr'):.2f} ATR",
            f"Breakout: closed {'above the range high' if side == 'long' else 'below the range low'}, "
            f"{g('extension_atr'):.2f} ATR beyond the edge",
            f"This breakout is {trend_word} "
            f"({self.cfg.ema_fast}/{self.cfg.ema_slow} EMA on the {self.cfg.or_minutes}-minute chart; "
            f"price is {g('ema_fast_dist_atr'):+.2f} ATR from the {self.cfg.ema_fast} EMA and "
            f"{g('ema_slow_dist_atr'):+.2f} ATR from the {self.cfg.ema_slow} EMA)",
            f"Breakout bar volume vs the 20-day average for this time of day: {g('vol_ratio_tod', 1.0):.2f}x",
            f"Breakout bar shape: body {g('body_pct')*100:.0f}% of range, "
            f"upper wick {g('upper_wick_pct')*100:.0f}%, lower wick {g('lower_wick_pct')*100:.0f}%",
            f"The range edge was tested {int(g('touches_before_break'))} time(s) before this break",
            f"Session move from the open: {g('trend_atr'):+.2f} ATR; "
            f"overnight gap: {g('overnight_gap_atr'):+.2f} ATR",
            f"Planned stop: {g('stop'):.2f}, planned target: {g('target'):.2f} "
            f"(risking 1 to make {self.cfg.rr}); the target is "
            f"{g('room_to_target_atr'):.2f} ATR away",
        ]

        # Structure, described from the trade's own point of view.
        pdh, pdl = g("dist_pdh_atr"), g("dist_pdl_atr")
        lines.append(
            f"Yesterday's high is {abs(pdh):.2f} ATR "
            f"{'below' if pdh > 0 else 'above'} price; yesterday's low is "
            f"{abs(pdl):.2f} ATR {'below' if pdl > 0 else 'above'} price"
        )
        if g("or_inside_prior_range") > 0.5:
            lines.append("The opening range sits entirely inside yesterday's range")
        barrier = g("round_above_atr") if side == "long" else g("round_below_atr")
        lines.append(
            f"The next round-number level in the trade's direction is "
            f"{barrier:.2f} ATR away"
            + (" — closer than the target, so the move must push through it"
               if barrier < g("room_to_target_atr") else "")
        )
        lines.append("Position: flat")
        return Snapshot(
            symbol=symbol, timestamp=ts, price=float(row["close"]), proposed=proposed,
            features=feats, context_lines=lines,
            stop=float(row["stop"]), target=float(row["target"]),
        )

    # ----------------------------------------------------------- gates

    def gates(self, max_extension_atr: float = 0.75, max_vol_ratio: float = 3.0,
              require_trend: bool = True, min_body_pct: float = 0.5,
              max_touches: int = 3) -> List:
        """
        The control arm: the critique video's advice, written as if-statements.

        If these match Jev, the honest conclusion is that you did not need Jev
        for this strategy. That result is worth filming too.
        """
        cfg_rr = self.cfg

        def trend_gate(s: Snapshot) -> Optional[str]:
            if require_trend and s.f("trend_align") < 0:
                return "counter_trend"
            return None

        def extension_gate(s: Snapshot) -> Optional[str]:
            # Chasing a bar that already ran puts the stop too far behind price.
            if s.f("extension_atr") > max_extension_atr:
                return "chased_too_far"
            return None

        def volume_gate(s: Snapshot) -> Optional[str]:
            if s.f("vol_ratio_tod", 1.0) > max_vol_ratio:
                return "volume_blowoff"
            return None

        def shape_gate(s: Snapshot) -> Optional[str]:
            # A breakout bar that is mostly wick is a rejection, not a breakout.
            if s.f("body_pct") < min_body_pct:
                return "weak_candle"
            return None

        def touches_gate(s: Snapshot) -> Optional[str]:
            # An edge tested many times is a liquidity pool, not a level.
            if s.f("touches_before_break") > max_touches:
                return "edge_overworked"
            return None

        return [trend_gate, extension_gate, volume_gate, shape_gate, touches_gate]

    # ------------------------------------------------------ jev prompt

    def jev_prompt(self) -> JevPrompt:
        return JevPrompt(
            entry_instructions=(
                "An opening-range-breakout system has confirmed a breakout and wants to "
                "enter now, risking 1 unit to make {rr}. Breakouts of the opening range "
                "frequently fail: price pushes past the edge, triggers stops, and reverses "
                "back into the range. Using only the state above, decide whether to take "
                "this trade now or stand aside."
            ).format(rr=self.cfg.rr),
            entry_criteria={
                Action.ENTER_LONG.value:
                    "The upside breakout is likely to continue far enough to reach the "
                    "target before the stop. Buy now.",
                Action.ENTER_SHORT.value:
                    "The downside breakout is likely to continue far enough to reach the "
                    "target before the stop. Sell short now.",
                Action.WAIT.value:
                    "This breakout is likely to fail and reverse back into the range, or "
                    "the setup is not clean enough to risk capital on. Take no position.",
            },
            extra_questions={
                # Recorded, never acted on. Lets us check afterwards whether Jev's
                # stated fakeout risk actually predicted the fakeouts.
                "fakeout_risk": {
                    "type": "score",
                    "instructions": ("How likely this breakout is a false move that reverses "
                                     "back into the opening range"),
                    "criteria": ["Very likely to continue in the breakout direction",
                                 "Genuinely unclear",
                                 "Very likely a fakeout that reverses"],
                },
                "trend_support": {
                    "type": "noul",
                    "instructions": ("The higher-timeframe trend supports trading in the "
                                     "breakout direction"),
                },
            },
        )
