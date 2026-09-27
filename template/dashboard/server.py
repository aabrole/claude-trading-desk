#!/usr/bin/env python3
"""
server.py
=========
The dashboard backend. Reads the uniform per-bot state written by
core/botstate.py and serves it to the web app over plain HTTP.

Python standard library only. No FastAPI, no Flask, no uvicorn: the deploy
image is a python:3.11-slim with zero pip installs, and this file has to keep
it that way. Runs on 3.9 (the laptop) and 3.11 (the container).

Endpoints
  GET /                     the dashboard page (index.html, served as-is)
  GET /api/state            every bot snapshot + a leaderboard ranked by return
  GET /api/events?limit&bot recent rows from the .events.jsonl feeds
  GET /api/history?bot      equity time series, replayed from the event feed
  GET /api/stream           Server-Sent Events: a state frame every 2s, plus
                            new events the moment they land
  GET /api/health, /healthz liveness, for the container healthcheck

Server-Sent Events rather than WebSockets: SSE is a plain HTTP response that
never ends, so it is a dozen lines on top of http.server, it survives every
proxy that understands HTTP/1.1, and the browser reconnects on its own with no
client library. The data only flows one way here, so a socket would be framing
and handshake machinery for nothing.

Robustness contract: a missing, empty, half-written or malformed state file must
never produce a 500. Every filesystem touch in here is wrapped, and the worst
case for any one bot is that it renders as "not yet running".

  python3 dashboard/server.py                    # 0.0.0.0:8080
  DASHBOARD_PORT=9000 python3 dashboard/server.py
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import traceback
import zlib
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------- config

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

STATE_DIR = Path(os.environ.get("BOT_STATE_DIR", REPO / "state"))
WEB_ROOT = Path(os.environ.get("DASHBOARD_WEB", HERE))
HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))

# How long a snapshot can go untouched before the UI calls it stale. These bots
# heartbeat on a cycle measured in minutes and sleep overnight, so this is
# deliberately generous: a sleeping bot is not a broken bot.
STALE_AFTER_S = float(os.environ.get("DASHBOARD_STALE_AFTER", "900"))
STREAM_INTERVAL_S = float(os.environ.get("DASHBOARD_STREAM_INTERVAL", "2"))

# A second desk to report on, so the laptop can show whether the deployed one is
# up: DASHBOARD_REMOTE=http://<ip>:8080. Checked server side rather than from the
# browser, so it works regardless of CORS, mixed content or a phone on the LAN.
REMOTE_URL = os.environ.get("DASHBOARD_REMOTE", "").rstrip("/")
REMOTE_LABEL = os.environ.get("DASHBOARD_REMOTE_LABEL", "Oracle Cloud")
REMOTE_TIMEOUT_S = float(os.environ.get("DASHBOARD_REMOTE_TIMEOUT", "4"))
REMOTE_CACHE_S = float(os.environ.get("DASHBOARD_REMOTE_CACHE", "10"))
_remote_cache = {"t": 0.0, "data": None}
_remote_lock = threading.Lock()

# The roster is declared here, not discovered, so a bot that has never started
# still appears on the board as "not yet running" instead of silently missing.
# Anything found on disk that is not in this list is appended at runtime, so the
# board is never capped at four.
#
#   jev_mode  gating  = the Jev model decides whether the trade is taken
#             monitor = Jev is scored and recorded but does NOT gate anything
#             none    = no Jev in the loop
#   fills     broker    = a broker confirmed the fill (paper or live)
#             simulated = the bot filled itself against its own data feed
# The roster is declared, not discovered, so a bot that has never run still gets
# a card saying so instead of being invisible. Edit dashboard/bots.json to
# describe yours; this list is only the fallback when that file is absent.
#
# fills:    "broker" if a broker reports the fills, "simulated" if the bot books
#           its own. Say so on the card. Simulated fills are not results.
# jev_mode: "gating" if the model can veto a trade, "monitor" if its score is
#           only recorded, "none" if the strategy does not consult it.
SERIES = ["#e0595f", "#4a9eca", "#d4a13c", "#2f855a", "#7b61c4", "#15161a"]

BOTS_FILE = Path(os.environ.get("DASHBOARD_BOTS", HERE / "bots.json"))

DEFAULT_BOTS: List[Dict[str, Any]] = [
    {
        "key": "example",
        "agent": "1",
        "label": "Example strategy",
        "strategy": "Replace this in dashboard/bots.json with your own.",
        "broker": "Alpaca paper",
        "jev_mode": "none",
        "fills": "broker",
        "accent": "#4a9eca",
    },
]


def load_bots() -> List[Dict[str, Any]]:
    """
    Roster from bots.json, falling back to DEFAULT_BOTS. A malformed file must
    not stop the desk from starting, so it is reported and ignored.
    """
    try:
        raw = json.loads(BOTS_FILE.read_text())
    except FileNotFoundError:
        return DEFAULT_BOTS
    except (OSError, json.JSONDecodeError) as e:
        print("  WARNING: %s is unreadable (%s); using the default roster" % (BOTS_FILE, e),
              flush=True)
        return DEFAULT_BOTS
    entries = raw.get("bots") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        print("  WARNING: %s has no bots; using the default roster" % BOTS_FILE, flush=True)
        return DEFAULT_BOTS
    out = []
    for i, b in enumerate(entries):
        if not isinstance(b, dict) or not b.get("key"):
            continue
        out.append({
            "key": str(b["key"]),
            "agent": str(b.get("agent", i + 1)),
            "label": str(b.get("label", b["key"])),
            "strategy": str(b.get("strategy", "")),
            "broker": str(b.get("broker", "paper")),
            "jev_mode": str(b.get("jev_mode", "none")),
            "fills": str(b.get("fills", "broker")),
            "accent": str(b.get("accent", SERIES[i % len(SERIES)])),
        })
    return out or DEFAULT_BOTS


BOTS = load_bots()
BOT_META = {b["key"]: b for b in BOTS}

# Palette lifted from reports/viz_core.py so the dashboard and the reports read
# as one set. Used for bots discovered on disk that are not in BOTS.

# Event payload fields that move the equity curve. `value` is deliberately not
# here: in a decision event it is the notional of the order, not a P&L.
EQUITY_FIELDS = ("equity",)
PNL_FIELDS = ("pnl", "realized", "realized_pnl", "profit")

SAFE_BOT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# ---------------------------------------------------------------- primitives

def _num(v: Any) -> Optional[float]:
    """A float, or None. Never raises, and rejects bool/NaN/inf."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def parse_ts(ts: Any) -> Optional[float]:
    """botstate writes %Y-%m-%dT%H:%M:%S%z. Tolerate a few nearby shapes."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    try:  # 3.9 fromisoformat does not eat a trailing Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_json(path: Path) -> Optional[dict]:
    """
    Read one snapshot. botstate writes atomically (temp file + os.replace) so a
    torn read should be impossible, but a half-flushed file on a foreign
    filesystem is cheap to survive: retry once, then give up quietly.
    """
    for attempt in (0, 1):
        try:
            raw = path.read_text()
        except (OSError, UnicodeDecodeError):
            return None
        if not raw.strip():
            if attempt == 0:
                time.sleep(0.05)
                continue
            return None
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            if attempt == 0:
                time.sleep(0.05)
                continue
            return None
        return obj if isinstance(obj, dict) else None
    return None


def tail_lines(path: Path, limit: int) -> List[str]:
    """Last `limit` lines, read backwards so a long feed stays cheap."""
    if limit <= 0:
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    chunk, buf, pos = 65536, b"", size
    try:
        with open(path, "rb") as f:
            while pos > 0 and buf.count(b"\n") <= limit:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
    except OSError:
        return []
    lines = buf.split(b"\n")
    if pos > 0:
        lines = lines[1:]  # first line is a fragment
    out = []
    for raw in lines:
        if raw.strip():
            out.append(raw.decode("utf-8", "replace"))
    return out[-limit:]


def parse_event(line: str, bot: str) -> Optional[dict]:
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    rec.setdefault("bot", bot)
    rec.setdefault("kind", "event")
    rec["t"] = parse_ts(rec.get("ts")) or 0.0
    # Stable-enough id so the browser can dedupe across a reconnect without the
    # server having to remember what it already sent.
    rec["uid"] = "%s:%s:%08x" % (rec.get("bot"), rec.get("ts", ""),
                                 zlib.crc32(line.encode("utf-8", "replace")))
    return rec


# ------------------------------------------------------------------ discovery

def discover() -> List[str]:
    """
    Every bot we should render: the declared roster, plus anything that has
    written a snapshot. Never capped at four, never hides an unknown bot.
    """
    keys = [b["key"] for b in BOTS]
    try:
        found = sorted(STATE_DIR.glob("*.json"))
    except OSError:
        found = []
    for path in found:
        k = path.stem
        if k in keys or not SAFE_BOT.match(k):
            continue
        # Bots write other things into the state directory too: lvl keeps its
        # own ledger in lvl_book.json. A snapshot is identified by the "bot"
        # field core/botstate.py always writes, or by having an event feed
        # beside it. Globbing *.json alone renders a ledger as a phantom bot.
        rec = read_json(path)
        if not isinstance(rec, dict):
            continue
        if rec.get("bot") or (STATE_DIR / ("%s.events.jsonl" % k)).is_file():
            keys.append(k)
    return keys


def accent_for(key: str, index: int) -> str:
    meta = BOT_META.get(key)
    if meta:
        return meta["accent"]
    return SERIES[index % len(SERIES)]


# --------------------------------------------------- observed equity samples
# Points seen while this process has been up. In memory only: the dashboard is
# a viewer and must never write into state/. They exist so the equity chart
# moves during a session even for a bot whose event feed carries no equity.

_SAMPLES: Dict[str, deque] = {}
_SAMPLE_LOCK = threading.Lock()


def _sample(key: str, t: float, equity: float) -> None:
    if not t:
        return
    with _SAMPLE_LOCK:
        dq = _SAMPLES.get(key)
        if dq is None:
            dq = _SAMPLES[key] = deque(maxlen=4000)
        if dq:
            lt, le = dq[-1]
            if equity == le or t <= lt:
                return
        dq.append((t, equity))


def _samples_for(key: str) -> List[Tuple[float, float]]:
    with _SAMPLE_LOCK:
        return list(_SAMPLES.get(key, ()))


# ---------------------------------------------------------------- bot records

def _blank_record(key: str, index: int, message: str = "") -> dict:
    meta = BOT_META.get(key, {})
    return {
        "key": key, "agent": meta.get("agent", ""),
        "label": meta.get("label") or key,
        "strategy": meta.get("strategy") or "", "strategy_live": "",
        "broker": meta.get("broker") or "unknown",
        "fills": meta.get("fills") or "broker",
        "jev_mode": meta.get("jev_mode", "none"),
        "accent": accent_for(key, index), "expected": key in BOT_META,
        "present": False, "status": "absent", "raw_status": "",
        "stale": False, "updated_at": "", "age_s": None,
        "equity": None, "start_equity": None, "cash": None,
        "buying_power": None, "day_pnl": None, "total_pnl": None,
        "return_pct": None, "open_positions": [], "open_positions_count": 0,
        "candidates": [], "trades_today": None, "trades_total": None,
        "win_rate": None, "decisions_total": None, "api_errors": None,
        "next_wake": "", "message": message, "host": "",
    }


def bot_record(key: str, index: int, now: float) -> dict:
    """
    One bot, merged from its roster entry and whatever is on disk. The shape is
    identical whether or not the bot exists, so the UI has no special cases
    beyond the `present` flag.
    """
    snap = read_json(STATE_DIR / ("%s.json" % key))
    if snap is None:
        return _blank_record(key, index)

    meta = BOT_META.get(key, {})
    rec = _blank_record(key, index)

    broker = snap.get("broker") or meta.get("broker") or "unknown"
    # A bot is on simulated fills if we said so, or if it says so itself. The
    # second half matters: `lvl` reports broker "databento-simulated", and a
    # viewer must never present that as broker-confirmed paper trading.
    fills = meta.get("fills") or "broker"
    if re.search(r"simulat|backtest|synthetic", str(broker), re.I):
        fills = "simulated"

    updated_at = snap.get("updated_at") or ""
    t_up = parse_ts(updated_at)
    age = (now - t_up) if t_up else None
    raw_status = str(snap.get("status") or "").strip().lower()
    stale = bool(age is not None and age > STALE_AFTER_S)

    if raw_status in ("error", "halted"):
        status = raw_status
    elif stale:
        status = "stale"
    else:
        status = raw_status or "unknown"

    equity = _num(snap.get("equity"))
    if equity is not None and t_up:
        _sample(key, t_up, equity)

    positions = snap.get("open_positions")
    positions = positions if isinstance(positions, list) else []
    candidates = snap.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []

    rec.update({
        "label": snap.get("persona") or meta.get("label") or key,
        # The roster line is the human description; strategy_live is what the
        # running code says it is doing. Keep both, they can disagree.
        "strategy_live": snap.get("strategy") or "",
        "broker": broker,
        "fills": fills,
        "present": True,
        "status": status,
        "raw_status": raw_status,
        "stale": stale,
        "updated_at": updated_at,
        "age_s": round(age, 1) if age is not None else None,
        "equity": equity,
        "start_equity": _num(snap.get("start_equity")),
        "cash": _num(snap.get("cash")),
        "buying_power": _num(snap.get("buying_power")),
        "day_pnl": _num(snap.get("day_pnl")),
        "total_pnl": _num(snap.get("total_pnl")),
        "return_pct": _num(snap.get("return_pct")),
        "open_positions": positions,
        "open_positions_count": len(positions),
        "candidates": candidates,
        "trades_today": _num(snap.get("trades_today")),
        "trades_total": _num(snap.get("trades_total")),
        "win_rate": _num(snap.get("win_rate")),
        "decisions_total": _num(snap.get("decisions_total")),
        "api_errors": _num(snap.get("api_errors")),
        "next_wake": snap.get("next_wake") or "",
        "message": snap.get("message") or "",
        "host": snap.get("host") or "",
    })
    return rec


def build_state() -> dict:
    now = time.time()
    bots = []
    for i, key in enumerate(discover()):
        try:
            bots.append(bot_record(key, i, now))
        except Exception:
            # One unreadable bot cannot take the whole board down.
            bots.append(_blank_record(key, i, "state file unreadable"))

    live = [b for b in bots if b["present"]]
    ranked = sorted(
        live,
        key=lambda b: (b["return_pct"] is None, -(b["return_pct"] or 0.0), b["key"]),
    )
    leaderboard = [{
        "rank": i + 1,
        "key": b["key"],
        "label": b["label"],
        "accent": b["accent"],
        "return_pct": b["return_pct"],
        "equity": b["equity"],
        "day_pnl": b["day_pnl"],
        "total_pnl": b["total_pnl"],
        "trades_total": b["trades_total"],
        "status": b["status"],
        "fills": b["fills"],
    } for i, b in enumerate(ranked)]

    def total(field: str) -> Optional[float]:
        vals = [b[field] for b in live if b[field] is not None]
        return round(sum(vals), 2) if vals else None

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "t": now,
        "state_dir": str(STATE_DIR),
        "bots": bots,
        "leaderboard": leaderboard,
        "totals": {
            "bots_expected": len(BOTS),
            "bots_present": len(live),
            "bots_running": sum(1 for b in live if b["status"] == "running"),
            "equity": total("equity"),
            "start_equity": total("start_equity"),
            "day_pnl": total("day_pnl"),
            "total_pnl": total("total_pnl"),
            "open_positions": sum(b["open_positions_count"] for b in live),
            "trades_total": total("trades_total"),
            "decisions_total": total("decisions_total"),
            "api_errors": total("api_errors"),
        },
    }


# --------------------------------------------------------------------- events

def read_events(limit: int = 200, bot: Optional[str] = None) -> List[dict]:
    keys = [bot] if bot else discover()
    rows: List[dict] = []
    for key in keys:
        if not SAFE_BOT.match(key or ""):
            continue
        for line in tail_lines(STATE_DIR / ("%s.events.jsonl" % key), limit):
            rec = parse_event(line, key)
            if rec is not None:
                rows.append(rec)
    rows.sort(key=lambda r: (r.get("t") or 0.0, r.get("bot") or ""))
    return rows[-limit:]


def build_history(key: str, index: int = 0) -> dict:
    """
    An equity curve for one bot, replayed from its event feed.

    The feed is the only per-bot history on disk, so we walk it in order and
    move a running equity whenever an event says what equity became (`equity`)
    or what it changed by (`pnl` and friends). A bot whose feed carries neither
    still gets an honest two-point line: start equity at its first event, and
    current equity from the snapshot. Points observed while this server has
    been up are merged in so the chart moves during a session.

    Every point is tagged with where it came from, because a replayed point and
    a snapshot point are not the same claim. Nothing is interpolated: inventing
    points on an equity curve is how a flat line becomes a story.
    """
    snap = read_json(STATE_DIR / ("%s.json" % key)) or {}
    meta = BOT_META.get(key, {})
    start_equity = _num(snap.get("start_equity"))
    equity = start_equity
    points: List[dict] = []

    def add(t: Optional[float], ts: str, value: Optional[float], source: str) -> None:
        if not t or value is None:
            return
        points.append({"t": t, "ts": ts, "equity": round(value, 4), "source": source})

    events = []
    if SAFE_BOT.match(key or ""):
        for line in tail_lines(STATE_DIR / ("%s.events.jsonl" % key), 20000):
            rec = parse_event(line, key)
            if rec is not None:
                events.append(rec)
    events.sort(key=lambda r: r.get("t") or 0.0)

    if events and start_equity is not None:
        add(events[0].get("t"), events[0].get("ts", ""), start_equity, "start")

    for ev in events:
        marked = None
        for f in EQUITY_FIELDS:
            v = _num(ev.get(f))
            if v is not None:
                equity = v
                marked = v
                break
        if marked is None and equity is not None:
            for f in PNL_FIELDS:
                d = _num(ev.get(f))
                if d is not None:
                    equity += d
                    marked = equity
                    break
        if marked is not None:
            add(ev.get("t"), ev.get("ts", ""), marked, "replay")

    for t, eq in _samples_for(key):
        add(t, time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)), eq, "observed")

    add(parse_ts(snap.get("updated_at")), snap.get("updated_at") or "",
        _num(snap.get("equity")), "snapshot")

    # One point per second, last writer wins, chronological.
    dedup: Dict[int, dict] = {}
    for p in sorted(points, key=lambda p: p["t"]):
        dedup[int(p["t"])] = p
    series = [dedup[k] for k in sorted(dedup)]

    return {
        "bot": key,
        "label": snap.get("persona") or meta.get("label") or key,
        "accent": accent_for(key, index),
        "start_equity": start_equity,
        "present": bool(snap),
        "points": series,
        "count": len(series),
    }


def build_all_history() -> dict:
    return {"bots": {k: build_history(k, i) for i, k in enumerate(discover())}}


# --------------------------------------------------------------- static files

INDEX_MISSING = b"""<!doctype html><meta charset="utf-8">
<title>Bot desk</title>
<style>body{margin:0;background:#f6f6f4;color:#15161a;font:16px/1.6 -apple-system,
BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}.wrap{max-width:720px;
margin:0 auto;padding:64px 24px}h1{font-size:28px;letter-spacing:-.6px;margin:0 0 10px}
code{font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}a{color:#2b6d92}
.note{background:#fff8e6;border-left:3px solid #d4a13c;padding:12px 15px;border-radius:7px}
</style>
<div class="wrap">
<h1>The API is up. The page is missing.</h1>
<p class="note"><code>dashboard/index.html</code> is not on disk. It is a plain
file checked into the repo with no build step, so this means an incomplete copy
rather than something you still have to compile.</p>
<p>The JSON is live either way:
<a href="/api/state">/api/state</a> &middot;
<a href="/api/events?limit=20">/api/events</a> &middot;
<a href="/api/history">/api/history</a></p>
</div>
"""


def remote_status() -> dict:
    """
    Whether the other desk is answering. Cached for REMOTE_CACHE_S so a room full
    of open tabs cannot turn a status light into a load test on the VM.

    Never raises and never blocks for long: an unreachable remote is a normal
    state to display, not an error, so every failure becomes a reason string.
    """
    if not REMOTE_URL:
        return {"configured": False}
    now = time.time()
    with _remote_lock:
        if _remote_cache["data"] and now - _remote_cache["t"] < REMOTE_CACHE_S:
            return _remote_cache["data"]

    out = {"configured": True, "url": REMOTE_URL, "label": REMOTE_LABEL, "ok": False}
    started = time.time()
    try:
        req = urllib.request.Request(REMOTE_URL + "/healthz",
                                     headers={"User-Agent": "bot-desk-status"})
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT_S) as r:
            body = json.loads(r.read(65536).decode("utf-8", "replace"))
        out["ok"] = bool(body.get("ok"))
        out["bots"] = body.get("bots") or []
        out["latency_ms"] = round((time.time() - started) * 1000, 1)
    except urllib.error.HTTPError as e:
        out["error"] = "HTTP %s" % e.code
    except urllib.error.URLError as e:
        out["error"] = str(getattr(e, "reason", e))[:120]
    except Exception as e:                                   # timeouts, bad JSON
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:100])

    # How many of its bots are live, but only if it is answering at all. A second
    # call, so a slow state build cannot make a healthy box look down.
    if out["ok"]:
        try:
            req = urllib.request.Request(REMOTE_URL + "/api/state",
                                         headers={"User-Agent": "bot-desk-status"})
            with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT_S) as r:
                st = json.loads(r.read(1 << 20).decode("utf-8", "replace"))
            tot = st.get("totals") or {}
            out["bots_running"] = tot.get("bots_running")
            out["bots_expected"] = tot.get("bots_expected")
            out["equity"] = tot.get("equity")
        except Exception:
            pass                                             # the light stays green

    out["checked_at"] = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    with _remote_lock:
        _remote_cache["t"] = now
        _remote_cache["data"] = out
    return out


# Served alongside the page. Anything not on this list is not reachable, so a
# stray .env or a state file can never be fetched over HTTP even though the
# state directory sits one level up from here.
SERVABLE = {".html", ".css", ".js", ".mjs", ".svg", ".png", ".jpg", ".jpeg",
            ".gif", ".webp", ".ico", ".woff", ".woff2", ".map", ".json", ".txt"}


def resolve_static(url_path: str) -> Optional[Path]:
    """Map a URL onto the dashboard directory, refusing anything that escapes it."""
    rel = unquote(url_path).lstrip("/")
    if not rel or rel.endswith("/"):
        rel += "index.html"
    try:
        target = (WEB_ROOT / rel).resolve()
        root = WEB_ROOT.resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    if target.suffix.lower() not in SERVABLE:
        return None
    return target if target.is_file() else None


# -------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "botdesk/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("DASHBOARD_ACCESS_LOG") or os.environ.get("DASHBOARD_VERBOSE"):
            super().log_message(fmt, *args)

    def _cors(self) -> None:
        # Permissive on purpose: the web app may be served from anywhere (a Vite
        # dev server, a Pages deploy) against this read-only API.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, code: int, body: bytes, ctype: str,
              cache: str = "no-store", head_only: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._cors()
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200, head_only: bool = False) -> None:
        body = json.dumps(obj, default=str, allow_nan=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", head_only=head_only)

    def _query(self) -> Dict[str, List[str]]:
        return parse_qs(urlparse(self.path).query)

    def _bot_param(self, q: Dict[str, List[str]]) -> Optional[str]:
        raw = (q.get("bot") or [""])[0].strip()
        if not raw:
            return None
        return raw if SAFE_BOT.match(raw) else None

    def _limit_param(self, q: Dict[str, List[str]], default: int, cap: int) -> int:
        try:
            n = int((q.get("limit") or [default])[0])
        except (TypeError, ValueError):
            return default
        return max(1, min(cap, n))

    # -- verbs ------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._route(head_only=True)

    def do_GET(self) -> None:
        self._route(head_only=False)

    def _route(self, head_only: bool) -> None:
        path = urlparse(self.path).path
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        try:
            if path == "/api/state":
                self._json(build_state(), head_only=head_only)
            elif path == "/api/events":
                q = self._query()
                limit = self._limit_param(q, 200, 5000)
                bot = self._bot_param(q)
                rows = read_events(limit, bot)
                self._json({"events": rows, "count": len(rows),
                            "limit": limit, "bot": bot}, head_only=head_only)
            elif path == "/api/history":
                bot = self._bot_param(self._query())
                if bot:
                    keys = discover()
                    idx = keys.index(bot) if bot in keys else 0
                    self._json(build_history(bot, idx), head_only=head_only)
                else:
                    self._json(build_all_history(), head_only=head_only)
            elif path == "/api/remote":
                self._json(remote_status(), head_only=head_only)
            elif path == "/api/stream":
                if head_only:
                    self._send(200, b"", "text/event-stream", head_only=True)
                else:
                    self._stream()
            elif path in ("/api/health", "/healthz"):
                self._json({"ok": True, "state_dir": str(STATE_DIR),
                            "state_dir_exists": STATE_DIR.is_dir(),
                            "page": str(WEB_ROOT / "index.html"),
                            "page_present": (WEB_ROOT / "index.html").is_file(),
                            "bots": discover()}, head_only=head_only)
            elif path.startswith("/api/"):
                self._json({"error": "no such endpoint", "path": path},
                           code=404, head_only=head_only)
            else:
                self._static(path, head_only)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser navigated away mid-response
        except Exception:
            traceback.print_exc()
            try:
                self._json({"error": "internal"}, code=500, head_only=head_only)
            except Exception:
                pass

    def _static(self, path: str, head_only: bool) -> None:
        target = resolve_static(path)
        index = WEB_ROOT / "index.html"
        if target is None and not index.is_file():
            self._send(200, INDEX_MISSING, "text/html; charset=utf-8",
                       head_only=head_only)
            return
        if target is None:
            # A path that looks like a file and is not one is a 404. Only
            # extension-less paths fall back to the page, so a mistyped asset
            # URL fails visibly instead of quietly receiving HTML.
            if "." in path.rsplit("/", 1)[-1]:
                self._send(404, b"not found", "text/plain; charset=utf-8",
                           head_only=head_only)
                return
            target = index
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8",
                       head_only=head_only)
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json",
                                                  "image/svg+xml"):
            ctype += "; charset=utf-8"
        # The page is edited in place and reloaded, so it must never be cached
        # or an edit ships stale JS to a browser that thinks it is current.
        cache = "no-store"
        self._send(200, body, ctype, cache=cache, head_only=head_only)

    # -- SSE --------------------------------------------------------------

    def _stream(self) -> None:
        """
        One never-ending HTTP response. A `state` frame every STREAM_INTERVAL_S,
        an `events` frame whenever a feed grows.

        `Connection: close` with no Content-Length is the legal HTTP/1.1 way to
        say "the body ends when the socket does", which is exactly the SSE shape
        and saves hand-rolling chunked encoding.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")  # tell nginx not to buffer
        self._cors()
        self.end_headers()
        self.close_connection = True

        def frame(kind: str, payload: Any) -> bool:
            try:
                blob = json.dumps(payload, default=str, allow_nan=False)
                self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, blob))
                                 .encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                return False

        try:  # ask the browser to come back fast if the socket drops
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
        except OSError:
            return

        # Offsets start at end-of-file: the backlog goes out once, in the
        # opening frame, and after that only genuinely new lines are pushed.
        offsets: Dict[str, int] = {}
        for key in discover():
            try:
                offsets[key] = (STATE_DIR / ("%s.events.jsonl" % key)).stat().st_size
            except OSError:
                offsets[key] = 0

        if not frame("state", build_state()):
            return
        if not frame("events", {"events": read_events(120), "backlog": True}):
            return

        while True:
            deadline = time.time() + STREAM_INTERVAL_S
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                time.sleep(min(0.25, left))

            fresh: List[dict] = []
            for key in discover():
                p = STATE_DIR / ("%s.events.jsonl" % key)
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                start = offsets.get(key)
                if start is None:
                    offsets[key] = size  # a bot that just appeared
                    continue
                if size < start:
                    start = offsets[key] = 0  # feed truncated or rotated
                if size == start:
                    continue
                try:
                    with open(p, "rb") as f:
                        f.seek(start)
                        blob = f.read(size - start)
                except OSError:
                    continue
                cut = blob.rfind(b"\n")  # keep a partial line for next pass
                if cut == -1:
                    continue
                offsets[key] = start + cut + 1
                for raw in blob[:cut].split(b"\n"):
                    line = raw.decode("utf-8", "replace")
                    if not line.strip():
                        continue
                    rec = parse_event(line, key)
                    if rec is not None:
                        fresh.append(rec)

            if fresh:
                fresh.sort(key=lambda r: (r.get("t") or 0.0, r.get("bot") or ""))
                if not frame("events", {"events": fresh[-500:], "backlog": False}):
                    return
            if not frame("state", build_state()):
                return


def main() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # a read-only mount is fine; we only ever read
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")
    mimetypes.add_type("text/css", ".css")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True  # an open SSE stream must not block shutdown
    built = (WEB_ROOT / "index.html").is_file()
    print("bot desk on http://%s:%d" % (HOST, PORT), flush=True)
    print("  state    %s" % STATE_DIR, flush=True)
    print("  page     %s%s" % (WEB_ROOT / "index.html",
                                  "" if built else "   (MISSING)"), flush=True)
    if REMOTE_URL:
        print("  watching %s  (%s)" % (REMOTE_URL, REMOTE_LABEL), flush=True)
    print("  bots     %s" % ", ".join(discover()), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
