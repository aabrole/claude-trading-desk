"""
decision.py
===========
The decision layer, behind one interface so a strategy can be run three ways
without changing a line of the strategy itself.

  RuleDecider   take every candidate the strategy proposes. The naive baseline.
  GateDecider   same candidates, vetoed by hand-written context filters.
  JevDecider    same candidates, scored by Jev on the same context.

The gated arm exists to keep the comparison honest. Any filter that removes
trades will raise a win rate, so "Jev lifted the win rate" means nothing on its
own. The question worth answering is whether Jev beats three if-statements.

Jev contract (POST https://api.typesafe.ai/v1/systemone), verified live:
  request : {"model","state","questions":{id:{"type","instructions","criteria"}}}
  response: {"model","answers":{id:{"type","choice","confidence","probabilities"}},"usage"}
  a "score" question returns the probability-weighted index of its criteria list.

We speak HTTP directly instead of using the official typesafe-sdk, which needs
Python >= 3.10 while pandas/alpaca here live on 3.9.

Every response is cached to cache/jev_cache.jsonl keyed by a hash of the exact
request, so a re-run is free and returns identical decisions. A black-box model
has no business in a backtest you cannot reproduce.
"""

from __future__ import annotations

import hashlib
import http.client
import threading
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from contracts import Action, Decision, ENTRIES, Snapshot

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
DEFAULT_CACHE = Path(__file__).parent / "cache" / "jev_cache.jsonl"

# A gate takes a snapshot and returns a veto reason, or None to allow it.
Gate = Callable[[Snapshot], Optional[str]]


class Decider:
    name = "base"

    def decide(self, snap: Snapshot) -> Decision:
        raise NotImplementedError

    def stats(self) -> Dict:
        return {}


# --------------------------------------------------------------- arm 1

class RuleDecider(Decider):
    """Takes whatever the strategy proposes. This is the strategy 'as advertised'."""
    name = "rules"

    def decide(self, snap: Snapshot) -> Decision:
        return Decision(action=snap.proposed, source="rules")


# --------------------------------------------------------------- arm 2

class GateDecider(Decider):
    """Entry candidates must survive every gate. Exits and holds pass through."""
    name = "gated"

    def __init__(self, gates: List[Gate]):
        self.gates = gates
        self.vetoes: Dict[str, int] = {}
        self.allowed = 0

    def decide(self, snap: Snapshot) -> Decision:
        if snap.proposed not in ENTRIES:
            return Decision(action=snap.proposed, source="gated")

        for gate in self.gates:
            reason = gate(snap)
            if reason:
                self.vetoes[reason] = self.vetoes.get(reason, 0) + 1
                return Decision(action=Action.WAIT, confidence=0.0,
                                source="gated", note=f"veto:{reason}")

        self.allowed += 1
        return Decision(action=snap.proposed, source="gated")

    def stats(self) -> Dict:
        total = self.allowed + sum(self.vetoes.values())
        return {
            "candidates": total,
            "allowed": self.allowed,
            "vetoed": sum(self.vetoes.values()),
            "veto_breakdown": dict(sorted(self.vetoes.items(), key=lambda kv: -kv[1])),
        }


# --------------------------------------------------------------- arm 3

@dataclass
class JevPrompt:
    """
    What to ask Jev. Supplied by the strategy, because only the strategy knows
    what its own candidates mean.

    `extra_questions` are asked alongside the decision and recorded but never
    acted on. They cost nothing extra (one call answers all questions in
    parallel) and let us check afterwards whether Jev's stated risk actually
    predicted the outcome.
    """
    entry_instructions: str
    entry_criteria: Dict[str, str]
    extra_questions: Dict[str, dict] = field(default_factory=dict)
    manage_instructions: Optional[str] = None
    manage_criteria: Optional[Dict[str, str]] = None


