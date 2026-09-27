---
name: trading-desk-dashboard
description: The live bot dashboard - a stdlib Python server plus one HTML file, server-sent events, and animated backtest curves for bots that have not started yet. Use when running, changing or debugging the desk.
version: 1.0.0
tags: [trading, dashboard, sse, visualisation]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

# The desk

Two files. `dashboard/server.py` is standard library only, and
`dashboard/index.html` is the entire front end with no build step and no external
requests. Start it with:

```bash
python3 dashboard/server.py          # http://localhost:8080
./dashboard/run_local.sh             # same, plus an indicator for a deployed desk
```

## It only reads

The desk never places an order and never writes to the state directory. When
deployed it mounts that directory **read-only**, so a bug in the dashboard cannot
corrupt what the bots are writing.

## Endpoints

| Endpoint | Returns |
| --- | --- |
| `/api/state` | every bot snapshot, a leaderboard, totals |
| `/api/events?limit&bot` | recent rows from the event feeds |
| `/api/history?bot` | equity curves replayed from those feeds |
| `/api/stream` | server-sent events: a state frame every 2s plus new events |
| `/api/remote` | whether another desk (your VM) is answering |
| `/healthz` | liveness, for the container healthcheck |

Server-sent events rather than websockets: SSE is an HTTP response that never
ends, so it is a few dozen lines on `http.server`, it survives proxies, and the
browser reconnects on its own with no client library. Data flows one way, so a
socket would be handshake machinery for nothing. **Nothing about live updating
requires a framework**: `EventSource` is a browser feature.

## The roster is declared, not discovered

Edit `dashboard/bots.json`. A bot that has never run still gets a card saying
"not yet running" rather than being invisible, which is what you want when you
are waiting for an open. Bots found on disk but not in the roster appear too, so
adding one needs no code change.

`key` must match what your bot passes to `BotState(bot=...)`.

## Waiting cards animate the backtest

While a bot has no live numbers, its card draws its backtested curve with the
percentage counting along with it, looping. Configure the sources in
`dashboard/backtest_sources.json` and run:

```bash
python3 dashboard/make_backtests.py
```

Rules this enforces, which are the difference between a chart and a claim:

- the card is badged `showing backtest` and labelled `backtest return` with its
  date range, so it is never mistaken for live performance
- nothing is smoothed, interpolated or extended
- thinning always keeps the last point, so a line ends where the data ends
- a `caveat` prints on the card itself, so a result that does not generalise says
  so on its face instead of in a footnote

Two lines on one card is how you show a filter's contribution honestly: same
signals, same period, one filtered and one not.

## Two things to know if you change it

**The state frame lands every two seconds.** Cards are rebuilt only when their own
markup changes, otherwise every animation restarts before it finishes. Keep that
signature check if you touch `renderCards`.

**The feed appends rather than re-rendering**, and drops any event `uid` it has
already shown. `EventSource` reconnects by itself and the server replays its
recent tail on every connect, so without that check a dropped wifi link
duplicates the feed.

## Equity history is replayed, not invented

Curves are rebuilt from the event feeds and every point is tagged with its origin:
`start`, `replay`, `observed`, `snapshot`. Nothing is interpolated. Inventing
points on an equity curve is how a flat line becomes a story.
