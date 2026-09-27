# Disclaimer

This software is research tooling for studying trading strategies. It is not
financial advice, not an investment product, and not a claim that any strategy in
it makes money.

**Hypothetical results.** Backtested performance is hypothetical. It benefits from
hindsight, does not account for every real cost, and is not a prediction of future
performance. Paper trading results are also hypothetical: fills are simulated by
the broker, and a simulated fill is not proof you would have been filled.

**The examples are examples.** `orb/` and `example_sma/` exist to demonstrate the
harness. Do not trade them.

**Most strategies do not work.** This kit is deliberately built to make that
visible: three arms, out-of-sample splits, a t-stat, and costs reported next to
gross. A negative result is the expected outcome and the correct response is to
stop, not to adjust parameters until the number turns positive.

**Your risk.** You are solely responsible for anything you run, paper or live, and
for any losses. The default configuration targets paper accounts. Pointing it at a
funded account is your decision alone.

**No warranty.** See `LICENSE`.
