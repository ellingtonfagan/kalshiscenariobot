"""Canonical bankroll-derived unit sizing.

Runtime unit values must come from the resolved bankroll, not the legacy static
``bankroll.unit_usd`` YAML field. This module centralizes that resolution so
sizing, exposure, health, and reporting cannot drift independently.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .sizing import UnitSizing, unit_sizing

UNIT_MISMATCH_TOLERANCE_DOLLARS = 0.01


def configured_unit_usd(settings: Any) -> float | None:
    game = getattr(settings, "game", None)
    if not isinstance(game, dict):
        return None
    bankroll = game.get("bankroll")
    if not isinstance(bankroll, dict) or "unit_usd" not in bankroll:
        return None
    try:
        return float(bankroll["unit_usd"])
    except (TypeError, ValueError):
        return None


def latest_balance_usd(ctx: Any) -> tuple[float, str, str | None]:
    """Resolve bankroll using the existing fail-soft execution ordering."""
    settings = ctx.settings
    mode = str(getattr(settings, "execution_mode", "paper") or "paper").lower()
    if mode == "paper":
        return float(getattr(settings, "paper_bankroll_usd", 1000.0)), "paper-bankroll", None

    reader = getattr(ctx, "read_json", None)
    sync = reader("portfolio_sync.json") if callable(reader) else {}
    sync = sync if isinstance(sync, dict) else {}
    if sync.get("ok") and sync.get("balance_usd_exact") is not None:
        return float(sync["balance_usd_exact"]), "portfolio-sync", None
    if sync.get("ok") and sync.get("balance_usd") is not None:
        return float(sync["balance_usd"]), "portfolio-sync", None
    if sync.get("ok") and sync.get("balance_cents") is not None:
        return float(sync["balance_cents"]) / 100.0, "portfolio-sync", None

    try:
        if mode == "demo":
            cents = ctx.kalshi.demo_balance_cents(settings.demo_api_base)
        else:
            cents = ctx.kalshi.balance_cents()
        return float(cents) / 100.0, f"{mode}-api", None
    except Exception as exc:
        if sync.get("balance_cents") is not None:
            return float(sync["balance_cents"]) / 100.0, "last-known-portfolio-sync", str(exc)
        return float(getattr(settings, "paper_bankroll_usd", 1000.0)), "paper-bankroll-fallback", str(exc)


def unit_invariant(
    settings: Any,
    canonical: UnitSizing,
    *,
    tolerance_dollars: float = UNIT_MISMATCH_TOLERANCE_DOLLARS,
) -> dict[str, Any]:
    configured = configured_unit_usd(settings)
    payload: dict[str, Any] = {
        "ok": True,
        "canonical_unit_size_dollars": canonical.unit_size_dollars,
        "configured_unit_usd": configured,
        "delta_dollars": None,
        "warnings": [],
    }
    if configured is None:
        return payload
    delta = round(canonical.unit_size_dollars - configured, 6)
    payload["delta_dollars"] = delta
    if abs(delta) > float(tolerance_dollars):
        payload["ok"] = False
        payload["warnings"].append(
            "legacy configured bankroll.unit_usd diverges from canonical bankroll-derived unit"
        )
    return payload


def unit_context(ctx: Any) -> dict[str, Any]:
    bankroll, source, error = latest_balance_usd(ctx)
    return unit_context_from_bankroll(
        ctx.settings,
        bankroll_usd=bankroll,
        bankroll_source=source,
        balance_error=error,
    )


def unit_context_from_bankroll(
    settings: Any,
    *,
    bankroll_usd: float,
    bankroll_source: str,
    balance_error: str | None = None,
) -> dict[str, Any]:
    unit = unit_sizing(bankroll_usd, float(getattr(settings, "unit_fraction", 0.015)))
    invariant = unit_invariant(settings, unit)
    warnings = []
    if unit.warning:
        warnings.append(unit.warning)
    warnings.extend(invariant.get("warnings") or [])
    return {
        "bankroll_usd": bankroll_usd,
        "bankroll_source": bankroll_source,
        "balance_error": balance_error,
        "unit": unit,
        "unit_size_dollars": unit.unit_size_dollars,
        "unit_cents": max(int(round(unit.unit_size_dollars * 100)), 1),
        "unit_fraction": unit.unit_fraction,
        "requested_unit_fraction": unit.requested_unit_fraction,
        "warning": "; ".join(warnings) if warnings else None,
        "warnings": warnings,
        "invariant": invariant,
    }


def unit_payload(ctx: Any) -> dict[str, Any]:
    payload = unit_context(ctx)
    unit = payload.pop("unit")
    payload["unit"] = asdict(unit)
    return payload


def unit_payload_from_bankroll(
    settings: Any,
    *,
    bankroll_usd: float,
    bankroll_source: str,
    balance_error: str | None = None,
) -> dict[str, Any]:
    payload = unit_context_from_bankroll(
        settings,
        bankroll_usd=bankroll_usd,
        bankroll_source=bankroll_source,
        balance_error=balance_error,
    )
    unit = payload.pop("unit")
    payload["unit"] = asdict(unit)
    return payload
