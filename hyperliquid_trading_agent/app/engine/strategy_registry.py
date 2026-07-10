from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from hyperliquid_trading_agent.app.engine.alpha.base import AlphaStrategy
from hyperliquid_trading_agent.app.engine.alpha.directional import (
    DirectionalMomentumStrategy,
    SupportResistanceReversionStrategy,
)
from hyperliquid_trading_agent.app.engine.alpha.equity import EquityOptionsFlowStrategy
from hyperliquid_trading_agent.app.engine.alpha.microstructure import MicrostructureOFIStrategy
from hyperliquid_trading_agent.app.engine.alpha.news_event import NewsEventAlphaStrategy
from hyperliquid_trading_agent.app.engine.alpha.wave1a import wave_1a_specs, wave_1a_strategy_instances
from hyperliquid_trading_agent.app.engine.alpha.wave1c import (
    RangeRotationStrategy,
    VolatilityCompressionBreakoutStrategy,
    wave_1c_specs,
    wave_1c_strategy_instances,
)
from hyperliquid_trading_agent.app.engine.alpha.wave2 import (
    WAVE_2_DEFERRED_IDS as _WAVE_2_DEFERRED_IDS,
)
from hyperliquid_trading_agent.app.engine.alpha.wave2 import (
    WAVE_2A_IDS as _WAVE_2A_IDS,
)
from hyperliquid_trading_agent.app.engine.alpha.wave2 import (
    WAVE_2B_IDS as _WAVE_2B_IDS,
)
from hyperliquid_trading_agent.app.engine.alpha.wave2 import (
    WAVE_2C_IDS as _WAVE_2C_IDS,
)
from hyperliquid_trading_agent.app.engine.alpha.wave2 import (
    wave_2_specs,
    wave_2_strategy_instances,
)
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
    "news_event_alpha_v2",
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

WAVE_2A_IDS = _WAVE_2A_IDS
WAVE_2B_IDS = _WAVE_2B_IDS
WAVE_2C_IDS = _WAVE_2C_IDS
WAVE_2_DEFERRED_IDS = _WAVE_2_DEFERRED_IDS

CatalogMode = Literal["wave1a_locked", "wave1c", "shadow_full_catalog", "specs_only"]
ALPHA_CATALOG_MODES = {"wave1a_locked", "wave1c", "shadow_full_catalog", "specs_only"}
PRE_WAVE1A_RUNTIME_IDS = PRE_WAVE1A_COMPARISON_IDS - {"equity_options_flow_v1"}
WAVE_1C_FULL_IDS = WAVE_1C_DETERMINISTIC_IDS | WAVE_1C_OPTIONAL_IDS
SHADOW_FULL_CATALOG_ACTIVE_IDS = (
    WAVE_1A_NUCLEUS_IDS | PRE_WAVE1A_RUNTIME_IDS | WAVE_1C_FULL_IDS | WAVE_2_DEFERRED_IDS
)


