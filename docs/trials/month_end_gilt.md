# Trial 5: `month_end_gilt`: FAIL

| | |
|---|---|
| Registered | 2026-10-24, before any backtest |
| Tested | 2026-10-24, once, from a clean commit, with a data manifest |
| Verdict | **FAIL**: the month-end effect is real but costs consume all of it, and it is not stable across the sample |
| Priors, stated before testing | criterion 1 ~12%; all criteria ~10% |

## Hypothesis

Holding a UK gilt ETF (iShares Core UK Gilts, `IGLT`) only over the **last
three London trading days of each month**, and cash otherwise, earns a
positive mean monthly return in excess of the overnight rate (SONIA) after
costs. It also has to beat the same three-day exposure placed on random days
earlier in the month.

The mechanism is index extension: insurers and passive bond funds buy at the
month-end rebalance, when the index duration lengthens. Hartley and Schwarz
(2019) report the effect for US Treasuries. **No gilt-specific test had been
published**, and the pre-registration said so.

The pre-registration also said what would *not* count as support. Lower
drawdown and volatility follow mechanically from being in the market 3
sessions in 21. A positive mean that does not beat the random-days placebo
could just be owning duration. And the most likely way the trial ends was
stated up front: a real gross effect that costs consume.

## Rule

- Buy `IGLT` with the whole sleeve at the close of the 4th-to-last London
  trading day of the month. Sell at the close of the last trading day. The
  position holds the returns of the last three sessions.
- Otherwise the sleeve is in cash earning SONIA. No leverage, no shorting.
- Evaluation window **2009-01-02 to 2026-09-30**, fixed: 213 calendar months.
  2008 is excluded because every `IGLT` bar that year has zero volume.
- Returns are total returns built from the raw close plus the official
  distributions, not a vendor's adjusted close.
- No fitted parameters. Costs are **14 bp per month** as a round trip (6 bp on
  each gilt-ETF side, 1 bp on each cash-leg side), with 28 bp reported as a
  stress case.
- A month whose entry or exit session has no bar is skipped and counted.

**Control:** cash (SONIA) held continuously, so the null is that the mean
monthly net excess is zero or negative. The usual "beat buy-and-hold SPY"
test was replaced on purpose, because a strategy in cash about 85% of the time
cannot fairly be compared with an asset it never holds. The timing control is
a **placebo**: 1,000 series that hold the gilt ETF for three consecutive
sessions on a random day outside the last five sessions of the month, with
identical costs.

## Criteria (all four must pass, fixed before the run)

1. Mean monthly net excess return > 0 **and** Newey-West t > **2.33**. That is
   the one-sided 1% level, a Bonferroni correction of 5% across the five
   trials so far.
2. The mean beats the **95th percentile of the 1,000 placebo series**.
3. The mean is positive in **both** sub-periods (2009-2014 and 2015-2026).
4. Leakage gate: 10 shuffled histories, fail if t > 2.

The deflated Sharpe was **reported, not deciding**, and the pre-registration
said why. An asset held 3 sessions in 21 has a Sharpe that scales with the
square root of its exposure, so it is not comparable with the earlier,
fully invested trials. The cross-trial spread also reflects different
strategy types, not selection noise. Multiple testing was moved into
criterion 1 instead. That was a loosening, decided before any data and
applied to this trial only.

## Result

| # | Criterion | Value | Verdict |
|---|---|---|---|
| 1 | Mean monthly net excess, Newey-West t | **−0.5 bp**, t = −0.09 | **FAIL** |
| 2 | Mean above the placebo 95th percentile | −0.5 bp vs −9.3 bp; ranks first of 1,001 | PASS |
| 3 | Positive in both sub-periods | +4.4 bp (71 months); **−3.0 bp** (141 months) | **FAIL** |
| 4 | Leakage gate | t = −8.18 | PASS |

