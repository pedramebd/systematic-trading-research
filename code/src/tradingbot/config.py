"""Configuration loading.

The YAML files in config/ are the authority for every risk number in the
system. This module loads them and exposes them as frozen dataclasses so a
typo becomes an error at start-up rather than a silent zero at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


@dataclass(frozen=True)
class LossLimits:
    daily_loss_pct: float
    weekly_loss_pct: float
    drawdown_warn_pct: float
    drawdown_warn_exposure: float
    drawdown_stop_pct: float
    max_consecutive_losses: int


@dataclass(frozen=True)
class PositionLimits:
    max_risk_per_trade_pct: float
    max_instrument_pct: float
    max_gross_exposure_pct: float
    max_book_correlation: float
    rebalance_band_pct: float


@dataclass(frozen=True)
class Operational:
    max_data_staleness_minutes: int
    session_edge_minutes: int
    vol_regime_multiple: float
    require_reconciliation: bool


@dataclass(frozen=True)
class Permissions:
    allow_leverage: bool
    allow_shorting: bool
    allow_live_trading: bool


@dataclass(frozen=True)
class Costs:
    commission_bps: float
    slippage_bps: float
    spread_bps: float

    @property
    def total_bps(self) -> float:
        """One-way cost in basis points."""
        return self.commission_bps + self.slippage_bps + self.spread_bps / 2.0


@dataclass(frozen=True)
class Limits:
    version: int
    loss: LossLimits
    position: PositionLimits
    operational: Operational
    permissions: Permissions
    costs: Costs
    promotion_gates: dict[str, Any]

    def __post_init__(self) -> None:
        # Constitution section III: leverage is prohibited at this stage.
        if self.position.max_gross_exposure_pct > 100.0 and not self.permissions.allow_leverage:
            raise ValueError("max_gross_exposure_pct > 100 requires permissions.allow_leverage")
        if self.loss.drawdown_stop_pct <= self.loss.drawdown_warn_pct:
            raise ValueError("drawdown_stop_pct must exceed drawdown_warn_pct")
        if self.loss.weekly_loss_pct <= self.loss.daily_loss_pct:
            raise ValueError("weekly_loss_pct must exceed daily_loss_pct")
        if not 0.0 <= self.loss.drawdown_warn_exposure < 1.0:
            raise ValueError(
                "drawdown_warn_exposure must be in [0, 1) -- the warn level\n"
                "may only REDUCE exposure, never maintain or raise it"
            )
        if self.position.rebalance_band_pct < 0.0:
            raise ValueError("rebalance_band_pct must not be negative")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


SINGLE_NAME_INSTRUMENT_CAP_PCT = 20.0


def _check_instrument_cap(limits: Limits, universe: dict[str, Any]) -> None:
    """A cap above 20% is only sound for diversified funds.

    SPY and GLD are index ETFs, so letting one of them be most of the book is
    not concentration in the sense the 20% rule guards against. A single
    stock is. If one is ever added to the execution universe while the cap is
    high, refuse to start rather than quietly allow a 100% single-name book.
    """
    if limits.position.max_instrument_pct <= SINGLE_NAME_INSTRUMENT_CAP_PCT:
        return
    offenders = [
        item["symbol"]
        for item in universe.get("execution", [])
        if not str(item.get("asset_class", "")).endswith("_etf")
    ]
    if offenders:
        raise ValueError(
            f"max_instrument_pct is {limits.position.max_instrument_pct} but "
            f"{offenders} are not ETFs. Lower the cap to "
            f"{SINGLE_NAME_INSTRUMENT_CAP_PCT} or remove them from the universe."
        )


@cache
def load_limits(path: Path | None = None) -> Limits:
    limits = _build_limits(path)
    if path is None:
        _check_instrument_cap(limits, load_universe())
    return limits


def _build_limits(path: Path | None) -> Limits:
    raw = _load_yaml(path or CONFIG_DIR / "limits.yaml")
    return Limits(
        version=raw["version"],
        loss=LossLimits(**raw["loss_limits"]),
        position=PositionLimits(**raw["position_limits"]),
        operational=Operational(**raw["operational"]),
        permissions=Permissions(**raw["permissions"]),
        costs=Costs(**raw["costs"]),
        promotion_gates=raw["promotion_gates"],
    )


@cache
def load_universe(path: Path | None = None) -> dict[str, Any]:
    return _load_yaml(path or CONFIG_DIR / "universe.yaml")


def tradeable_symbols(path: Path | None = None) -> frozenset[str]:
    """Symbols the bot is permitted to hold. Signal-only symbols excluded."""
    universe = load_universe(path)
    return frozenset(item["symbol"] for item in universe["execution"])
