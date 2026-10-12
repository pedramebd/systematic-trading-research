"""Per-asset-class trading costs for research backtests.

`config/limits.yaml` charges one cost to everything: 6 bp one way, which is
pessimistic for US ETFs and about five times too low for Alpaca crypto (0.25%
taker per side plus an unpublished spread). A backtest that trades crypto at
the ETF rate flatters itself.

This module reads `research/costs.yaml` and turns it into the one input
`backtest.run` understands: `cost_bps`, a one-way cost in basis points per
symbol. It is opt-in. A run that is not passed `cost_bps` charges limits.yaml's
single rate exactly as before.

An asset class may be given as `limits`, meaning "whatever limits.yaml says",
so the ETF number lives in one place. It resolves against the `limits` passed
to `load_research_costs` (default: config/limits.yaml), NOT against the
`limits` later given to `backtest.run` -- a cost-stress run must build its
`cost_bps` from the same stressed `Limits`. A symbol's class comes from
`config/universe.yaml`, extended by `symbols:` in the research file for
instruments the bot may never trade. An unknown symbol or class raises: a
silent default is how a crypto run ends up costed as SPY.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tradingbot.config import REPO_ROOT, Costs, Limits, load_limits, load_universe

RESEARCH_COSTS_PATH = REPO_ROOT / "research" / "costs.yaml"
INHERIT = "limits"


@dataclass(frozen=True)
class ResearchCosts:
    #: asset class -> cost components (same formula as limits.yaml)
    by_class: Mapping[str, Costs]
    #: symbol -> asset class
    asset_class: Mapping[str, str]

    def bps_for(self, symbol: str) -> float:
        """One-way cost in basis points for one symbol."""
        if symbol not in self.asset_class:
            raise KeyError(
                f"{symbol!r} has no asset class; add it to config/universe.yaml "
                f"or to `symbols:` in research/costs.yaml"
            )
        klass = self.asset_class[symbol]
        if klass not in self.by_class:
            raise KeyError(
                f"asset class {klass!r} of {symbol!r} has no cost in research/costs.yaml"
            )
        return self.by_class[klass].total_bps

    def cost_bps(self, symbols: Iterable[str]) -> dict[str, float]:
        """The `cost_bps` mapping `backtest.run` takes."""
        return {symbol: self.bps_for(symbol) for symbol in symbols}


def _universe_classes(universe: Mapping[str, Any]) -> dict[str, str]:
    classes: dict[str, str] = {}
    for section in ("execution", "signal_only"):
        for item in universe.get(section, []) or []:
            classes[str(item["symbol"])] = str(item["asset_class"])
    return classes


def load_research_costs(
    path: Path | None = None,
    limits: Limits | None = None,
    universe: Mapping[str, Any] | None = None,
) -> ResearchCosts:
    raw = yaml.safe_load((path or RESEARCH_COSTS_PATH).read_text(encoding="utf-8"))
    limits = limits or load_limits()
    universe = load_universe() if universe is None else universe

    by_class: dict[str, Costs] = {}
    for klass, spec in (raw.get("asset_classes") or {}).items():
        if spec == INHERIT:
            by_class[klass] = limits.costs
        elif isinstance(spec, Mapping):
            costs = Costs(**spec)
            if min(costs.commission_bps, costs.slippage_bps, costs.spread_bps) < 0:
                raise ValueError(f"asset class {klass!r} has a negative cost component")
            by_class[klass] = costs
        else:
            raise ValueError(f"asset class {klass!r}: expected {INHERIT!r} or a mapping")

    classes = _universe_classes(universe)
    for symbol, klass in (raw.get("symbols") or {}).items():
        symbol, klass = str(symbol), str(klass)
        if classes.get(symbol, klass) != klass:
            raise ValueError(
                f"{symbol!r} is {classes[symbol]!r} in universe.yaml but {klass!r} "
                f"in research/costs.yaml"
            )
        classes[symbol] = klass

    return ResearchCosts(by_class=by_class, asset_class=classes)
