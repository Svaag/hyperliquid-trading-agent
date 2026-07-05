from __future__ import annotations

from dataclasses import dataclass

OUTCOME_ASSET_ID_OFFSET = 100_000_000


@dataclass(frozen=True)
class OutcomeAssetId:
    outcome_id: int
    side: int

    def __post_init__(self) -> None:
        if self.outcome_id < 0:
            raise ValueError("outcome_id must be non-negative")
        if self.side not in {0, 1}:
            raise ValueError("HIP-4 side must be 0 or 1")

    @property
    def encoding(self) -> int:
        return encoding(self.outcome_id, self.side)

    @property
    def coin(self) -> str:
        return coin(self.outcome_id, self.side)

    @property
    def balance_token(self) -> str:
        return balance_token(self.outcome_id, self.side)

    @property
    def exchange_asset_id(self) -> int:
        return exchange_asset_id(self.outcome_id, self.side)


def encoding(outcome_id: int, side: int) -> int:
    if outcome_id < 0:
        raise ValueError("outcome_id must be non-negative")
    if side not in {0, 1}:
        raise ValueError("HIP-4 side must be 0 or 1")
    return 10 * int(outcome_id) + int(side)


def coin(outcome_id: int, side: int) -> str:
    return f"#{encoding(outcome_id, side)}"


def balance_token(outcome_id: int, side: int) -> str:
    return f"+{encoding(outcome_id, side)}"


def exchange_asset_id(outcome_id: int, side: int) -> int:
    return OUTCOME_ASSET_ID_OFFSET + encoding(outcome_id, side)


def parse_coin(value: str) -> OutcomeAssetId:
    raw = value.strip()
    if not raw.startswith("#"):
        raise ValueError("HIP-4 trade coin must start with '#'")
    return _parse_encoding(raw[1:])


def parse_balance_token(value: str) -> OutcomeAssetId:
    raw = value.strip()
    if not raw.startswith("+"):
        raise ValueError("HIP-4 balance token must start with '+'")
    return _parse_encoding(raw[1:])


def parse_identifier(value: str) -> OutcomeAssetId:
    """Parse a HIP-4 side identifier.

    Supported forms mirror Hyperliquid's current HIP-4 conventions:
    `#20` for book coins, `+20` for balance tokens, `20` for bare encodings,
    and `100000020` for exchange asset ids.
    """

    raw = value.strip()
    if raw.startswith("#"):
        return parse_coin(raw)
    if raw.startswith("+"):
        return parse_balance_token(raw)
    if not raw.isdigit():
        raise ValueError("HIP-4 identifier must be numeric or start with '#' / '+'")
    numeric = int(raw)
    if numeric >= OUTCOME_ASSET_ID_OFFSET:
        numeric -= OUTCOME_ASSET_ID_OFFSET
    return _parse_encoding(str(numeric))


def outcome_asset_id_from_identifier(value: str) -> int | None:
    try:
        asset = parse_identifier(value)
    except ValueError:
        return None
    return asset.exchange_asset_id


def _parse_encoding(raw: str) -> OutcomeAssetId:
    if not raw.isdigit():
        raise ValueError("HIP-4 encoded asset must be numeric")
    encoded = int(raw)
    side = encoded % 10
    if side not in {0, 1}:
        raise ValueError("HIP-4 encoded side must be 0 or 1")
    return OutcomeAssetId(outcome_id=encoded // 10, side=side)
