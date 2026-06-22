"""Conservative binary-contract sizing helpers."""
from __future__ import annotations

from dataclasses import dataclass

from .guardrails import MAX_STAKE_UNITS


@dataclass(frozen=True)
class KellyResult:
    side: str
    fraction: float
    adjusted_fraction: float
    stake_units: float
    contracts: int
    entry_price_cents: int
    skipped_reason: str | None = None


def capped_kelly(edge: float, market_prob: float, entry_price_cents: int,
                 unit_cents: int, max_units: float = MAX_STAKE_UNITS,
                 multiplier: float = 0.5, min_edge: float = 0.05) -> KellyResult:
    """Half-Kelly for a binary YES/NO edge, hard-capped in units."""
    side = "yes" if edge >= 0 else "no"
    abs_edge = abs(edge)
    if abs_edge < min_edge:
        return KellyResult(side, 0.0, 0.0, 0.0, 0, entry_price_cents,
                           f"edge {abs_edge:.3f} below {min_edge:.3f}")
    if market_prob <= 0 or market_prob >= 1:
        return KellyResult(side, 0.0, 0.0, 0.0, 0, entry_price_cents,
                           "market probability must be inside (0,1)")
    if entry_price_cents <= 0:
        return KellyResult(side, 0.0, 0.0, 0.0, 0, entry_price_cents,
                           "entry price must be positive")

    fraction = edge / (1 - market_prob) if side == "yes" else abs_edge / market_prob
    adjusted = max(fraction * multiplier, 0.0)
    stake_units = min(adjusted, max_units)
    stake_cents = int(stake_units * unit_cents)
    contracts = stake_cents // entry_price_cents
    if contracts <= 0:
        return KellyResult(side, fraction, adjusted, 0.0, 0, entry_price_cents,
                           "position too small for one contract")
    stake_units = (contracts * entry_price_cents) / max(unit_cents, 1)
    return KellyResult(side, fraction, adjusted, stake_units, contracts, entry_price_cents)
