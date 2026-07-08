"""Pure Kalshi fee model used by edge and execution gates."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_TAKER_FEE_FACTOR = 0.07
DEFAULT_MAKER_FEE_FACTOR = 0.0175


@dataclass(frozen=True)
class FeeModel:
    taker_factor: float = DEFAULT_TAKER_FEE_FACTOR
    maker_factor: float = DEFAULT_MAKER_FEE_FACTOR

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def _factor(maker: bool, taker_factor: float, maker_factor: float) -> float:
    return max(float(maker_factor if maker else taker_factor), 0.0)


def _price_prob(price_cents: int | float) -> float:
    price = float(price_cents)
    if price <= 0 or price >= 100:
        raise ValueError("price_cents must be between 1 and 99")
    return price / 100.0


def expected_fee(
    price_cents: int | float,
    maker: bool,
    *,
    taker_factor: float = DEFAULT_TAKER_FEE_FACTOR,
    maker_factor: float = DEFAULT_MAKER_FEE_FACTOR,
) -> float:
    """Return expected fee in cents per contract.

    Kalshi reports average_fee_paid as dollars per contract. Returning cents here
    keeps edge conversion explicit: fee probability points are fee_cents / 100.
    """
    p = _price_prob(price_cents)
    cents = _factor(maker, taker_factor, maker_factor) * p * (1.0 - p) * 100.0
    return round(cents, 4)


def expected_fee_prob(
    price_cents: int | float,
    maker: bool,
    *,
    taker_factor: float = DEFAULT_TAKER_FEE_FACTOR,
    maker_factor: float = DEFAULT_MAKER_FEE_FACTOR,
) -> float:
    return round(
        expected_fee(
            price_cents,
            maker,
            taker_factor=taker_factor,
            maker_factor=maker_factor,
        ) / 100.0,
        6,
    )


def expected_total_fee_cents(
    price_cents: int | float,
    contracts: int | float,
    maker: bool,
    *,
    taker_factor: float = DEFAULT_TAKER_FEE_FACTOR,
    maker_factor: float = DEFAULT_MAKER_FEE_FACTOR,
    round_up_to_cent: bool = True,
) -> float:
    raw = expected_fee(
        price_cents,
        maker,
        taker_factor=taker_factor,
        maker_factor=maker_factor,
    ) * max(float(contracts), 0.0)
    return float(math.ceil(raw)) if round_up_to_cent else round(raw, 4)


def model_from_settings(settings: Any) -> FeeModel:
    return FeeModel(
        taker_factor=float(getattr(settings, "kalshi_taker_fee_factor", DEFAULT_TAKER_FEE_FACTOR)),
        maker_factor=float(getattr(settings, "kalshi_maker_fee_factor", DEFAULT_MAKER_FEE_FACTOR)),
    )


def fee_details(
    price_cents: int | float | None,
    *,
    maker: bool,
    settings: Any | None = None,
) -> dict[str, Any]:
    if price_cents is None:
        return {
            "liquidity_role": "maker" if maker else "taker",
            "expected_fee_cents": None,
            "expected_fee_prob": None,
            "fee_model": (model_from_settings(settings).as_dict() if settings is not None else FeeModel().as_dict()),
        }
    model = model_from_settings(settings) if settings is not None else FeeModel()
    cents = expected_fee(
        price_cents,
        maker,
        taker_factor=model.taker_factor,
        maker_factor=model.maker_factor,
    )
    return {
        "liquidity_role": "maker" if maker else "taker",
        "expected_fee_cents": cents,
        "expected_fee_prob": round(cents / 100.0, 6),
        "fee_model": model.as_dict(),
    }
