"""Phase: order-reconcile. Refresh demo order statuses, late fills, and maker cancels."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..order_reconciliation import reconcile_demo_orders
from ..research import ResearchStore
from .base import Context, load_context


def _format(payload: dict) -> str:
    fill_rate = payload.get("fill_rate") or {}
    buckets = fill_rate.get("buckets") or []
    bucket_text = "; ".join(
        (
            f"{row.get('signal_source')}:{row.get('liquidity_role')} "
            f"placed={row.get('placed', 0)} filled={row.get('filled', 0)} "
            f"canceled_unfilled={row.get('canceled_unfilled', 0)}"
        )
        for row in buckets[:4]
    ) or "none"
    return (
        f"[order-reconcile] checked={payload.get('checked_count', 0)} "
        f"fills_inserted={payload.get('fills_inserted_count', 0)} "
        f"canceled={payload.get('canceled_count', 0)} "
        f"errors={len(payload.get('errors') or [])} fill_rate={bucket_text}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    if ctx.settings.execution_mode != "demo":
        payload = {
            "checked_count": 0,
            "fills_inserted_count": 0,
            "canceled_count": 0,
            "errors": [],
            "reason": "order reconciliation is demo-only in this phase",
        }
        ctx.write_json("order_reconcile.json", payload)
        deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
        return payload
    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    payload = reconcile_demo_orders(ctx, store, audit)
    ctx.write_json("order_reconcile.json", payload)
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
