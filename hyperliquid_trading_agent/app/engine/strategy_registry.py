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
from hyperliquid_trading_agent.app.engine.alpha.wave1c import wave_1c_specs, wave_1c_strategy_instances
from hyperliquid_trading_agent.app.engine.schemas import StrategySpec

WAVE_1A_NUCLEUS_IDS = {
    "microstructure_ofi_v2",
    "liquidation_cascade_v1",
    "liquidation_mean_revert_v1",
    "funding_carry_v1",
    "oi_breakout_v1",
    "legacy_signal_adapter_v1",
    "regime_defensive_flat_v1",
}

PRE_WAVE1A_COMPARISON_IDS = {
    "directional_momentum_v2",
    "support_resistance_reversion_v2",
    "microstructure_ofi_v1",
    "news_event_alpha_v1",
    "equity_options_flow_v1",
}

WAVE_1C_DETERMINISTIC_IDS = {
    "microstructure_absorption_v1",
    "funding_squeeze_v1",
    "basis_reversion_v1",
    "news_impulse_v1",
}

WAVE_1C_OPTIONAL_IDS = {
    "range_rotation_v1",
    "volatility_compression_breakout_v1",
}


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
    """Runtime strategy instances for the locked Wave 1A nucleus.

    Wave 1 is intentionally evidence-first. Pre-Wave1A strategies remain available
    as disabled comparison specs, but they are not active runtime alpha and do not
    count as breadth until a later controlled Wave 1C/1D promotion.
    """

    return wave_1a_strategy_instances()


def pre_wave1a_comparison_specs() -> list[StrategySpec]:
    """Disabled specs retained for historical/replay comparison only."""

    strategies = [
        DirectionalMomentumStrategy(),
        SupportResistanceReversionStrategy(),
        MicrostructureOFIStrategy(),
        NewsEventAlphaStrategy(),
        EquityOptionsFlowStrategy(),
    ]
    return [
        strategy.spec.model_copy(
            update={
                "enabled": False,
                "counts_for_breadth": False,
                "metadata": {
                    **strategy.spec.metadata,
                    "wave_status": "pre_wave1a_comparison_only",
                    "runtime_enabled_reason": "wave1a_nucleus_locked",
                },
            }
        )
        for strategy in strategies
    ]


def create_default_strategy_registry(
    *,
    include_planned_wave_1a_specs: bool = True,
    include_pre_wave1a_comparison_specs: bool = True,
    include_planned_wave_1c_specs: bool = True,
    enable_wave_1c: bool = False,
) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register_many(default_strategy_instances())
    if enable_wave_1c:
        registry.register_many(wave_1c_strategy_instances())
    if include_planned_wave_1a_specs:
        for spec in planned_wave_1a_specs():
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    if include_planned_wave_1c_specs:
        for spec in planned_wave_1c_specs(enabled=enable_wave_1c):
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    if include_pre_wave1a_comparison_specs:
        for spec in pre_wave1a_comparison_specs():
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    return registry


def planned_wave_1a_specs() -> list[StrategySpec]:
    """Specs for the accepted Wave 1A nucleus."""

    return wave_1a_specs()


def planned_wave_1c_specs(*, enabled: bool = False) -> list[StrategySpec]:
    """Specs for deterministic Wave 1C breadth, gated behind evidence readiness."""

    return wave_1c_specs(enabled=enabled)
