#!/usr/bin/env python3
"""
make_backtests.py
=================
Exports each bot's backtested equity curve to dashboard/backtests.json, so the
desk has something true to show while the market is shut and the live cards have
no numbers yet. A waiting card draws its curve, animated, with the percentage
counting along with it.

Read your curves from whatever your backtest already writes. Configure that in
dashboard/backtest_sources.json:

  {
    "bots": {
      "example": {
        "trades": 412,
        "caveat": "",
        "note": "One sentence the card shows under the numbers.",
        "lines": [
          {"name": "with the filter", "hero": true,
           "csv": "../strategies/example_sma/out/equity_filtered.csv"},
          {"name": "unfiltered", "hero": false,
           "csv": "../strategies/example_sma/out/equity_rules.csv"}
        ]
      }
    }
  }

Each csv needs a date-like first column and an "equity" column (or any single
other column, which is then taken as equity). Paths are relative to this file.

The hero line is the one whose percentage the card displays. A second line is
drawn behind it, which is how you show a filter's contribution honestly: same
signals, same period, one line filtered and one not.

Rules this enforces, because they are the difference between a chart and a
claim:
  - nothing is smoothed, interpolated or extended
  - thinning always keeps the last point, so a line ends where the data ends
  - `caveat` prints on the card itself, so a result that does not generalise
    says so on its face rather than in a footnote nobody reads

  python3 dashboard/make_backtests.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
except ImportError:
    sys.exit("make_backtests.py needs pandas: pip install pandas")

HERE = Path(__file__).resolve().parent
CONFIG = Path(__file__).resolve().parent / "backtest_sources.json"
OUT = HERE / "backtests.json"
MAX_POINTS = 170


def thin(s: "pd.Series", n: int = MAX_POINTS) -> List[List[float]]:
    """Every Nth point, the last always kept: the end of the line must be real."""
    s = s.dropna()
    if len(s) > n:
        step = len(s) // n + 1
        keep = list(range(0, len(s), step))
        if keep[-1] != len(s) - 1:
            keep.append(len(s) - 1)
        s = s.iloc[keep]
    return [[int(pd.Timestamp(t).timestamp()), round(float(v), 2)] for t, v in s.items()]


def load_curve(path: Path) -> Optional["pd.Series"]:
    if not path.exists():
        print("  missing: %s" % path)
        return None
    try:
        d = pd.read_csv(path, index_col=0)
    except Exception as e:
        print("  unreadable (%s): %s" % (e, path))
        return None
    if d.empty:
        print("  empty: %s" % path)
        return None
    col = "equity" if "equity" in d.columns else d.columns[0]
    s = d[col]
    s.index = pd.to_datetime(s.index, utc=True, errors="coerce")
    s = s.dropna()
    if len(s) < 2:
        print("  fewer than two usable rows: %s" % path)
        return None
    return s


def ret_pct(s: "pd.Series") -> float:
    return (s.iloc[-1] / s.iloc[0] - 1.0) * 100.0


def span(s: "pd.Series") -> str:
    return "%s to %s" % (s.index[0].strftime("%b %Y"), s.index[-1].strftime("%b %Y"))


def build(key: str, spec: Dict[str, Any]) -> Optional[dict]:
    lines: List[dict] = []
    hero: Optional["pd.Series"] = None
    for spec_line in spec.get("lines", []):
        csv = spec_line.get("csv")
        if not csv:
            continue
        s = load_curve((HERE / csv).resolve())
        if s is None:
            continue
        is_hero = bool(spec_line.get("hero")) or hero is None
        if is_hero and hero is None:
            hero = s
        lines.append({"name": str(spec_line.get("name", csv)),
                      "hero": is_hero and s is hero,
                      "points": thin(s)})
    if hero is None or not lines:
        return None
    second = ""
    others = [l for l in lines if not l["hero"]]
    if others:
        # Name the comparison line's own return, so the contrast is a number and
        # not just two shapes on a chart.
        for spec_line, l in zip(spec.get("lines", []), lines):
            if l is others[0]:
                s = load_curve((HERE / spec_line["csv"]).resolve())
                if s is not None:
                    second = "%s %+.1f%%" % (l["name"], ret_pct(s))
                break
    rec = {
        "key": key,
        "headline": ret_pct(hero),
        "period": span(hero),
        "trades": spec.get("trades"),
        "lines": lines,
        "note": str(spec.get("note", "")),
        "second": second,
    }
    if spec.get("caveat"):
        rec["caveat"] = str(spec["caveat"])
    return rec


def main() -> None:
    if not CONFIG.exists():
        sys.exit("no %s. See the docstring at the top of this file for its shape." % CONFIG)
    try:
        cfg = json.loads(CONFIG.read_text())
    except json.JSONDecodeError as e:
        sys.exit("%s is not valid JSON: %s" % (CONFIG, e))

    bots: Dict[str, dict] = {}
    for key, spec in (cfg.get("bots") or {}).items():
        if key.startswith("_"):
            continue
        rec = build(key, spec)
        if rec:
            bots[key] = rec
            print("  %-10s %+7.1f%%  %-22s %d line(s), %d points%s"
                  % (key, rec["headline"], rec["period"], len(rec["lines"]),
                     sum(len(l["points"]) for l in rec["lines"]),
                     "  [%s]" % rec["caveat"] if rec.get("caveat") else ""))
        else:
            print("  %-10s skipped: no usable curve" % key)

    if not bots:
        sys.exit("nothing exported. Check the csv paths in %s." % CONFIG)

    OUT.write_text(json.dumps({
        "generated_at": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%S%z"),
        "disclaimer": ("Backtested results, not live trading. Hypothetical and not a "
                       "prediction of future performance."),
        "bots": bots,
    }, separators=(",", ":")))
    print("\nwrote %s (%.1f KB)" % (OUT, OUT.stat().st_size / 1024))


if __name__ == "__main__":
    main()
