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


@dataclass(frozen=True)
class UnitSizing:
    bankroll_usd: float
    unit_fraction: float
    unit_size_dollars: float
    requested_unit_fraction: float
    warning: str | None = None


@dataclass(frozen=True)
class ContractSizing:
    units_staked: float
    stake_dollars: float
    contracts: int
    notional_dollars: float
    unit_size_dollars: float
    skipped_reason: str | None = None


def unit_sizing(bankroll_usd: float, unit_fraction: float) -> UnitSizing:
    """Return a clamped one-unit dollar size from bankroll."""
    requested = float(unit_fraction)
    clamped = min(max(requested, 0.005), 0.02)
    warning = None
    if clamped != requested:
        warning = (
            f"NBABOT_UNIT_FRACTION {requested:.4f} outside [0.005, 0.020]; "
            f"using {clamped:.4f}"
        )
    bankroll = max(float(bankroll_usd), 0.0)
    return UnitSizing(
        bankroll_usd=bankroll,
        unit_fraction=clamped,
        unit_size_dollars=round(bankroll * clamped, 4),
        requested_unit_fraction=requested,
        warning=warning,
    )


def contracts_for_units(
    units: float,
    entry_price_cents: int,
    sizing: UnitSizing,
    *,
    minimum_contracts: int = 1,
    max_units: float = MAX_STAKE_UNITS,
    max_order_notional_fraction: float = 0.10,
) -> ContractSizing:
    """Convert unit stake to whole contracts with a bankroll notional backstop."""
    price = max(int(entry_price_cents), 0) / 100.0
    if price <= 0:
        return ContractSizing(0.0, 0.0, 0, 0.0, sizing.unit_size_dollars, "entry price must be positive")
    capped_units = min(max(float(units), 0.0), float(max_units))
    stake_dollars = capped_units * sizing.unit_size_dollars
    contracts = int(stake_dollars // price)
    if contracts <= 0 and capped_units > 0 and minimum_contracts > 0:
        contracts = int(minimum_contracts)
    notional = contracts * price
    max_notional = max(float(max_order_notional_fraction), 0.0) * sizing.bankroll_usd
    if max_notional > 0 and notional > max_notional:
        return ContractSizing(
            capped_units,
            round(stake_dollars, 4),
            0,
            round(notional, 4),
            sizing.unit_size_dollars,
            (
                f"order notional ${notional:.2f} exceeds "
                f"{max_order_notional_fraction:.3f} bankroll cap (${max_notional:.2f})"
            ),
        )
    effective_units = notional / sizing.unit_size_dollars if sizing.unit_size_dollars > 0 else 0.0
    return ContractSizing(
        round(effective_units, 6),
        round(notional, 4),
        contracts,
        round(notional, 4),
        sizing.unit_size_dollars,
    )


def capped_kelly(edge: float, market_prob: float, entry_price_cents: int,
                 unit_cents: int, max_units: float = MAX_STAKE_UNITS,
                 multiplier: float | None = None, min_edge: float = 0.05,
                 validated: bool = False,
                 correlation_group_size: int = 1) -> KellyResult:
    """Fractional Kelly for a binary edge, hard-capped in units.

    Unvalidated market types default to quarter-Kelly. Validated market types may
    use half-Kelly. Concurrent correlated candidates reduce the multiplier
    proportionally, floored at one-eighth of the selected Kelly fraction.
    """
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

    selected_multiplier = 0.5 if validated else 0.25
    if multiplier is not None:
        selected_multiplier = float(multiplier)
    group_size = max(int(correlation_group_size or 1), 1)
    correlation_reduction = max(1.0 / group_size, 0.125)
    fraction = edge / (1 - market_prob) if side == "yes" else abs_edge / market_prob
    adjusted = max(fraction * selected_multiplier * correlation_reduction, 0.0)
    stake_units = min(adjusted, max_units)
    stake_cents = int(stake_units * unit_cents)
    contracts = stake_cents // entry_price_cents
    if contracts <= 0:
        return KellyResult(side, fraction, adjusted, 0.0, 0, entry_price_cents,
                           "position too small for one contract")
    stake_units = (contracts * entry_price_cents) / max(unit_cents, 1)
    return KellyResult(side, fraction, adjusted, stake_units, contracts, entry_price_cents)
