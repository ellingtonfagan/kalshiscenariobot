"""Phase: demo-execute. Submit one gated order to Kalshi demo only."""
from __future__ import annotations

from dataclasses import asdict

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..execution import execute_demo
from ..research import ResearchStore
from ..risk import RiskContext, evaluate_trade_intent
from .base import Context, load_context
from .paper import _candidate_intents


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    if ctx.settings.execution_mode != "demo":
        msg = "[demo-execute] blocked: set NBABOT_EXECUTION_MODE=demo to use Kalshi demo"
        deliver(msg, ctx.settings.deliver_to)
        return {"reason": "mode-blocked"}

    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    intents = _candidate_intents(ctx)
    if not intents:
        audit.log("DEMO_NO_CANDIDATES", {"game_id": ctx.settings.game_id}, ctx.settings.game_id)
        deliver("[demo-execute] no candidates; run snapshot-market first", ctx.settings.deliver_to)
        return {"orders": [], "reason": "no-candidates"}

    for intent in intents:
        decision = evaluate_trade_intent(intent, ctx.settings, RiskContext())
        receipt = execute_demo(intent, decision, ctx.settings, store, audit, ctx.kalshi)
        result = {"intent": asdict(intent), "decision": asdict(decision), "receipt": asdict(receipt)}
        ctx.write_json("demo_execute.json", result)
        hope = " HOPE BET" if intent.hope_bet else ""
        out = (
            f"[demo-execute] {receipt.status}: {intent.scenario_id} {intent.ticker} "
            f"{intent.contracts} {intent.side.upper()} @ {intent.price_cents}c "
            f"stake={intent.stake_units:.3f}u edge={intent.edge:+.3f} "
            f"SGP-adjusted scenario p={intent.sgp_adjusted_prob:.3f}{hope}"
        )
        deliver(guardrails.with_footer(out), ctx.settings.deliver_to)
        return result

    return {"orders": [], "reason": "no-approved"}
