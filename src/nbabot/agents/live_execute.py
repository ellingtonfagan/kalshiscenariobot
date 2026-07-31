"""Phase: live-execute. Submit one gated real-money Kalshi order."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..execution import OrderReceipt, build_order_request, execute_live
from ..research import ResearchStore
from .base import Context, load_context
from .paper import _candidate_intents, execute_intent_batch, refresh_research_for_execution


LIVE_ACK = "LIVE_TRADES_REAL_MONEY"
BROAD_SLATE_ACK = "BROAD_SLATE_TRADES_REAL_MONEY"


def _blocked_reason(ctx: Context, intent: object | None = None) -> str | None:
    if ctx.settings.execution_mode != "live":
        return "set NBABOT_EXECUTION_MODE=live"
    if ctx.settings.dry_run:
        return "set NBABOT_DRY_RUN=0"
    if getattr(ctx.settings, "live_trading_ack", "") != LIVE_ACK:
        return f"set NBABOT_LIVE_TRADING_ACK={LIVE_ACK}"
    if (
        intent is not None
        and getattr(intent, "signal_source", "consensus") in {"qual", "qual_activated_quant"}
    ):
        return "qual/QAQ-sourced intents are hard-blocked in live mode"
    if (
        intent is not None
        and getattr(intent, "broad_slate", False)
        and getattr(ctx.settings, "broad_slate_execution", "") != BROAD_SLATE_ACK
    ):
        return f"set NBABOT_BROAD_SLATE_EXECUTION={BROAD_SLATE_ACK}"
    return None


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    blocked = _blocked_reason(ctx)
    if blocked:
        msg = f"[live-execute] blocked: {blocked}"
        deliver(msg, ctx.settings.deliver_to)
        return {"reason": "mode-blocked", "detail": blocked}

    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    refresh_research_for_execution(ctx)
    intents = _candidate_intents(ctx)
    if not intents:
        audit.log("LIVE_NO_CANDIDATES", {"game_id": ctx.settings.game_id}, ctx.settings.game_id)
        deliver("[live-execute] no candidates; run snapshot-market first", ctx.settings.deliver_to)
        return {"orders": [], "reason": "no-candidates"}

    live_intents = []
    for intent in intents:
        blocked = _blocked_reason(ctx, intent)
        if blocked and "NBABOT_BROAD_SLATE_EXECUTION" in blocked:
            msg = f"[live-execute] blocked: {blocked}"
            deliver(msg, ctx.settings.deliver_to)
            return {"reason": "mode-blocked", "detail": blocked}
        if store.order_exists("live_orders", build_order_request(intent, "live").client_order_id):
            continue
        live_intents.append(intent)
    if not live_intents:
        return {"orders": [], "reason": "no-approved"}

    result = execute_intent_batch(
        ctx=ctx,
        store=store,
        audit=audit,
        intents=live_intents,
        table="live_orders",
        mode="live",
        executor=lambda intent, decision: _execute_live_or_reject(ctx, store, audit, intent, decision),
        artifact="live_execute.json",
    )
    first = result["orders"][0]
    intent = first["intent"]
    receipt = first["receipt"]
    hope = " HOPE BET" if intent["hope_bet"] else ""
    out = (
        f"[live-execute] {result['attempted_count']} attempted, "
        f"{result['approved_count']} approved; first {receipt['status']}: "
        f"{intent['scenario_id']} {intent['ticker']} "
        f"{intent['contracts']} {intent['side'].upper()} @ {intent['price_cents']}c "
        f"stake={intent['stake_units']:.3f}u net_edge={intent['edge']:+.3f} "
        f"SGP-adjusted scenario p={intent['sgp_adjusted_prob']:.3f}{hope}"
    )
    deliver(guardrails.with_footer(out), ctx.settings.deliver_to)
    return result


def _execute_live_or_reject(ctx: Context, store: ResearchStore, audit: AuditTrail,
                            intent: object, decision: object) -> OrderReceipt:
    blocked = _blocked_reason(ctx, intent)
    if blocked:
        request = build_order_request(intent, "live")
        receipt = OrderReceipt(
            request.client_order_id,
            "live",
            "rejected",
            {"reasons": [blocked]},
        )
        audit.log(
            "LIVE_REJECTED",
            {
                "signal_source": getattr(intent, "signal_source", "consensus"),
                "confluence_verdict": getattr(intent, "confluence_verdict", "none"),
                "client_order_id": receipt.client_order_id,
                "mode": receipt.mode,
                "status": receipt.status,
                "response": receipt.response,
            },
            ctx.settings.game_id,
        )
        return receipt
    return execute_live(intent, decision, ctx.settings, store, audit, ctx.kalshi)
