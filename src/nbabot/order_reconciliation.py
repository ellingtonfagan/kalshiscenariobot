"""Post-hoc order fill reconciliation and maker lifecycle management."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .research import ResearchStore, utc_now

RESTING_STATUSES = {"resting", "open", "pending"}
TERMINAL_STATUSES = {"executed", "filled", "canceled", "cancelled", "expired", "rejected"}


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _num(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _price_cents(raw: Any) -> float | None:
    value = _num(raw)
    if value is None:
        return None
    if 0 <= value <= 1:
        value *= 100
    return round(value, 4) if 0 <= value <= 100 else None


def _order_doc(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("order", "event_order"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return payload


def _exchange_order_id(order_row: dict[str, Any]) -> str:
    receipt = _loads(order_row.get("receipt_json"), {}) or {}
    response = receipt.get("response") if isinstance(receipt, dict) else {}
    response = response if isinstance(response, dict) else {}
    order = _order_doc(response)
    for doc in (order, response):
        for key in ("order_id", "id", "exchange_order_id"):
            value = doc.get(key)
            if value not in (None, ""):
                return str(value)
    return str(order_row["client_order_id"])


def _status(order: dict[str, Any]) -> str:
    return str(
        order.get("status")
        or order.get("order_status")
        or order.get("state")
        or ""
    ).lower()


def _filled_count(order: dict[str, Any]) -> float:
    for key in ("filled_count", "fill_count", "filled", "executed_count"):
        value = _num(order.get(key))
        if value is not None:
            return value
    return 0.0


def _fee_cents(fill: dict[str, Any]) -> float | None:
    for key in ("fee_cents", "fee_paid_cents"):
        value = _num(fill.get(key))
        if value is not None:
            return round(value, 4)
    for key in ("fee", "fee_paid", "average_fee_paid", "fee_cost"):
        value = _num(fill.get(key))
        if value is not None:
            return round(value * 100.0, 4)
    return None


def _fill_docs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("fills", "trades"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if isinstance(payload.get("fill"), dict):
        return [payload["fill"]]
    return []


def _normalize_fill(
    fill: dict[str, Any],
    *,
    order_row: dict[str, Any],
    order_doc: dict[str, Any],
    exchange_order_id: str,
) -> dict[str, Any] | None:
    request = _loads(order_row.get("request_json"), {}) or {}
    count = _num(
        fill.get("contracts")
        or fill.get("count")
        or fill.get("fill_count")
        or fill.get("filled_count")
        or fill.get("count_fp")
        or fill.get("quantity")
    )
    price = None
    side = str(fill.get("side") or fill.get("outcome_side") or request.get("side") or "yes").lower()
    side_price_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    for key in (
        "price_cents", "price", side_price_key, "yes_price",
        "yes_price_dollars", "no_price_dollars", "average_fill_price", "avg_price",
    ):
        price = _price_cents(fill.get(key))
        if price is not None:
            break
    if count is None or count <= 0 or price is None:
        return None
    filled_at = (
        fill.get("filled_at")
        or fill.get("created_time")
        or fill.get("created_at")
        or order_doc.get("updated_time")
        or utc_now()
    )
    return {
        "fill_id": fill.get("fill_id") or fill.get("id") or fill.get("trade_id"),
        "exchange_order_id": exchange_order_id,
        "client_order_id": order_row["client_order_id"],
        "ticker": fill.get("ticker") or fill.get("market_ticker") or order_doc.get("ticker") or request.get("ticker"),
        "side": side,
        "contracts": count,
        "price_cents": price,
        "fee_cents": _fee_cents(fill),
        "filled_at": filled_at,
        "raw_fill": fill,
    }


def _synthetic_fill(order_row: dict[str, Any], order_doc: dict[str, Any], exchange_order_id: str) -> dict[str, Any] | None:
    count = _filled_count(order_doc)
    request = _loads(order_row.get("request_json"), {}) or {}
    price = None
    for key in ("average_fill_price", "avg_price", "price", "yes_price"):
        price = _price_cents(order_doc.get(key))
        if price is not None:
            break
    if price is None:
        price = _price_cents(request.get("price_cents"))
    if count <= 0 or price is None:
        return None
    return _normalize_fill(
        {
            "fill_id": f"{exchange_order_id}:synthetic",
            "contracts": count,
            "price_cents": price,
            "fee_cents": _fee_cents(order_doc),
            "filled_at": order_doc.get("updated_time") or order_doc.get("created_time") or utc_now(),
        },
        order_row=order_row,
        order_doc=order_doc,
        exchange_order_id=exchange_order_id,
    )


def _age_minutes(order_row: dict[str, Any]) -> float | None:
    created = _parse_dt(order_row.get("created_at"))
    if created is None:
        return None
    return (datetime.now(timezone.utc) - created).total_seconds() / 60.0


def _market_started(market: dict[str, Any]) -> bool:
    status = str(market.get("status") or "").lower()
    if status and status not in {"open", "active"}:
        return True
    close_at = _parse_dt(market.get("close_time") or market.get("expected_expiration_time"))
    return bool(close_at and close_at <= datetime.now(timezone.utc))


def _liquidity_role(order_row: dict[str, Any]) -> str:
    intent = _loads(order_row.get("intent_json"), {}) or {}
    request = _loads(order_row.get("request_json"), {}) or {}
    return str(intent.get("liquidity_role") or request.get("liquidity_role") or "maker")


def reconcile_demo_orders(ctx: Any, store: ResearchStore, audit: Any | None = None) -> dict[str, Any]:
    """Refresh recorded demo orders from Kalshi demo and persist late fills/cancels."""
    rows = []
    for row in store.list_order_records("demo", include_terminal=True):
        lifecycle = row.get("lifecycle") or {}
        if int(lifecycle.get("terminal") or 0):
            status = str(lifecycle.get("status") or "").lower()
            filled_count = float(lifecycle.get("filled_count") or 0.0)
            if status in {"canceled", "cancelled", "expired", "rejected"} and filled_count <= 0:
                continue
        rows.append(row)
    reconciled: list[dict[str, Any]] = []
    canceled: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    max_age = int(getattr(ctx.settings, "resting_order_max_age_minutes", 90))

    for row in rows:
        exchange_id = _exchange_order_id(row)
        try:
            status_payload = ctx.kalshi.demo_order(ctx.settings.demo_api_base, exchange_id)
            order_doc = _order_doc(status_payload)
        except Exception as exc:
            err = {"client_order_id": row["client_order_id"], "exchange_order_id": exchange_id, "error": str(exc)}
            errors.append(err)
            if audit is not None:
                audit.dead_letter("DEMO_ORDER_RECONCILE_STATUS", str(exc), err, row["game_id"])
            continue

        status = _status(order_doc)
        fills_payload = {}
        fills: list[dict[str, Any]] = []
        try:
            fills_payload = ctx.kalshi.demo_fills(ctx.settings.demo_api_base, order_id=exchange_id)
            fills = _fill_docs(fills_payload)
        except Exception as exc:
            errors.append({
                "client_order_id": row["client_order_id"],
                "exchange_order_id": exchange_id,
                "endpoint": "/portfolio/fills",
                "error": str(exc),
            })

        matching_fills = [
            fill for fill in fills
            if str(fill.get("order_id") or exchange_id) == exchange_id
        ]
        normalized = [
            item for item in (
                _normalize_fill(fill, order_row=row, order_doc=order_doc, exchange_order_id=exchange_id)
                for fill in matching_fills
            )
            if item is not None
        ]
        if not normalized:
            synthetic = _synthetic_fill(row, order_doc, exchange_id)
            if synthetic is not None:
                normalized.append(synthetic)

        inserted = 0
        for fill in normalized:
            if store.record_fill(row["game_id"], row["client_order_id"], fill):
                inserted += 1
        filled_count = sum(float(fill.get("contracts") or 0) for fill in normalized) or _filled_count(order_doc)
        terminal = status in TERMINAL_STATUSES
        store.record_order_lifecycle(
            client_order_id=row["client_order_id"],
            game_id=row["game_id"],
            mode="demo",
            status=status or "unknown",
            terminal=terminal,
            filled_count=filled_count,
            raw={"order": order_doc, "fills": fills_payload},
        )
        reconciled.append({
            "client_order_id": row["client_order_id"],
            "exchange_order_id": exchange_id,
            "status": status,
            "terminal": terminal,
            "filled_count": filled_count,
            "fills_inserted": inserted,
            "fills": normalized,
        })

        if terminal or status not in RESTING_STATUSES or _liquidity_role(row) != "maker":
            continue
        reason = None
        age = _age_minutes(row)
        if age is not None and age >= max_age:
            reason = f"resting_order_age_{int(age)}m_exceeds_{max_age}m"
        else:
            request = _loads(row.get("request_json"), {}) or {}
            ticker = request.get("ticker")
            if ticker:
                try:
                    if _market_started(ctx.kalshi.market(str(ticker))):
                        reason = "game_or_market_started"
                except Exception:
                    reason = None
        if not reason:
            continue
        try:
            cancel_response = ctx.kalshi.demo_cancel_event_order(ctx.settings.demo_api_base, exchange_id)
            canceled_at = utc_now()
            store.record_order_lifecycle(
                client_order_id=row["client_order_id"],
                game_id=row["game_id"],
                mode="demo",
                status="canceled",
                terminal=True,
                filled_count=filled_count,
                canceled_at=canceled_at,
                cancel_reason=reason,
                raw={"order": order_doc, "cancel_response": cancel_response},
            )
            item = {
                "client_order_id": row["client_order_id"],
                "exchange_order_id": exchange_id,
                "reason": reason,
                "canceled_at": canceled_at,
                "response": cancel_response,
            }
            canceled.append(item)
            if audit is not None:
                audit.log("DEMO_ORDER_CANCELED", item, row["game_id"])
        except Exception as exc:
            err = {"client_order_id": row["client_order_id"], "exchange_order_id": exchange_id, "reason": reason, "error": str(exc)}
            errors.append(err)
            if audit is not None:
                audit.dead_letter("DEMO_ORDER_CANCEL", str(exc), err, row["game_id"])

    return {
        "generated_at": utc_now(),
        "checked_count": len(rows),
        "reconciled_count": len(reconciled),
        "fills_inserted_count": sum(int(row.get("fills_inserted") or 0) for row in reconciled),
        "canceled_count": len(canceled),
        "errors": errors,
        "orders": reconciled,
        "canceled": canceled,
        "fill_rate": store.fill_rate_summary(),
    }
