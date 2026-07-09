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
    normalized_shadow_intent,
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
    reconciliation = None
    if ctx.settings.execution_mode == "demo":
        from ..order_reconciliation import reconcile_demo_orders

        reconciliation = reconcile_demo_orders(ctx, store, audit)
    orders = store.list_execution_orders(unsettled_only=True)
    shadows = store.list_shadow_intents(unsettled_only=True)

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

    shadow_records: list[dict[str, Any]] = []
    shadow_pending: list[dict[str, Any]] = []
    shadow_skipped: list[dict[str, Any]] = []
    shadow_errors: list[dict[str, Any]] = []
    for raw_shadow in shadows:
        shadow = normalized_shadow_intent(raw_shadow)
        if shadow is None:
            shadow_skipped.append({
                "client_order_id": raw_shadow.get("client_order_id"),
                "game_id": raw_shadow.get("game_id"),
                "mode": raw_shadow.get("mode"),
                "reason": "missing ticker/side",
            })
            continue
        try:
            market = ctx.kalshi.market(shadow["ticker"])
        except Exception as exc:
            error = {
                "client_order_id": shadow["client_order_id"],
                "ticker": shadow["ticker"],
                "error": str(exc),
            }
            shadow_errors.append(error)
            audit.dead_letter("SHADOW_SETTLEMENT_AUDIT_MARKET_LOOKUP", str(exc), error, shadow["game_id"])
            continue
        resolution = market_resolution(market)
        if not resolution["settled"]:
            shadow_pending.append({
                "client_order_id": shadow["client_order_id"],
                "game_id": shadow["game_id"],
                "mode": shadow["mode"],
                "ticker": shadow["ticker"],
                "market_status": resolution.get("market_status"),
                "reason": "market not settled or winner side unavailable",
            })
            continue
        consensus = closing_consensus(
            ctx.settings.data_dir,
            shadow["ticker"],
            shadow["side"],
            market,
        )
        record = settlement_record(shadow, market, resolution, consensus)
        inserted = store.record_shadow_settlement(record)
        record["inserted"] = inserted
        shadow_records.append(record)
        if inserted:
            audit.log("SHADOW_SETTLEMENT_AUDIT_RECORD", record, shadow["game_id"])

    payload = {
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "checked_count": len(orders),
        "pending_count": len(pending),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "shadow_checked_count": len(shadows),
        "shadow_pending_count": len(shadow_pending),
        "shadow_skipped_count": len(shadow_skipped),
        "shadow_error_count": len(shadow_errors),
        "summary": summarize(records),
        "reconciliation": reconciliation,
        "shadow_summary": summarize(shadow_records),
        "records": records,
        "shadow_records": shadow_records,
        "pending": pending,
        "shadow_pending": shadow_pending,
        "skipped": skipped,
        "shadow_skipped": shadow_skipped,
        "errors": errors,
        "shadow_errors": shadow_errors,
        "notes": [
            "Read-only audit phase; no orders are placed or modified.",
            "Win/loss comes from Kalshi market settlement fields only.",
            "CLV uses the latest available pre-close candidate-ranker consensus snapshot.",
        ],
    }
    ctx.write_json("settlement_audit.json", payload)
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
