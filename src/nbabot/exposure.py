"""Open exposure reconciliation against exchange state."""
from __future__ import annotations

from typing import Any

from .research import ResearchStore, utc_now
from .units import unit_context

OPEN_ORDER_STATUSES = {"resting", "open", "pending", "accepted"}
TERMINAL_ORDER_STATUSES = {"filled", "executed", "canceled", "cancelled", "expired", "rejected"}
DEFAULT_TOLERANCE_UNITS = 0.01


def _num(raw: Any) -> float | None:
    try:
        if raw in (None, ""):
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _order_docs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("orders", "event_orders"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _status(order: dict[str, Any]) -> str:
    return str(
        order.get("status")
        or order.get("order_status")
        or order.get("state")
        or ""
    ).lower()


def _ticker(order: dict[str, Any]) -> str:
    return str(order.get("ticker") or order.get("market_ticker") or "")


def _price_cents(order: dict[str, Any]) -> float:
    side = str(order.get("side") or order.get("outcome_side") or "yes").lower()
    keys = (
        "price_cents",
        "price",
        "yes_price" if side == "yes" else "no_price",
        "yes_price_dollars" if side == "yes" else "no_price_dollars",
    )
    for key in keys:
        value = _num(order.get(key))
        if value is None:
            continue
        if 0 <= value <= 1:
            value *= 100
        if 0 <= value <= 100:
            return value
    return 100.0


def _count(order: dict[str, Any]) -> float:
    # Kalshi's order list returns fixed-point fields suffixed `_fp`
    # (remaining_count_fp / initial_count_fp / fill_count_fp). Missing those
    # made every resting order count as 0 contracts, so open orders
    # contributed no exposure and the cap could be stacked past silently.
    for key in ("remaining_count", "remaining_count_fp", "count",
                "contracts", "quantity"):
        value = _num(order.get(key))
        if value is not None:
            return max(value, 0.0)
    filled = _num(order.get("filled_count")) or _num(order.get("fill_count_fp")) or 0.0
    total = _num(order.get("initial_count")) or _num(order.get("initial_count_fp")) or 0.0
    return max(total - filled, 0.0)


def exchange_state(ctx: Any) -> dict[str, Any]:
    mode = str(getattr(ctx.settings, "execution_mode", "paper") or "paper").lower()
    if mode == "demo":
        positions = ctx.kalshi.demo_positions(ctx.settings.demo_api_base)
        orders = ctx.kalshi.demo_orders(ctx.settings.demo_api_base, limit=500)
        source = "kalshi-demo"
    else:
        positions = ctx.kalshi.positions()
        orders = ctx.kalshi.orders(limit=500)
        source = "kalshi-live"
    return {"mode": mode, "source": source, "positions": positions, "orders": _order_docs(orders)}


def exchange_exposure_units(
    state: dict[str, Any],
    *,
    unit_size_dollars: float | None = None,
    unit_usd: float | None = None,
) -> dict[str, Any]:
    unit = max(float(unit_size_dollars if unit_size_dollars is not None else unit_usd or 1.0), 0.01)
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    position_units = {
        str(ticker): round(abs(float(contracts)) / unit, 6)
        for ticker, contracts in positions.items()
        if abs(float(contracts or 0)) > 0
    }
    open_orders = []
    order_units = 0.0
    for order in state.get("orders") or []:
        status = _status(order)
        if status in TERMINAL_ORDER_STATUSES:
            continue
        if status and status not in OPEN_ORDER_STATUSES:
            continue
        contracts = _count(order)
        if contracts <= 0:
            continue
        units = (contracts * _price_cents(order) / 100.0) / unit
        order_units += units
        open_orders.append({
            "ticker": _ticker(order),
            "status": status or "unknown",
            "contracts": contracts,
            "exposure_units": round(units, 6),
        })
    return {
        "unit_size_dollars": round(unit, 4),
        "position_exposure_units": round(sum(position_units.values()), 6),
        "open_order_exposure_units": round(order_units, 6),
        "total_exposure_units": round(sum(position_units.values()) + order_units, 6),
        "position_exposure_by_ticker": position_units,
        "open_orders": open_orders,
    }


def reconcile_open_exposure(
    ctx: Any,
    store: ResearchStore,
    table: str,
    *,
    game_id: str | None = None,
    tolerance_units: float = DEFAULT_TOLERANCE_UNITS,
) -> dict[str, Any]:
    game_id = game_id or ctx.settings.game_id
    local_game = store.game_order_exposure_units(table, game_id)
    local_portfolio = store.daily_order_exposure_units(table)
    payload: dict[str, Any] = {
        "checked_at": utc_now(),
        "game_id": game_id,
        "table": table,
        "local_game_exposure_units": round(local_game, 6),
        "local_portfolio_exposure_units": round(local_portfolio, 6),
        "authoritative_game_exposure_units": round(local_game, 6),
        "authoritative_portfolio_exposure_units": round(local_portfolio, 6),
        "diverged": False,
        "warnings": [],
    }
    try:
        units = unit_context(ctx)
        state = exchange_state(ctx)
        exchange = exchange_exposure_units(
            state,
            unit_size_dollars=float(units["unit_size_dollars"]),
        )
    except Exception as exc:
        payload["exchange_ok"] = False
        payload["exchange_error"] = str(exc)
        payload["warnings"].append(f"exchange exposure reconciliation failed: {exc}")
        return payload
    payload["unit"] = {
        "bankroll_usd": units["bankroll_usd"],
        "bankroll_source": units["bankroll_source"],
        "balance_error": units["balance_error"],
        "unit_size_dollars": units["unit_size_dollars"],
        "unit_cents": units["unit_cents"],
        "unit_fraction": units["unit_fraction"],
        "requested_unit_fraction": units["requested_unit_fraction"],
        "invariant": units["invariant"],
    }
    if units.get("warnings"):
        payload["warnings"].extend(str(warning) for warning in units["warnings"])

    exchange_total = float(exchange["total_exposure_units"])
    diff = abs(local_portfolio - exchange_total)
    payload.update({
        "exchange_ok": True,
        "exchange_source": state["source"],
        "exchange_positions": state["positions"],
        "exchange_open_orders": exchange["open_orders"],
        "exchange_position_exposure_units": exchange["position_exposure_units"],
        "exchange_open_order_exposure_units": exchange["open_order_exposure_units"],
        "exchange_total_exposure_units": exchange["total_exposure_units"],
        "exposure_delta_units": round(local_portfolio - exchange_total, 6),
    })
    if diff > float(tolerance_units):
        payload["diverged"] = True
        payload["authoritative_game_exposure_units"] = round(exchange_total, 6)
        payload["authoritative_portfolio_exposure_units"] = round(exchange_total, 6)
        payload["warnings"].append(
            "local open exposure diverges from exchange; using exchange positions/orders as source of truth"
        )
    return payload


def write_reconciliation(ctx: Any, payload: dict[str, Any]) -> None:
    writer = getattr(ctx, "write_json", None)
    if callable(writer):
        writer("exposure_reconciliation.json", payload)
