# Constitution

Binding rules for this system. Every agent, every contributor and the bot
itself operate under these. Where a rule has a number attached, that number
lives in `config/limits.yaml` and is **enforced in code**, not by good
intentions. This document explains the reasoning; the YAML is the authority.

If this document and `config/limits.yaml` disagree, the YAML wins and the
document is a bug.

---

## I. First principles

1. **No model sits in the live order path.** Claude and every other agent
   operate at development time, and in offline daily analysis. The runtime
   trading loop is deterministic Python that can be unit-tested and replayed.

2. **Risk enforcement is code.** A limit that exists only in prose is not a
   limit. Every rule in section III is implemented in `src/tradingbot/risk/`
   with tests that attempt to breach it.

3. **Authority is asymmetric.** Any agent, monitor or human may *reduce*
   risk, halt a strategy, or flatten the book, at any time, without
   justification. *Increasing* risk requires passing the gates in section IV.

4. **Nothing is trusted until it is measured.** Every recommendation an agent
   makes is logged and later scored against what actually happened. An agent
   whose accuracy is never measured is a superstition.

5. **The benchmark is risk-adjusted, not absolute.** Beating a savings account
   with three times its volatility is not success. The comparison is Sharpe
   against buy-and-hold SPY, after costs.

---

## II. Separation of duties

| Layer | Responsibility | May place orders? |
|---|---|---|
| `data/` | ingestion, storage, point-in-time correctness | no |
| `research/` | backtesting, walk-forward, statistics | no |
| `strategies/` | signals -> target weights (pure functions) | no |
| `portfolio/` | allocation across strategies, vol targeting | no |
| `risk/` | limit enforcement, halt state | **veto only** |
| `execution/` | order placement, reconciliation | yes |
| `monitoring/` | regime and macro analysis, briefings | **halt only** |

Strategies are pure functions of point-in-time data. They perform no I/O,
know nothing about account equity, and cannot size their own positions.

---

## III. Risk rules

Numbers in `config/limits.yaml`. Actions are automatic and immediate.

### Loss limits

| Trigger | Action |
|---|---|
| daily loss exceeds `daily_loss_pct` | halt all trading for the remainder of the day |
| weekly loss exceeds `weekly_loss_pct` | halt for the week; written review required |
| drawdown from peak exceeds `drawdown_warn_pct` | reduce every target weight to `drawdown_warn_exposure`; lifts automatically once the drawdown recovers |
| drawdown from peak exceeds `drawdown_stop_pct` | full stop; project returns to paper trading |
| `max_consecutive_losses` on one strategy | halt that strategy pending review |

**What a halt does.** Loss-limit halts (daily, weekly, drawdown,
consecutive losses) FLATTEN: every position is closed and nothing new is
opened, after the book has reconciled against the broker. Integrity halts (a
reconciliation mismatch, an unreadable state file, a missing order, or a
manual stop) FREEZE: no order of any kind is sent, sells included, because a
book that cannot be trusted cannot be traded -- not even to exit.

Selling a long down toward zero is always allowed unless a FREEZE is active.
Shorting means ENDING below zero, and remains prohibited.

### Position limits

- Risk on any single trade may not exceed `max_risk_per_trade_pct` of equity.
- No single instrument may exceed `max_instrument_pct` of equity.
- Gross exposure may not exceed `max_gross_exposure_pct`. **Leverage is
  prohibited at this stage**; this value is 100.
- No new position whose correlation to the existing book exceeds
  `max_book_correlation`.
- No trade is placed until a holding's weight has drifted more than
  `rebalance_band_pct` from its target. A constant target is not a reason
  to trade; drift past the band is.

### Operational

- Data older than `max_data_staleness_minutes` -> no trading this cycle.
- No trading in the first or last `session_edge_minutes` of a session.
- Realised volatility above `vol_regime_multiple` times the strategy's
  backtest assumption -> no new positions.
- Reconciliation between internal book and broker must pass before the open.
  A mismatch halts trading; it is never auto-corrected.
- Every order is logged with the signal, the inputs, and the git commit that
  produced it.

---

## IV. Promotion gates

A strategy advances one stage at a time. Each gate is pass/fail and is
decided **before** the test is run, not after.

1. **Pre-registration.** Hypothesis, economic rationale, universe, timeframe,
   entry/exit, success threshold and the number of parameter combinations to
   be tried, all written to `research/<strategy>.md` before any backtest runs.
2. **In-sample backtest.** Does the idea work at all.
3. **Walk-forward.** Rolling train/test on unseen data. Most ideas die here.
4. **Statistical validation.** Deflated Sharpe above zero accounting for the
   total number of strategies tested; survives the Model Confidence Set.
5. **Paper trading.** Minimum `min_paper_months` months. This tests the
   plumbing, not the edge.
6. **Live, small.** Only if paper Sharpe is within `paper_live_tolerance` of
   backtest Sharpe, with no unexplained P&L discrepancies and a tested
   kill-switch.

A strategy that fails a gate is dead. It is not tuned and re-run. Every
strategy tested increments the multiple-testing counter in
`research/registry.yaml`, which raises the bar for every later candidate.

---

## V. Human override

- You may halt anything, instantly, with no justification.
- You may **not** resume without a written, timestamped reason recorded in
  `config/overrides.yaml`.
- A kill-switch never resumes automatically. A human resets it by hand, the
  next session at the earliest.
- Overrides may only reduce risk. "The news looks good, size up" is not an
  override; it is a new strategy and must pass section IV.
- **Every override is shadow-tracked.** The system continues to simulate what
  it would have done, and records the counterfactual P&L. The running total
  is reported monthly.

---

## VI. Change control

- No parameter change to a live strategy without re-running walk-forward.
- Deployment is from a git tag, never from a dirty working tree.
- Automated agents may open pull requests. They may never merge them.
- Agents may auto-fix **Tier 1 only**: formatting, import order, lint,
  docstrings, type annotations. Everything else raises a notification.
- Any pull request touching a CODEOWNERS path requires human approval.
- A pull request that changes both a protected implementation and the
  BEHAVIOUR of its tests is rejected automatically. Behaviour is judged by
  comparing parsed syntax trees (`scripts/check_protected_paths.py`), so
  reformatting and docstring edits pass while a changed assertion or a
  deleted test does not. If a fix genuinely requires a test to change, a
  human decides and records that decision by applying the `risk-reviewed`
  label to the pull request. Applying that label IS the review: it asserts
  the diff was read and the tests still assert what they should. An
  automated agent must never apply it.

---

## VII. Prohibitions

- No leverage.
- No shorting until explicitly enabled in `limits.yaml` and gate-tested.
- No trading of instruments absent from `config/universe.yaml`.
- No live credentials in the repository, ever. A key that reaches a commit is
  considered compromised and must be rotated; deleting the commit is not
  sufficient.
- No strategy goes live without passing every gate in section IV.
- No claim about performance is made from in-sample results.