@dataclass
class StrategyRegistry:
    """In-memory registry of strategy instances and metadata contracts."""

    _strategies: dict[str, AlphaStrategy] = field(default_factory=dict)
    _specs: dict[str, StrategySpec] = field(default_factory=dict)
    catalog_mode: CatalogMode = "wave1a_locked"

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
        catalog = self.catalog_summary()
        return {
            "strategy_count": len(specs),
            "enabled_strategy_count": len([spec for spec in specs if spec.enabled]),
            "catalog_mode": self.catalog_mode,
            "alpha_catalog_mode": self.catalog_mode,
            "alpha_catalog_active_ids": sorted(self._strategies),
            "alpha_breadth_count": len([spec for spec in specs if spec.enabled and spec.counts_for_breadth]),
            "families": sorted({spec.family for spec in specs}),
            "alpha_families": sorted(self.families(alpha_only=True)),
            "runtime_enabled": catalog["runtime_enabled"],
            "paper_eligible": catalog["paper_eligible"],
            "shadow_only": catalog["shadow_only"],
            "spec_only": catalog["spec_only"],
        }

    def catalog_summary(self) -> dict[str, Any]:
        specs = self.specs(enabled_only=False)
        runtime_ids = set(self._strategies)
        enabled_specs = [spec for spec in specs if spec.enabled]
        paper_specs = [spec for spec in enabled_specs if _spec_paper_eligible(spec)]
        shadow_specs = [spec for spec in enabled_specs if _spec_shadow_only(spec)]
        family_rows: list[dict[str, Any]] = []
        for family in sorted({spec.family for spec in specs}):
            family_specs = [spec for spec in specs if spec.family == family]
            family_rows.append(
                {
                    "family": family,
                    "total_specs": len(family_specs),
                    "runtime_enabled": len([spec for spec in family_specs if spec.strategy_id in runtime_ids]),
                    "paper_eligible": len([spec for spec in family_specs if spec.enabled and _spec_paper_eligible(spec)]),
                    "shadow_only": len([spec for spec in family_specs if spec.enabled and _spec_shadow_only(spec)]),
                    "strategy_ids": [spec.strategy_id for spec in family_specs],
                }
            )
        return {
            "mode": self.catalog_mode,
            "total_specs": len(specs),
            "runtime_enabled": len(runtime_ids),
            "enabled_specs": len(enabled_specs),
            "paper_eligible": len(paper_specs),
            "shadow_only": len(shadow_specs),
            "spec_only": len([spec for spec in specs if spec.strategy_id not in runtime_ids]),
            "alpha_breadth_count": len([spec for spec in enabled_specs if spec.counts_for_breadth]),
            "runtime_enabled_ids": sorted(runtime_ids),
            "paper_eligible_ids": sorted(spec.strategy_id for spec in paper_specs),
            "shadow_only_ids": sorted(spec.strategy_id for spec in shadow_specs),
            "spec_only_ids": sorted(spec.strategy_id for spec in specs if spec.strategy_id not in runtime_ids),
            "families": family_rows,
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


def pre_wave1a_runtime_instances() -> list[AlphaStrategy]:
    """Crypto-only pre-Wave1A strategies that can emit in full shadow catalog mode."""

    return [
        DirectionalMomentumStrategy(),
        SupportResistanceReversionStrategy(),
        MicrostructureOFIStrategy(),
        NewsEventAlphaStrategy(),
    ]


def wave_1c_full_strategy_instances() -> list[AlphaStrategy]:
    """Deterministic plus optional Wave1C strategies for the full shadow catalog."""

    return [*wave_1c_strategy_instances(), RangeRotationStrategy(), VolatilityCompressionBreakoutStrategy()]


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
    include_planned_wave_2_specs: bool = True,
    enable_wave_1c: bool = False,
    catalog_mode: CatalogMode | str = "wave1a_locked",
    news_event_alpha_mode: Literal["off", "shadow", "paper"] | None = None,
) -> StrategyRegistry:
    mode = _resolve_catalog_mode(catalog_mode, enable_wave_1c=enable_wave_1c)
    registry = StrategyRegistry(catalog_mode=mode)
    registry.register_many(
        runtime_strategy_instances(
            catalog_mode=mode,
            news_event_alpha_mode=news_event_alpha_mode,
        )
    )
    wave_1c_enabled = mode == "wave1c"
    if include_planned_wave_1a_specs:
        for spec in planned_wave_1a_specs():
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    if include_planned_wave_1c_specs:
        for spec in planned_wave_1c_specs(enabled=wave_1c_enabled):
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    if include_planned_wave_2_specs:
        for spec in planned_wave_2_specs():
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    if include_pre_wave1a_comparison_specs:
        for spec in pre_wave1a_comparison_specs():
            if registry.spec(spec.strategy_id) is None:
                registry.register_spec(spec)
    return registry


def runtime_strategy_instances(
    *,
    catalog_mode: CatalogMode | str = "wave1a_locked",
    news_event_alpha_mode: Literal["off", "shadow", "paper"] | None = None,
) -> list[AlphaStrategy]:
    """Return runtime-enabled strategy instances for a catalog mode."""

    mode = _resolve_catalog_mode(catalog_mode, enable_wave_1c=False)
    effective_news_mode = news_event_alpha_mode
    if effective_news_mode is None:
        # Preserve the historical direct-factory catalog shape. EngineService always
        # supplies the explicit operator setting, including "off".
        effective_news_mode = "shadow" if mode == "shadow_full_catalog" else "off"
    news = _news_event_instances(effective_news_mode, shadow_only=mode == "shadow_full_catalog")
    if mode in {"wave1a_locked", "specs_only"}:
        return [*default_strategy_instances(), *news]
    if mode == "wave1c":
        return [
            *default_strategy_instances(),
            *news,
            *[_active_instance(strategy, reason="wave1c_enabled") for strategy in wave_1c_strategy_instances()],
        ]
    if mode == "shadow_full_catalog":
        strategies: list[AlphaStrategy] = []
        strategies.extend(_shadow_only_instances(default_strategy_instances(), reason="shadow_full_catalog_wave1a"))
        pre_wave = [strategy for strategy in pre_wave1a_runtime_instances() if strategy.strategy_id != "news_event_alpha_v2"]
        strategies.extend(_shadow_only_instances(pre_wave, reason="shadow_full_catalog_pre_wave1a"))
        strategies.extend(news)
        strategies.extend(_shadow_only_instances(wave_1c_full_strategy_instances(), reason="shadow_full_catalog_wave1c"))
        strategies.extend(_shadow_only_instances(wave_2_strategy_instances(), reason="shadow_full_catalog_wave2", force_breadth=True))
        return strategies
    raise ValueError(f"unsupported catalog mode: {catalog_mode}")


