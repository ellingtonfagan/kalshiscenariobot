"""Phase: settlement-audit. Resolve filled orders and record CLV."""
from __future__ import annotations

from typing import Any

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..research import ResearchStore, utc_now
from ..settlement_audit import (
    SETTLEMENT_EVENT_TYPE,
    closing_consensus,
    market_resolution,
    normalized_order,
    settlement_record,
    summarize,
)
from .base import Context, load_context


def _format(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    return (
        f"[settlement-audit] checked={payload.get('checked_count', 0)} "
        f"settled={summary.get('settled_count', 0)} "
        f"pending={payload.get('pending_count', 0)} "
        f"skipped={payload.get('skipped_count', 0)} "
        f"clv_available={summary.get('clv_available_count', 0)} "
        f"clv_beat_rate={summary.get('clv_beat_rate')}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    orders = store.list_execution_orders(unsettled_only=True)

    records: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for raw_order in orders:
        order = normalized_order(raw_order)
        if order is None:
            skipped.append({
                "client_order_id": raw_order.get("client_order_id"),
                "game_id": raw_order.get("game_id"),
                "mode": raw_order.get("mode"),
                "reason": "no filled exposure or missing ticker/side",
            })
            continue

        try:
            market = ctx.kalshi.market(order["ticker"])
        except Exception as exc:
            error = {
                "client_order_id": order["client_order_id"],
                "ticker": order["ticker"],
                "error": str(exc),
            }
            errors.append(error)
            audit.dead_letter("SETTLEMENT_AUDIT_MARKET_LOOKUP", str(exc), error, order["game_id"])
            continue

        resolution = market_resolution(market)
        if not resolution["settled"]:
            pending.append({
                "client_order_id": order["client_order_id"],
                "game_id": order["game_id"],
                "mode": order["mode"],
                "ticker": order["ticker"],
                "market_status": resolution.get("market_status"),
                "reason": "market not settled or winner side unavailable",
            })
            continue

        consensus = closing_consensus(
            ctx.settings.data_dir,
            order["ticker"],
            order["side"],
            market,
        )
        record = settlement_record(order, market, resolution, consensus)
        inserted = store.record_settlement(record)
        record["inserted"] = inserted
        records.append(record)
        if inserted:
            audit.log(SETTLEMENT_EVENT_TYPE, record, order["game_id"])

    payload = {
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "checked_count": len(orders),
        "pending_count": len(pending),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "summary": summarize(records),
        "records": records,
        "pending": pending,
        "skipped": skipped,
        "errors": errors,
        "notes": [
            "Read-only audit phase; no orders are placed or modified.",
            "Win/loss comes from Kalshi market settlement fields only.",
            "CLV uses the latest available pre-close candidate-ranker consensus snapshot.",
        ],
    }
    ctx.write_json("settlement_audit.json", payload)
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
