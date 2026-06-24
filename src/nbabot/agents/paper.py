"""Phase: paper. Create local paper fills for approved single-leg intents."""
from __future__ import annotations

from dataclasses import asdict

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..execution import TradeIntent, execute_paper
from ..research import ResearchStore
from ..risk import RiskContext, evaluate_trade_intent
from ..sizing import capped_kelly
from .base import Context, load_context


def _candidate_intents(ctx: Context) -> list[TradeIntent]:
    snap = ctx.read_json("market_snapshot.json") or {}
    rows = snap.get("rows", [])
    intents: list[TradeIntent] = []
    unit_cents = int(round(ctx.settings.unit_usd * 100))
    for row in rows:
        implied = row.get("implied")
        ticker = row.get("ticker")
        entry = row.get("entry_price_cents")
        if implied is None or not ticker or not entry:
            continue
        edge = float(row["prior_p"]) - float(implied)
        if edge < ctx.settings.min_edge:
            continue
        size = capped_kelly(
            edge=edge,
            market_prob=float(implied),
            entry_price_cents=int(entry),
            unit_cents=unit_cents,
            max_units=guardrails.MAX_STAKE_UNITS,
            min_edge=ctx.settings.min_edge,
        )
        if size.contracts <= 0:
            continue
        risk = int(row.get("risk", 0))
        intents.append(TradeIntent(
            game_id=ctx.settings.game_id,
            scenario_id=row["scenario_id"],
            ticker=ticker,
            action="buy",
            side=row.get("side") or size.side,
            contracts=size.contracts,
            price_cents=size.entry_price_cents,
            stake_units=size.stake_units,
            model_prob=float(row["prior_p"]),
            market_prob=float(implied),
            edge=edge,
            sgp_adjusted_prob=row.get("sgp_adjusted_prob"),
            risk=risk,
            hope_bet=guardrails.is_hope_bet(risk),
            captured_at=row["captured_at"],
            bid_cents=row.get("bid"),
            ask_cents=row.get("ask"),
            rationale=f"{row.get('label')} prior edge {edge:+.3f}",
        ))
    intents.sort(key=lambda i: i.edge or 0, reverse=True)
    return intents


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    intents = _candidate_intents(ctx)
    if not intents:
        msg = "[paper] no approved candidates; run snapshot-market or lower NBABOT_MIN_EDGE after research"
        audit.log("PAPER_NO_CANDIDATES", {"game_id": ctx.settings.game_id}, ctx.settings.game_id)
        deliver(msg, ctx.settings.deliver_to)
        return {"orders": [], "reason": "no-candidates"}

    exposure = 0.0
    receipts = []
    for intent in intents:
        decision = evaluate_trade_intent(intent, ctx.settings, RiskContext(game_exposure_units=exposure))
        receipt = execute_paper(intent, decision, ctx.settings, store, audit)
        receipts.append({"intent": asdict(intent), "decision": asdict(decision), "receipt": asdict(receipt)})
        if decision.approved:
            exposure += intent.stake_units
        if exposure >= ctx.settings.max_game_exposure_units:
            break

    store.record_risk_snapshot(ctx.settings.game_id, {
        "game_exposure_units": exposure,
        "daily_pnl_units": 0.0,
        "open_positions": len([r for r in receipts if r["decision"]["approved"]]),
        "circuit_breaker_on": False,
    })
    ctx.write_json("paper.json", {"orders": receipts})
    first = receipts[0]
    intent = first["intent"]
    status = first["receipt"]["status"]
    hope = " HOPE BET" if intent["hope_bet"] else ""
    out = (
        f"[paper] {status}: {intent['scenario_id']} {intent['ticker']} "
        f"{intent['contracts']} {intent['side'].upper()} @ {intent['price_cents']}c "
        f"stake={intent['stake_units']:.3f}u edge={intent['edge']:+.3f} "
        f"SGP-adjusted scenario p={intent['sgp_adjusted_prob']:.3f}{hope}"
    )
    deliver(guardrails.with_footer(out), ctx.settings.deliver_to)
    return {"orders": receipts}