def _news_event_instances(
    mode: Literal["off", "shadow", "paper"],
    *,
    shadow_only: bool,
) -> list[AlphaStrategy]:
    """Activate Newswire alpha independently of the historical wave catalog.

    News risk-state consumption is always independent of this switch.  The strategy is
    opt-in at runtime (shadow by default in Settings), and the full-shadow catalog can
    never make it paper eligible even when a conflicting operator setting is supplied.
    """
    if mode == "off":
        return []
    strategy: AlphaStrategy = NewsEventAlphaStrategy()
    if mode == "shadow" or shadow_only:
        return _shadow_only_instances([strategy], reason="newswire_alpha_shadow")
    return [_active_instance(strategy, reason="newswire_alpha_paper")]


def _spec_activation_scope(spec: StrategySpec) -> str:
    return str(spec.metadata.get("activation_scope") or "paper_shadow")


def _spec_paper_eligible(spec: StrategySpec) -> bool:
    return bool(spec.metadata.get("paper_eligible", True)) and _spec_activation_scope(spec) != "shadow_only"


def _spec_shadow_only(spec: StrategySpec) -> bool:
    return _spec_activation_scope(spec) == "shadow_only" or bool(spec.metadata.get("operator_promotion_required", False))


def _resolve_catalog_mode(catalog_mode: CatalogMode | str, *, enable_wave_1c: bool) -> CatalogMode:
    mode = str(catalog_mode).strip().lower()
    if enable_wave_1c and mode == "wave1a_locked":
        mode = "wave1c"
    if mode not in ALPHA_CATALOG_MODES:
        raise ValueError(f"unsupported alpha catalog mode: {catalog_mode}")
    return mode  # type: ignore[return-value]


def _active_instance(strategy: AlphaStrategy, *, reason: str) -> AlphaStrategy:
    spec = _strategy_spec(strategy)
    return _strategy_with_spec(
        strategy,
        spec.model_copy(
            update={
                "enabled": True,
                "counts_for_breadth": spec.counts_for_breadth,
                "metadata": {**spec.metadata, "runtime_enabled_reason": reason},
            }
        ),
    )


def _shadow_only_instances(
    strategies: Iterable[AlphaStrategy],
    *,
    reason: str,
    force_breadth: bool = False,
) -> list[AlphaStrategy]:
    return [_strategy_with_spec(strategy, _shadow_only_spec(_strategy_spec(strategy), reason=reason, force_breadth=force_breadth)) for strategy in strategies]


def _shadow_only_spec(spec: StrategySpec, *, reason: str, force_breadth: bool = False) -> StrategySpec:
    updates: dict[str, Any] = {
        "enabled": True,
        "counts_for_breadth": bool(spec.counts_for_breadth or force_breadth),
        "metadata": {
            **spec.metadata,
            "activation_scope": "shadow_only",
            "paper_eligible": False,
            "operator_promotion_required": True,
            "runtime_enabled_reason": reason,
            "catalog_mode": "shadow_full_catalog",
        },
    }
    if spec.max_candidates_per_run <= 0:
        updates["max_candidates_per_run"] = 1
    if spec.max_allocation_share_pct <= 0.0 and spec.family != "risk_off_defensive":
        updates["max_allocation_share_pct"] = 25.0
    if spec.min_confidence >= 1.0:
        updates["min_confidence"] = 0.35
    if spec.min_ev_bps >= 999.0:
        updates["min_ev_bps"] = 8.0
    return spec.model_copy(update=updates)


def _strategy_with_spec(strategy: AlphaStrategy, spec: StrategySpec) -> AlphaStrategy:
    setattr(strategy, "spec", spec)
    setattr(strategy, "strategy_id", spec.strategy_id)
    return strategy


def planned_wave_1a_specs() -> list[StrategySpec]:
    """Specs for the accepted Wave 1A nucleus."""

    return wave_1a_specs()


def planned_wave_1c_specs(*, enabled: bool = False) -> list[StrategySpec]:
    """Specs for deterministic Wave 1C breadth, gated behind evidence readiness."""

    return wave_1c_specs(enabled=enabled)


def planned_wave_2_specs() -> list[StrategySpec]:
    """Disabled specs for the deferred proprietary perp-DEX Wave 2 roadmap."""

    return wave_2_specs()
