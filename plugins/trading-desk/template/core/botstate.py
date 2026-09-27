"""
botstate.py
===========
One uniform state file per bot, so a dashboard can read every strategy the same
way regardless of which broker it trades through.

Each bot writes:
  state/<bot>.json        a full snapshot, overwritten every cycle
  state/<bot>.events.jsonl  an append-only feed of everything that happened

The snapshot is what a dashboard polls; the event feed is what it streams. Both
are plain files, so this works identically on a laptop, in Docker, or synced to
a Cloudflare KV namespace with no extra machinery.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

STATE_DIR = Path(os.environ.get("BOT_STATE_DIR", Path(__file__).resolve().parent.parent / "state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Candidate:
    """Something the bot is watching. Streamed whether or not it is traded."""
    symbol: str
    note: str = ""
    proposed: str = ""
    probability: Optional[float] = None
    probabilities: Dict[str, float] = field(default_factory=dict)
    aux: Dict[str, float] = field(default_factory=dict)
    decision: str = ""
    traded: bool = False
    latency_ms: Optional[float] = None


@dataclass
class Position:
    symbol: str
    qty: float
    side: str
    entry: float
    last: float
    unrealized: float
    opened_at: str = ""


@dataclass
class Snapshot:
    bot: str
    persona: str
    strategy: str
    broker: str
    status: str                       # running | sleeping | halted | error
    updated_at: str
    equity: float
    start_equity: float
    cash: float = 0.0
    buying_power: float = 0.0
    day_pnl: float = 0.0
    total_pnl: float = 0.0
    return_pct: float = 0.0
    open_positions: List[Position] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    trades_today: int = 0
    trades_total: int = 0
    win_rate: Optional[float] = None
    next_wake: str = ""
    message: str = ""
    host: str = field(default_factory=socket.gethostname)
    decisions_total: int = 0
    api_errors: int = 0


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a reader never sees half a file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        # mkstemp creates 0600 and os.replace keeps it, which makes the snapshot
        # unreadable to anything not running as the writing user. The dashboard
        # is a separate container running as nobody, and this file exists for it
        # to read: it carries status and equity, never a credential. Without this
        # the desk reports every healthy bot as "absent".
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


class BotState:
    def __init__(self, bot: str, persona: str, strategy: str, broker: str,
                 start_equity: float, state_dir: Path = STATE_DIR):
        self.bot = bot
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snap_path = self.dir / f"{bot}.json"
        self.event_path = self.dir / f"{bot}.events.jsonl"
        self.snapshot = Snapshot(bot=bot, persona=persona, strategy=strategy, broker=broker,
                                 status="starting", updated_at=self._now(),
                                 equity=start_equity, start_equity=start_equity)

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def update(self, **kw) -> None:
        for k, v in kw.items():
            if hasattr(self.snapshot, k):
                setattr(self.snapshot, k, v)
        s = self.snapshot
        s.total_pnl = s.equity - s.start_equity
        s.return_pct = (s.equity / s.start_equity - 1) * 100 if s.start_equity else 0.0
        s.updated_at = self._now()
        _atomic_write(self.snap_path, json.dumps(asdict(s), indent=1, default=str))

    def event(self, kind: str, **payload) -> None:
        """
        Append one event. `kind` is one of:
          heartbeat | candidate | decision | order | fill | exit | halt | error
        Candidate and decision events are what make the stream live: they fire on
        every scan, not only when a trade happens, which matters because these
        strategies trade a few times a week rather than a few times a minute.
        """
        rec = {"ts": self._now(), "bot": self.bot, "kind": kind, **payload}
        with open(self.event_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        if kind == "decision":
            self.snapshot.decisions_total += 1
        elif kind == "error":
            self.snapshot.api_errors += 1

    def candidates(self, cands: List[Candidate]) -> None:
        self.update(candidates=cands)
        for c in cands:
            self.event("candidate", **asdict(c))


def read_all(state_dir: Path = STATE_DIR) -> Dict[str, dict]:
    out = {}
    for p in sorted(Path(state_dir).glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
    return out


def tail_events(state_dir: Path = STATE_DIR, limit: int = 200) -> List[dict]:
    rows = []
    for p in sorted(Path(state_dir).glob("*.events.jsonl")):
        try:
            lines = p.read_text().splitlines()[-limit:]
        except OSError:
            continue
        for l in lines:
            try:
                rows.append(json.loads(l))
            except json.JSONDecodeError:
                continue
    return sorted(rows, key=lambda r: r.get("ts", ""))[-limit:]
