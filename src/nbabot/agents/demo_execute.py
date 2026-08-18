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
    _failed_check_names,
    execution_limits,
    record_shadow_intents,
    refresh_research_for_execution,
)
from ..units import unit_payload


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
    game_exposure = float(limits["game_exposure_units"])
    portfolio_exposure = float(limits["portfolio_exposure_units"])
    broad_slate_count = int(limits["broad_slate_trade_count"])
    broad_slate_limit = int(limits["broad_slate_daily_trade_limit"])
    paper_demo_broad_slate_count = int(limits["paper_demo_broad_slate_trade_count"])
    paper_demo_daily_cap = int(limits["paper_demo_daily_trade_cap"])
    qual_daily_trade_count = int(limits["qual_daily_trade_count"])
    qual_daily_trade_cap = int(limits["qual_daily_trade_cap"])
    receipts = []
    for intent in intents:
        decision = evaluate_trade_intent(
            intent,
            ctx.settings,
            RiskContext(
                game_exposure_units=game_exposure,
                portfolio_exposure_units=portfolio_exposure,
                broad_slate_trade_count=broad_slate_count,
                broad_slate_daily_trade_limit=broad_slate_limit,
                paper_demo_broad_slate_trade_count=paper_demo_broad_slate_count,
                paper_demo_daily_trade_cap=paper_demo_daily_cap,
                qual_daily_trade_count=qual_daily_trade_count,
                qual_daily_trade_cap=qual_daily_trade_cap,
            ),
        )
        receipt = execute_demo(intent, decision, ctx.settings, store, audit, ctx.kalshi)
        receipts.append({
            "intent": asdict(intent),
            "decision": asdict(decision),
            "receipt": asdict(receipt),
        })
        if receipt.status in {"submitted", "filled"}:
            game_exposure += intent.stake_units
            portfolio_exposure += intent.stake_units
            if intent.broad_slate:
                broad_slate_count += 1
                paper_demo_broad_slate_count += 1
            if intent.signal_source in {"qual", "qual_activated_quant"}:
                qual_daily_trade_count += 1
        if (
            game_exposure >= ctx.settings.max_game_exposure_units
            or portfolio_exposure >= float(getattr(
                ctx.settings,
                "max_daily_exposure_units",
                guardrails.MAX_STAKE_UNITS,
            ))
        ):
            break

    store.record_risk_snapshot(ctx.settings.game_id, {
        "game_exposure_units": game_exposure,
        "portfolio_exposure_units": portfolio_exposure,
        "daily_pnl_units": 0.0,
        "open_positions": len([
            r for r in receipts
            if r["receipt"]["status"] in {"submitted", "filled"}
        ]),
        "circuit_breaker_on": False,
        "unit": unit_payload(ctx),
    })
    result = {"orders": receipts, "shadow_intents_inserted": shadow_inserted}
    ctx.write_json("demo_execute.json", result)
    if receipts:
        first = receipts[0]
        intent = first["intent"]
        receipt = first["receipt"]
        hope = " HOPE BET" if intent["hope_bet"] else ""
        out = (
            f"[demo-execute] {receipt['status']}: {intent['scenario_id']} {intent['ticker']} "
            f"{intent['contracts']} {intent['side'].upper()} @ {intent['price_cents']}c "
            f"stake={intent['stake_units']:.3f}u net_edge={intent['edge']:+.3f} "
            f"SGP-adjusted scenario p={intent['sgp_adjusted_prob']:.3f}{hope}"
        )
        failed = _failed_check_names(first["decision"])
        if failed:
            out += f" reasons={', '.join(failed)}"
        elif receipt.get("response", {}).get("reasons"):
            out += f" reasons={', '.join(receipt['response']['reasons'])}"
        deliver(guardrails.with_footer(out), ctx.settings.deliver_to)
    return result