class JevDecider(Decider):
    """
    Jev as a second opinion on candidates the strategy already found.

    Jev is not hunting for trades here. The strategy still proposes every entry;
    Jev only answers "take it or stand aside". Both arms therefore see an
    identical candidate set, which is the only way the A/B means anything.
    """
    name = "jev"

    def __init__(
        self,
        prompt: JevPrompt,
        api_key: Optional[str] = None,
        model: str = JEV_MODEL,
        threshold: float = 0.55,
        manage_positions: bool = False,
        cache_path: Path = DEFAULT_CACHE,
        offline: bool = False,
        timeout: float = 15.0,
        retries: int = 4,
        fallback: str = "wait",     # what to do when Jev cannot answer: wait | rule
        log_path: Optional[Path] = None,   # append every decision for the dashboard
    ):
        self.prompt = prompt
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        self.model = model
        self.threshold = threshold
        self.manage_positions = manage_positions
        self.cache_path = Path(cache_path)
        self.offline = offline
        self.timeout = timeout
        self.retries = retries
        self.fallback = fallback
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text("")      # fresh log per run

        self.cache: Dict[str, Dict] = {}
        self._lock = threading.Lock()      # cache file + counters, for prefetch
        self.calls = 0
        self.cache_hits = 0
        self.errors = 0
        self.below_threshold = 0
        self.disagreed = 0
        self.latencies: List[float] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self._load_cache()

    # -- cache -----------------------------------------------------

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        with open(self.cache_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    self.cache[rec["key"]] = rec["response"]
                except (json.JSONDecodeError, KeyError):
                    continue

    def _save(self, key: str, response: Dict) -> None:
        with self._lock:
            self.cache[key] = response
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "a") as f:
                f.write(json.dumps({"key": key, "response": response}) + "\n")

    @staticmethod
    def _key(payload: Dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]

    # -- transport -------------------------------------------------

    def _post(self, payload: Dict) -> Dict:
        req = urllib.request.Request(
            JEV_URL,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def _ask(self, payload: Dict) -> Optional[Dict]:
        key = self._key(payload)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        if self.offline:
            return None
        if not self.api_key:
            raise RuntimeError("TYPESAFE_API_KEY not set (console.typesafe.ai/keys), "
                               "or pass offline=True to run from cache only.")

        for attempt in range(self.retries + 1):
            t0 = time.time()
            try:
                resp = self._post(payload)
            except urllib.error.HTTPError as e:
                # 4xx other than rate-limit will not fix itself; stop retrying.
                code = e.code
                if code != 429 and 400 <= code < 500:
                    detail = ""
                    try:
                        detail = e.read().decode()[:200]
                    except Exception:
                        pass
                    self.errors += 1
                    print(f"[jev] HTTP {code} {detail}")
                    return None
                if attempt == self.retries:
                    self.errors += 1
                    print(f"[jev] giving up after {attempt + 1} tries: HTTP {code}")
                    return None
                time.sleep(0.5 * (2 ** attempt))
                continue
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
                # A long sequential run WILL meet dropped keep-alives, resets and
                # truncated bodies. urllib does not wrap those in URLError, so a
                # narrower net lets them kill the whole backtest mid-flight.
                if attempt == self.retries:
                    self.errors += 1
                    print(f"[jev] giving up after {attempt + 1} tries: "
                          f"{type(e).__name__}: {str(e)[:80]}")
                    return None
                time.sleep(0.5 * (2 ** attempt))
                continue

            usage = resp.get("usage") or {}
            with self._lock:
                self.latencies.append((time.time() - t0) * 1000)
                self.calls += 1
                self.input_tokens += usage.get("input_tokens", 0)
                self.output_tokens += usage.get("output_tokens", 0)
            self._save(key, resp)
            return resp
        return None

    # -- parsing ---------------------------------------------------

    @staticmethod
    def _answers(resp: Dict) -> Dict:
        return resp.get("answers") or {}

    @classmethod
    def _aux(cls, resp: Dict) -> Dict[str, float]:
        """Flatten every non-decision answer into plain numbers we can analyse later."""
        out: Dict[str, float] = {}
        for qid, ans in cls._answers(resp).items():
            if qid == "action" or not isinstance(ans, dict):
                continue
            kind = ans.get("type")
            if kind == "score" and ans.get("score") is not None:
                out[f"jev_{qid}"] = float(ans["score"])
            elif kind == "noul" and ans.get("noul") is not None:
                out[f"jev_{qid}"] = float(ans["noul"])
            elif kind == "choice":
                for opt, p in (ans.get("probabilities") or {}).items():
                    out[f"jev_{qid}_{opt}"] = float(p)
            if ans.get("confidence") is not None:
                out[f"jev_{qid}_conf"] = float(ans["confidence"])
        return out

    # -- decide ----------------------------------------------------

    def _questions_for(self, snap: Snapshot) -> Optional[Dict]:
        if snap.proposed in ENTRIES:
            q = {"action": {"type": "choice",
                            "instructions": self.prompt.entry_instructions,
                            "criteria": self.prompt.entry_criteria}}
            q.update(self.prompt.extra_questions)
            return q
        if snap.proposed == Action.HOLD and self.manage_positions and self.prompt.manage_criteria:
            return {"action": {"type": "choice",
                               "instructions": self.prompt.manage_instructions or "",
                               "criteria": self.prompt.manage_criteria}}
        return None

    def _fallback(self, snap: Snapshot, note: str) -> Decision:
        action = snap.proposed if self.fallback == "rule" else Action.WAIT
        return Decision(action=action, confidence=0.0, source="jev", note=note)

    def decide(self, snap: Snapshot) -> Decision:
        questions = self._questions_for(snap)
        if questions is None:
            return Decision(action=snap.proposed, source="jev", note="not_asked")

        state = "\n".join(snap.context_lines)
        payload = {"model": self.model, "state": state, "questions": questions}
        cached = self._key(payload) in self.cache

        resp = self._ask(payload)
        if resp is None:
            return self._fallback(snap, "no_answer")

        ans = self._answers(resp).get("action")
        if not isinstance(ans, dict) or "choice" not in ans:
            return self._fallback(snap, "unparsed_response")

        probs = {k: float(v) for k, v in (ans.get("probabilities") or {}).items()}
        aux = self._aux(resp)
        conf = float(ans.get("confidence", 0.0))
        latency = 0.0 if cached else (self.latencies[-1] if self.latencies else 0.0)
        raw = ans["choice"]
        chosen = Action(raw) if raw in Action._value2member_map_ else snap.proposed

        def d(action: Action, note: str = "") -> Decision:
            dec = Decision(action=action, probabilities=probs, confidence=conf,
                           source="jev", latency_ms=latency, cached=cached,
                           note=note, aux=aux)
            self._log(snap, state, dec)
            return dec

        if chosen in ENTRIES:
            # Argmax is not enough. A 34/33/33 split has an argmax too.
            if probs.get(chosen.value, 0.0) < self.threshold:
                self.below_threshold += 1
                return d(Action.WAIT, f"below_threshold({probs.get(chosen.value, 0.0):.2f})")
            # Never let Jev flip the side. Fading the strategy is a different
            # strategy, and mixing the two would make the A/B unreadable.
            if chosen != snap.proposed:
                self.disagreed += 1
                return d(Action.WAIT, "disagreed_side")

        return d(chosen)

    def prefetch(self, snapshots, workers: int = 5, verbose: bool = True) -> Dict:
        """
        Ask every question up front, concurrently, then let the backtest read
        answers from cache.

        This is only legitimate because the questions are independent: the
        strategy emits at most one candidate per symbol per session, and Jev is
        asked only about entries, always from a flat position. No answer can
        change another's input, so concurrent and sequential asking produce
        identical results. If a strategy ever asks Jev to manage an open
        position, that stops being true and this must not be used.

        Rate limit is 1,200 requests/minute (20/s). At ~270ms per call each
        worker sustains ~3.7/s, so 5 workers sit just under the cap.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        payloads, seen = [], set()
        for snap in snapshots:
            questions = self._questions_for(snap)
            if questions is None:
                continue
            payload = {"model": self.model,
                       "state": "\n".join(snap.context_lines),
                       "questions": questions}
            key = self._key(payload)
            if key in self.cache or key in seen:
                continue
            seen.add(key)
            payloads.append(payload)

        if not payloads:
            if verbose:
                print(f"[jev] prefetch: all {len(snapshots)} decisions already cached")
            return {"fetched": 0, "cached": len(snapshots)}

        est_tok = len(payloads) * 888
        if verbose:
            print(f"[jev] prefetch: {len(payloads):,} new calls on {workers} workers "
                  f"(~{est_tok/1e6:.2f}M input tokens, ~${est_tok/1e6*0.042:.4f}, "
                  f"~{len(payloads)/(workers*3.7)/60:.1f} min)")

        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(self._ask, p) for p in payloads]
            for f in as_completed(futures):
                f.result()          # surface any escaped exception
                done += 1
                if verbose and done % 250 == 0:
                    rate = done / max(0.001, time.time() - t0)
                    print(f"[jev] prefetch {done:,}/{len(payloads):,} "
                          f"({rate:.1f} req/s, {self.errors} errors)")
        elapsed = time.time() - t0
        if verbose:
            print(f"[jev] prefetch done: {done:,} calls in {elapsed/60:.1f} min "
                  f"({done/max(0.001, elapsed):.1f} req/s), {self.errors} errors")
        return {"fetched": done, "elapsed_s": round(elapsed, 1), "errors": self.errors}

    def _log(self, snap: Snapshot, state: str, dec: Decision) -> None:
        """
        The decision stream: every question asked and every answer, in order.
        This is what the dashboard renders and what you screen-record.
        """
        if not self.log_path:
            return
        rec = {
            "timestamp": snap.timestamp.isoformat(),
            "symbol": snap.symbol,
            "price": snap.price,
            "proposed": snap.proposed.value,
            "state": state,
            "action": dec.action.value,
            "probabilities": dec.probabilities,
            "confidence": dec.confidence,
            "aux": dec.aux,
            "note": dec.note,
            "latency_ms": round(dec.latency_ms, 1),
            "cached": dec.cached,
            "taken": dec.action in ENTRIES,
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def stats(self) -> Dict:
        lat = sorted(self.latencies)

        def pct(p: float) -> float:
            return round(lat[min(int(len(lat) * p), len(lat) - 1)], 1) if lat else 0.0

        return {
            "api_calls": self.calls,
            "cache_hits": self.cache_hits,
            "errors": self.errors,
            "vetoed_below_threshold": self.below_threshold,
            "vetoed_disagreed_side": self.disagreed,
            "latency_ms_p50": pct(0.50),
            "latency_ms_p95": pct(0.95),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


DECIDERS = {"rules": RuleDecider, "gated": GateDecider, "jev": JevDecider}
