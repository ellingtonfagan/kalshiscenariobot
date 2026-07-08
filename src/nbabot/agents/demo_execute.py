"""Phase: demo-execute. Submit one gated order to Kalshi demo only."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..execution import execute_demo
from ..kalshi import DEMO_CREDENTIALS_BLOCKED_REASON
from ..research import ResearchStore
from ..risk import RiskContext, evaluate_trade_intent
from .base import Context, load_context
from .paper import (
    _candidate_intents,
    execution_limits,
    record_shadow_intents,
    refresh_research_for_execution,
)


def _blocked_reason(ctx: Context) -> str | None:
    if ctx.settings.execution_mode != "demo":
        return "set NBABOT_EXECUTION_MODE=demo to use Kalshi demo"
    if not str(getattr(ctx.settings, "kalshi_demo_api_key", "") or "").strip():
        return DEMO_CREDENTIALS_BLOCKED_REASON
    demo_key_path = getattr(ctx.settings, "kalshi_demo_private_key_path", None)
    if demo_key_path is None or not Path(demo_key_path).is_file():
        return DEMO_CREDENTIALS_BLOCKED_REASON
    return None


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    blocked = _blocked_reason(ctx)
    if blocked:
        msg = f"[demo-execute] blocked: {blocked}"
        deliver(msg, ctx.settings.deliver_to)
        return {"reason": "mode-blocked", "detail": blocked}

    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    refresh_research_for_execution(ctx)
    shadow_inserted = record_shadow_intents(ctx, store, audit, mode="demo")
    intents = _candidate_intents(ctx)
    if not intents:
        audit.log("DEMO_NO_CANDIDATES", {"game_id": ctx.settings.game_id}, ctx.settings.game_id)
        deliver("[demo-execute] no candidates; run snapshot-market first", ctx.settings.deliver_to)
        return {
            "orders": [],
            "reason": "no-candidates",
            "shadow_intents_inserted": shadow_inserted,
        }

    limits = execution_limits(ctx, store, "demo_orders")
    for intent in intents:
        decision = evaluate_trade_intent(
            intent,
            ctx.settings,
            RiskContext(
                game_exposure_units=float(limits["game_exposure_units"]),
                portfolio_exposure_units=float(limits["portfolio_exposure_units"]),
                broad_slate_trade_count=int(limits["broad_slate_trade_count"]),
                broad_slate_daily_trade_limit=int(limits["broad_slate_daily_trade_limit"]),
                paper_demo_broad_slate_trade_count=int(
                    limits["paper_demo_broad_slate_trade_count"]
                ),
                paper_demo_daily_trade_cap=int(limits["paper_demo_daily_trade_cap"]),
                qual_daily_trade_count=int(limits["qual_daily_trade_count"]),
                qual_daily_trade_cap=int(limits["qual_daily_trade_cap"]),
            ),
        )
        receipt = execute_demo(intent, decision, ctx.settings, store, audit, ctx.kalshi)
        result = {
            "intent": asdict(intent),
            "decision": asdict(decision),
            "receipt": asdict(receipt),
            "shadow_intents_inserted": shadow_inserted,
        }
        ctx.write_json("demo_execute.json", result)
        hope = " HOPE BET" if intent.hope_bet else ""
        out = (
            f"[demo-execute] {receipt.status}: {intent.scenario_id} {intent.ticker} "
            f"{intent.contracts} {intent.side.upper()} @ {intent.price_cents}c "
            f"stake={intent.stake_units:.3f}u net_edge={intent.edge:+.3f} "
            f"SGP-adjusted scenario p={intent.sgp_adjusted_prob:.3f}{hope}"
        )
        deliver(guardrails.with_footer(out), ctx.settings.deliver_to)
        return result

    return {"orders": [], "reason": "no-approved"}