- **The effect is there, and it is the size of the cost.** Gross of costs the
  mean excess is **+13.5 bp** a month. The assumed round trip is 14 bp, so the
  net is −0.5 bp. The break-even cost is 13.5 bp.
- **Timing does matter.** The month-end windows beat all 1,000 placebo series
  (empirical p = 0.001). The placebo averages about −3.7 bp gross. That
  passes criterion 2 and is still not support: a window that is better than
  random days but worth nothing after costs is not tradeable.
- **It does not hold up over time.** +4.4 bp in 2009-2014, −3.0 bp in 2015-2026
  and −5.4 bp in 2020-2026.
- **Other series agree.** On the official fund NAV total return the net mean is
  +0.6 bp (t 0.12), and on the FTSE gilt index +1.0 bp (t 0.19). At the 28 bp
  stress cost the mean is −14.5 bp (t −2.50).
- **A few windows carry the gross number.** The best five windows are 2022-09
  (+870 bp), 2021-11, 2021-10, 2016-07 and 2024-01. Without them the mean is
  −7.9 bp.
- **In money:** about −0.07% a year, or roughly £3, £7 and £13 a year on £5,000,
  £10,000 and £20,000, before tax and fees.

The pre-registration had expected this: a FAIL "mostly means too small to
trade at retail spreads". It is not evidence that no month-end effect exists.

**Two things the pre-registration got wrong, found and disclosed, neither
changing the verdict:**

- It expected **zero skipped months**. One was skipped: in May 2012 the spring
  bank holiday was moved for the Diamond Jubilee, so a Monday that looked like
  a holiday was a normal session and the entry bar was missing from the data
  source. The registered rule was applied as written, so 212 months were
  scored, not 213.
- A **bad price print** on 2009-12-24 (+3.57% on the day, while the fund's
  official NAV moved −0.10%) is the entry bar of the worst window (−355 bp).
  It was found after the run, in review. It was **not corrected and the trial
  was not re-run**. Excluding that month the mean is about +1.1 bp, and the NAV
  and index versions, which do not use the bar, fail the same two criteria.
  The bar is now flagged in the data checks, and any later use of the series
  must handle it explicitly or the code raises.

An independent validator agent reproduced the deciding numbers from the raw
inputs with its own code, written from the frozen pre-registration. It also
noted that criterion 4 is a vacuous pass for a calendar-only rule, since the
shuffled strategy sits in cash most of the time.

**Corrected-harness note (2026-10).** This trial ran after the harness fix.
The risk-enforced Sharpe in the registry, **0.0743**, includes one halt caused
by the bad print above. The unenforced harness Sharpe is 0.0786. Reported
deflated Sharpe: 0.0043 and 0.0069 against 0.95. It decided nothing.

## What it taught

- **A real effect is not a tradeable one.** The gross effect and the placebo
  both said "something is here". Costs of 14 bp and a break-even of 13.5 bp
  said "not enough". Writing the cost arithmetic into the pre-registration
  meant the result was read against that number, not argued after the fact.
- **State the likely failure before the run.** The pre-registration named
  "costs consume the effect" as the most probable outcome and gave power
  figures: a Treasury-sized effect would pass about 3 times in 10. The FAIL
  was therefore informative, not a surprise.
- **Passing the placebo is not passing.** Criterion 2 passed with the best
  possible rank. It is the right control for "is month-end special?" and the
  wrong one for "does it pay?". Both questions were asked, separately.
- **Say it when your own expectation is wrong.** "Expected skipped months: 0"
  was in the frozen text and was false. The text was left alone and the
  correction was written in the Results, stated first.
- **Do not repair the data to improve the result.** The bad print would have
  been easy to fix and the fixed number is better. It is reported as a
  disclosed secondary figure, which cannot rescue a FAIL.
- **Name the rescues so they are not tried.** A lower cost, dropping the
  cash-leg cost, fixing the 2009-12 bar, another entry day or hold length, a
  different sub-period split, or letting the placebo alone decide would each
  be a new trial.
