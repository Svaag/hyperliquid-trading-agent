from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str = ""
    normalized: str | None = None


def round_size_to_sz_decimals(size: float | str | Decimal, sz_decimals: int | None) -> Decimal:
    decimals = max(0, int(sz_decimals or 0))
    value = _decimal(size)
    quantum = Decimal(1).scaleb(-decimals)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def validate_hyperliquid_price(price: float | str | Decimal, *, sz_decimals: int | None, is_spot: bool = False) -> ValidationResult:
    try:
        value = _decimal(price)
    except ValueError as exc:
        return ValidationResult(False, str(exc))
    if value <= 0:
        return ValidationResult(False, "price must be positive")
    sig_figs = count_significant_figures(value)
    if sig_figs > 5:
        return ValidationResult(False, f"price has {sig_figs} significant figures; Hyperliquid allows at most 5")
    max_decimals = 8 if is_spot else 6
    allowed_decimals = max(0, max_decimals - int(sz_decimals or 0))
    exponent = value.normalize().as_tuple().exponent
    decimals = max(0, -exponent) if isinstance(exponent, int) else 0
    if decimals > allowed_decimals:
        return ValidationResult(False, f"price has {decimals} decimals; allowed is {allowed_decimals} for this asset")
    return ValidationResult(True, normalized=format(value.normalize(), "f"))


def asset_validation_summary(asset: dict[str, Any] | None, entry: float | None, size_units: float | None) -> dict[str, Any]:
    if not asset:
        return {"status": "asset_context_missing"}
    is_spot = asset.get("kind") == "spot"
    sz_decimals = asset.get("sz_decimals")
    price_result = validate_hyperliquid_price(entry, sz_decimals=sz_decimals, is_spot=is_spot) if entry else None
    rounded_size = str(round_size_to_sz_decimals(size_units, sz_decimals)) if size_units is not None else None
    return {
        "kind": asset.get("kind"),
        "asset_id": asset.get("asset_id"),
        "sz_decimals": sz_decimals,
        "max_leverage": asset.get("max_leverage"),
        "price_valid": price_result.valid if price_result else None,
        "price_reason": price_result.reason if price_result else "entry_missing",
        "rounded_size": rounded_size,
    }


def count_significant_figures(value: Decimal) -> int:
    normalized = value.normalize()
    digits = normalized.as_tuple().digits
    if normalized == 0:
        return 1
    # Decimal tuples do not include leading zeros, so count all coefficient digits
    # after trimming trailing zeros introduced by integer formatting.
    return len(digits)


def _decimal(value: float | str | Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal value") from exc
