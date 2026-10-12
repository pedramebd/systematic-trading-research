"""Attempts to breach every rule in CONSTITUTION.md section III.

A limit that has no test trying to break it is not a limit.
"""

from datetime import datetime, timedelta

import pytest

from tradingbot.config import load_limits
from tradingbot.risk.engine import OrderRequest, RiskEngine, Severity
from tradingbot.risk.state import AccountState, Halt, HaltRegistry, HaltScope, Position


def account(equity=100_000.0, **kwargs):
    defaults = dict(
        equity=equity,
        peak_equity=equity,
        day_start_equity=equity,
        week_start_equity=equity,
        as_of=datetime(2026, 6, 1, 14, 30),
    )
    defaults.update(kwargs)
    return AccountState(**defaults)


@pytest.fixture
def engine():
    return RiskEngine(halts=HaltRegistry())


# ---------- position limits ----------


def test_reasonable_order_is_approved(engine):
    order = OrderRequest("SPY", 30, 500.0, "control", risk_amount=500.0)
    assert engine.check_order(order, account()).approved


def test_instrument_concentration_is_blocked(engine):
    # 250 x 500 = 125% of equity: above the 100% cap for index ETFs. (Was 100
    # shares = 50%, against the old 20% cap.)
    order = OrderRequest("SPY", 250, 500.0, "control", risk_amount=500.0)
    decision = engine.check_order(order, account())
    assert not decision.approved
    assert "max_instrument" in {v.rule for v in decision.blocking}


def test_per_trade_risk_is_blocked(engine):
    order = OrderRequest("SPY", 10, 500.0, "control", risk_amount=5_000.0)
    decision = engine.check_order(order, account())
    assert "max_risk_per_trade" in {v.rule for v in decision.blocking}


def test_leverage_is_blocked(engine):
    state = account(positions={"GLD": Position("GLD", 100, 95_000.0)})
    order = OrderRequest("SPY", 30, 500.0, "control", risk_amount=100.0)
    decision = engine.check_order(order, state)
    assert "max_gross_exposure" in {v.rule for v in decision.blocking}


def test_symbol_outside_universe_is_blocked(engine):
    order = OrderRequest("TSLA", 1, 100.0, "control", risk_amount=10.0)
    assert "universe" in {v.rule for v in engine.check_order(order, account()).blocking}


def test_shorting_is_blocked_by_default(engine):
    order = OrderRequest("SPY", -10, 500.0, "control", risk_amount=100.0)
    assert "shorting" in {v.rule for v in engine.check_order(order, account()).blocking}


# ---------- loss limits ----------


def test_daily_loss_triggers_day_halt(engine):
    limit = load_limits().loss.daily_loss_pct
    state = account(equity=100_000.0 * (1 - limit / 100 - 0.001))
    state = AccountState(
        equity=state.equity,
        peak_equity=100_000.0,
        day_start_equity=100_000.0,
        week_start_equity=100_000.0,
        as_of=datetime(2026, 6, 1),
    )
    violations = engine.check_account(state)
    assert any(v.rule == "daily_loss" for v in violations)
    assert engine.halts.is_halted()


def test_drawdown_stop_triggers_full_halt(engine):
    state = AccountState(
        equity=80_000.0,
        peak_equity=100_000.0,
        day_start_equity=80_000.0,
        week_start_equity=80_000.0,
        as_of=datetime(2026, 6, 1),
    )
    engine.check_account(state)
    assert HaltScope.FULL in engine.halts.active_scopes()


def test_consecutive_losses_halt_only_that_strategy(engine):
    limit = load_limits().loss.max_consecutive_losses
    state = account(consecutive_losses={"spy_vol": limit})
    engine.check_account(state)
    assert engine.halts.is_halted("spy_vol")
    assert not engine.halts.is_halted("other_strategy")


def test_order_rejected_while_halted(engine):
    engine.halts.add(Halt(HaltScope.DAY, "daily loss", datetime(2026, 6, 1)))
    order = OrderRequest("SPY", 10, 500.0, "control", risk_amount=100.0)
    decision = engine.check_order(order, account())
    assert not decision.approved
    assert "halted" in {v.rule for v in decision.blocking}


# ---------- halt state machine ----------


def test_halt_never_lifts_itself():
    halts = HaltRegistry()
    halts.add(Halt(HaltScope.DAY, "daily loss", datetime(2026, 6, 1)))
    assert halts.is_halted()
    assert halts.is_halted()  # still halted on re-check; nothing expires


def test_resume_requires_a_written_reason():
    halts = HaltRegistry()
    halts.add(Halt(HaltScope.DAY, "daily loss", datetime(2026, 6, 1)))
    with pytest.raises(ValueError):
        halts.clear(HaltScope.DAY, "", datetime(2026, 6, 2), by="reviewer")
    with pytest.raises(ValueError):
        halts.clear(HaltScope.DAY, "reviewed", datetime(2026, 6, 2), by="")


def test_resume_is_logged():
    halts = HaltRegistry()
    halts.add(Halt(HaltScope.DAY, "daily loss", datetime(2026, 6, 1)))
    halts.clear(
        HaltScope.DAY, "reviewed the logs, data bug fixed", datetime(2026, 6, 2), by="reviewer"
    )
    assert not halts.is_halted()
    assert len(halts.resume_log) == 1


def test_halt_requires_a_reason():
    with pytest.raises(ValueError):
        Halt(HaltScope.DAY, "   ", datetime(2026, 6, 1))


# ---------- operational ----------


def test_stale_data_blocks_trading(engine):
    limit = load_limits().operational.max_data_staleness_minutes
    now = datetime(2026, 6, 1, 14, 30)
    state = account(as_of=now, data_timestamp=now - timedelta(minutes=limit + 5))
    order = OrderRequest("SPY", 10, 500.0, "control", risk_amount=100.0)
    assert "data_staleness" in {v.rule for v in engine.check_order(order, state).blocking}


def test_paper_only_warning_is_present_but_not_blocking(engine):
    order = OrderRequest("SPY", 30, 500.0, "control", risk_amount=500.0)
    decision = engine.check_order(order, account())
    assert decision.approved
    assert any(v.rule == "paper_only" and v.severity is Severity.WARN for v in decision.violations)


# ---------- config sanity ----------


def test_leverage_is_off_and_gross_is_capped_at_100():
    limits = load_limits()
    assert limits.permissions.allow_leverage is False
    assert limits.position.max_gross_exposure_pct <= 100.0


def test_live_trading_is_disabled():
    assert load_limits().permissions.allow_live_trading is False
