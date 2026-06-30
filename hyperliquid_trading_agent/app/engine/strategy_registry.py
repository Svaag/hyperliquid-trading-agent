from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from hyperliquid_trading_agent.app.engine.alpha.base import AlphaStrategy
from hyperliquid_trading_agent.app.engine.alpha.directional import (
    DirectionalMomentumStrategy,
    SupportResistanceReversionStrategy,
)
from hyperliquid_trading_agent.app.engine.alpha.equity import EquityOptionsFlowStrategy
from hyperliquid_trading_agent.app.engine.alpha.microstructure import MicrostructureOFIStrategy
from hyperliquid_trading_agent.app.engine.alpha.news_event import NewsEventAlphaStrategy
from hyperliquid_trading_agent.app.engine.alpha.wave1a import wave_1a_specs, wave_1a_strategy_instances
from hyperliquid_trading_agent.app.engine.schemas import StrategySpec


@dataclass
class StrategyRegistry:
    """In-memory registry of strategy instances and metadata contracts."""

    _strategies: dict[str, AlphaStrategy] = field(default_factory=dict)
    _specs: dict[str, StrategySpec] = field(default_factory=dict)

    def register(self, strategy: AlphaStrategy) -> AlphaStrategy:
        spec = _strategy_spec(strategy)
        strategy_id = spec.strategy_id
        declared_id = getattr(strategy, "strategy_id", strategy_id)
        if declared_id != strategy_id:
            raise ValueError(f"strategy_id mismatch for {strategy!r}: {declared_id!r} != {strategy_id!r}")
        if strategy_id in self._specs:
            raise ValueError(f"duplicate strategy_id registered: {strategy_id}")
        self._strategies[strategy_id] = strategy
        self._specs[strategy_id] = spec
        return strategy

    def register_spec(self, spec: StrategySpec) -> StrategySpec:
        if spec.strategy_id in self._specs:
            raise ValueError(f"duplicate strategy_id registered: {spec.strategy_id}")
        self._specs[spec.strategy_id] = spec
        return spec

    def register_many(self, strategies: Iterable[AlphaStrategy]) -> None:
        for strategy in strategies:
            self.register(strategy)

    def get(self, strategy_id: str) -> AlphaStrategy | None:
        return self._strategies.get(strategy_id)

    def spec(self, strategy_id: str) -> StrategySpec | None:
        return self._specs.get(strategy_id)

    def require_spec(self, strategy_id: str) -> StrategySpec:
        spec = self.spec(strategy_id)
        if spec is None:
            raise KeyError(strategy_id)
        return spec

    def strategies(self, *, enabled_only: bool = True) -> list[AlphaStrategy]:
        strategies = list(self._strategies.values())
        if not enabled_only:
            return strategies
        return [strategy for strategy in strategies if strategy.spec.enabled]

    def specs(self, *, enabled_only: bool = False) -> list[StrategySpec]:
        specs = list(self._specs.values())
        if enabled_only:
            specs = [spec for spec in specs if spec.enabled]
        return sorted(specs, key=lambda spec: spec.strategy_id)

    def alpha_breadth_specs(self) -> list[StrategySpec]:
        return [spec for spec in self.specs(enabled_only=True) if spec.counts_for_breadth]

    def families(self, *, alpha_only: bool = False) -> set[str]:
        specs = self.alpha_breadth_specs() if alpha_only else self.specs(enabled_only=True)
        return {spec.family for spec in specs}

    def metadata(self) -> dict[str, Any]:
        specs = self.specs(enabled_only=False)
        return {
            "strategy_count": len(specs),
            "enabled_strategy_count": len([spec for spec in specs if spec.enabled]),
            "alpha_breadth_count": len([spec for spec in specs if spec.enabled and spec.counts_for_breadth]),
            "families": sorted({spec.family for spec in specs}),
            "alpha_families": sorted(self.families(alpha_only=True)),
        }


def _strategy_spec(strategy: AlphaStrategy) -> StrategySpec:
    spec = getattr(strategy, "spec", None)
    if spec is None:
        raise ValueError(f"strategy {strategy!r} does not expose a StrategySpec")
    if not isinstance(spec, StrategySpec):
        spec = StrategySpec.model_validate(spec)
    return spec


def default_strategy_instances() -> list[AlphaStrategy]:
    return [
        DirectionalMomentumStrategy(),
        SupportResistanceReversionStrategy(),
        MicrostructureOFIStrategy(),
        NewsEventAlphaStrategy(),
        *wave_1a_strategy_instances(),
        EquityOptionsFlowStrategy(),
    ]


def create_default_strategy_registry(*, include_planned_wave_1a_specs: bool = True) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register_many(default_strategy_instances())
    if include_planned_wave_1a_specs:
        for spec in planned_wave_1a_specs():
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    return registry


def planned_wave_1a_specs() -> list[StrategySpec]:
    """Specs for the accepted Wave 1A nucleus."""

    return wave_1a_specs()
