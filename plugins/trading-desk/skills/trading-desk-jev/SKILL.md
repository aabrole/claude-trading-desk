---
name: trading-desk-jev
description: Wire a decision model (TypeSafe Jev System One) into the strategy harness as a filter, including the threshold that actually works, caching, and how to test whether it added anything. Use when adding or evaluating model-based trade filtering.
version: 1.0.0
tags: [trading, jev, typesafe, ai-decisions]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

# Model-filtered trades

The model does not hunt for trades. The strategy proposes every entry and the
model answers "take it or stand aside". That is the only arrangement where the
A/B against the control arm means anything, because both arms then see an
identical candidate set.

## The API

```
POST https://api.typesafe.ai/v1/systemone
{ "model": "...", "state": "<the situation in words>", "questions": [...] }
```

Question types: `noul` (free), `choice` (with a `criteria` map), `score` (with a
`criteria` list). The response gives `answers[id].choice`, `.confidence` and
`.probabilities`. `core/decision.py` implements this with stdlib `urllib`, so it
needs no SDK and runs on older Pythons.

Set `TYPESAFE_API_KEY` in `.env`.

## Threshold on the probability, not the confidence

Gate on `probabilities[chosen_action]`, not the `confidence` field.

In one measured case the `confidence` field was significantly correlated with
outcome **in the wrong direction** (-0.047, p=0.022) while the chosen action's
probability ranked trades correctly. Do not assume; measure which one ranks your
outcomes before trusting either.

Start near 0.30, not 0.55. A high threshold looks disciplined and mostly removes
sample size. Sweep it and look at the whole curve.

## Never let the model flip the side

The strategy decided the direction. The model's only powers are approve and
stand aside. A model that can reverse a trade is a different strategy with an
unmeasurable candidate set.

## Record extra questions, act on none of them

Ask for a risk score alongside the decision. One call answers all questions in
parallel, so it is free, and afterwards you can check whether the stated risk
actually predicted outcomes. Never let those answers affect a trade.

## Cache and prefetch

Key the cache on a hash of the exact request and append to JSONL, so a re-run
costs nothing and you can audit every decision later.

Prefetching with a thread pool is legitimate **only when each question is
independent**. If your strategy can hold a position across candidates, the
snapshot depends on prior outcomes and prefetching silently feeds the model the
wrong state. Verify independence before parallelising.

Retries must catch `urllib.error.HTTPError` and then
`(OSError, http.client.HTTPException, json.JSONDecodeError)`. A bare
`RemoteDisconnected` escaping the retry loop will kill a long run near the end.

## How to tell whether it helped

Compare the model arm to the **gated** arm, not to the unfiltered arm. Beating
unfiltered only shows that filtering helps. Then:

- t-test the difference in mean R between the two arms
- check the model arm's ranking power over the **full** candidate set, not just
  the accepted band, or restriction of range will hide it
- report the stand-aside rate: a model that approves 97% is not filtering

A likely finding, worth stating plainly: the model is a **combiner, not a
source**. It can weigh heterogeneous evidence you already computed, and it does
not manufacture an edge that is not in your features. Across six strategies in
the project this kit came from, it never beat hand-written rules by a
statistically significant margin. Publish that kind of result rather than hiding
it.

## Cost

Input is billed, output is free, and the volume here is tiny: a full multi-year
backtest of a few thousand candidates costs a few cents, and a live bot costs a
dollar or two a month.
