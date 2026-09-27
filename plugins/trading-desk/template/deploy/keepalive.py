#!/usr/bin/env python3
"""
keepalive.py
============
Oracle's Always Free tier stops instances that average under 5% CPU over a
24-hour window. Three trading bots that wake once every five minutes and
otherwise sleep will trip that and get shut down, usually overnight, usually
right before you wanted them running.

This burns a small, adjustable slice of one core so the 24-hour average stays
above the threshold, and does nothing else. It is deliberately the dumbest
possible solution: anything cleverer is harder to reason about at 3am.

  KEEPALIVE_TARGET_PCT   percent of one core to occupy (default 8)
  KEEPALIVE_PERIOD_SEC   duty-cycle window (default 10)
"""

import hashlib
import os
import time

TARGET = float(os.environ.get("KEEPALIVE_TARGET_PCT", "8")) / 100.0
PERIOD = float(os.environ.get("KEEPALIVE_PERIOD_SEC", "10"))


def burn(seconds: float) -> None:
    end = time.time() + seconds
    h = hashlib.sha256(b"keepalive")
    while time.time() < end:
        for _ in range(2000):
            h.update(h.digest())


if __name__ == "__main__":
    busy = max(0.05, min(PERIOD * TARGET, PERIOD))
    idle = max(0.0, PERIOD - busy)
    print(f"[keepalive] holding ~{TARGET*100:.0f}% of one core "
          f"({busy:.2f}s busy / {idle:.2f}s idle per {PERIOD:.0f}s)", flush=True)
    last = time.time()
    while True:
        burn(busy)
        time.sleep(idle)
        if time.time() - last > 3600:
            print(f"[keepalive] alive at {time.strftime('%Y-%m-%d %H:%M:%S%z')}", flush=True)
            last = time.time()
