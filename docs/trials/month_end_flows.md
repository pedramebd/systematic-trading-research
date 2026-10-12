# `month_end_flows`: WITHDRAWN before testing

| | |
|---|---|
| Registered | 2026-10-07 |
| Withdrawn | 2026-10-07, at review of the pre-registration, before any code or data was run |
| Counts toward `n_trials`? | **No.** No strategy code was written, no backtest was run, no strategy returns were seen |

## What it would have tested

The idea was to front-run predictable 60/40 rebalancing flows. When stocks and
bonds drift apart, balanced funds rebalance near month-end, and that flow is
partly predictable. The signal came from published research (the average of a
drift-threshold signal and a month-end calendar signal, unchanged), and it was
to be held as a long-only tilt between SPY and IEF around a static 60/40.

## Why it was withdrawn

**Costs, worked out before any test.** The pre-registration's own arithmetic
used typical volatilities, not the evaluation data. It put turnover at about
**80-110× equity a year**, which at the 6 bp one-way cost is **about 5-6.5% a
year**. The published gross return was about 10% a year, and that was for an
unconstrained futures spread. The ETF version would lose more of it to:

- the long-only cap on the tilt;
- next-open fills instead of the paper's timing;
- a shorter sample.

The prior that all criteria would pass was about 7%. Spending a trial, and
raising the multiple-testing bar for every later candidate, on an idea that
loses most of its gross return to costs on paper was judged not worth it.

## How the record treats it

- The pre-registration is kept, unchanged, with a withdrawal note at the top.
- The registry marks it `withdrawn`. It carries no Sharpe, and the trial count
  did not increase.
- Its control (static 60/40 SPY/IEF) was never built.
- Any later test of this mechanism is a **new** registration that increments
  `n_trials`. It may not reuse this pre-registration as if it had been run.

## What it taught

A pre-registration is also a cheap filter. Writing down turnover and costs
**before** looking at results can kill an idea for the price of an afternoon,
without using up a trial.
