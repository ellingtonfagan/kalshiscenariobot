"""Phase: live-execute. Submit one gated real-money Kalshi order."""
from __future__ import annotations

from dataclasses import asdict

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..execution import build_order_request, execute_live
from ..research import ResearchStore
from ..risk import RiskContext, evaluate_trade_intent
from .base import Context, load_context
from .paper import _candidate_intents


LIVE_ACK = "LIVE_TRADES_REAL_MONEY"


def _blocked_reason(ctx: Context) -> str | None:
    if ctx.settings.execution_mode != "live":
        return "set NBABOT_EXECUTION_MODE=live"
    if ctx.settings.dry_run:
        return "set NBABOT_DRY_RUN=0"
    if getattr(ctx.settings, "live_trading_ack", "") != LIVE_ACK:
        return f"set NBABOT_LIVE_TRADING_ACK={LIVE_ACK}"
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
    intents = _candidate_intents(ctx)
    if not intents:
        audit.log("LIVE_NO_CANDIDATES", {"game_id": ctx.settings.game_id}, ctx.settings.game_id)
        deliver("[live-execute] no candidates; run snapshot-market first", ctx.settings.deliver_to)
        return {"orders": [], "reason": "no-candidates"}

    exposure = store.game_order_exposure_units("live_orders", ctx.settings.game_id)
    for intent in intents:
        if store.order_exists("live_orders", build_order_request(intent, "live").client_order_id):
            continue
        decision = evaluate_trade_intent(
            intent,
            ctx.settings,
            RiskContext(game_exposure_units=exposure),
        )
        receipt = execute_live(intent, decision, ctx.settings, store, audit, ctx.kalshi)
        result = {"intent": asdict(intent), "decision": asdict(decision), "receipt": asdict(receipt)}
        ctx.write_json("live_execute.json", result)
        hope = " HOPE BET" if intent.hope_bet else ""
        out = (
            f"[live-execute] {receipt.status}: {intent.scenario_id} {intent.ticker} "
            f"{intent.contracts} {intent.side.upper()} @ {intent.price_cents}c "
            f"stake={intent.stake_units:.3f}u edge={intent.edge:+.3f} "
            f"SGP-adjusted scenario p={intent.sgp_adjusted_prob:.3f}{hope}"
        )
        deliver(guardrails.with_footer(out), ctx.settings.deliver_to)
        return result

    return {"orders": [], "reason": "no-approved"}
