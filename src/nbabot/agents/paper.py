"""Phase: paper. Create local paper fills for approved single-leg intents."""
from __future__ import annotations

from dataclasses import asdict

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..confluence import VETO_REASON
from ..execution import TradeIntent, client_order_id, execute_paper
from ..research import ResearchStore
from ..risk import (
    RiskContext,
    broad_slate_daily_trade_limit,
    evaluate_trade_intent,
)
from ..sizing import capped_kelly
from ..slate import configured_game_candidate_id
from .base import Context, load_context


def _execution_min_edge(settings: object) -> float:
    if hasattr(settings, "execution_min_edge"):
        return float(getattr(settings, "execution_min_edge"))
    mode = str(getattr(settings, "execution_mode", "paper") or "paper").lower()
    if mode in {"paper", "demo"}:
        return float(getattr(settings, "demo_min_edge", getattr(settings, "min_edge", 0.05)))
    return float(getattr(settings, "min_edge", 0.05))


def _row_signal_source(row: dict, meta: dict) -> str:
    return str(row.get("signal_source") or meta.get("signal_source") or "consensus")


def _row_min_edge(settings: object, signal_source: str) -> float:
    if signal_source == "qual":
        return float(getattr(settings, "qual_min_edge", 0.06))
    return _execution_min_edge(settings)


def _row_confluence(row: dict, meta: dict) -> dict:
    confluence = meta.get("confluence")
    if not isinstance(confluence, dict):
        confluence = row.get("confluence")
    return confluence if isinstance(confluence, dict) else {}


def _row_confluence_verdict(row: dict, meta: dict) -> str:
    return str(
        meta.get("confluence_verdict")
        or row.get("confluence_verdict")
        or "none"
    )


def _effective_row_min_edge(settings: object, signal_source: str, row: dict, meta: dict) -> float:
    if signal_source == "qual_activated_quant":
        required = row.get("required_edge")
        if required is None:
            required = meta.get("required_edge")
        if required is not None:
            return float(required)
    return _row_min_edge(settings, signal_source)


def _candidate_is_broad_slate(row: dict, settings: object) -> bool:
    if "broad_slate" in row:
        return bool(row.get("broad_slate"))
    candidate_id = row.get("candidate_id")
    if not candidate_id:
        return False
    configured_id = configured_game_candidate_id(settings)
    return not configured_id or str(candidate_id) != configured_id


def _paper_demo_mode(settings: object) -> bool:
    mode = str(getattr(settings, "execution_mode", "paper") or "paper").lower()
    return mode in {"paper", "demo"}


def execution_limits(ctx: Context, store: ResearchStore, table: str) -> dict[str, float | int]:
    research = ctx.read_json("research_bundle.json") or {}
    learning = research.get("performance_learning") or {}
    return {
        "game_exposure_units": store.game_order_exposure_units(table, ctx.settings.game_id),
        "portfolio_exposure_units": store.daily_order_exposure_units(table),
        "broad_slate_trade_count": store.daily_order_count(table, broad_slate_only=True),
        "broad_slate_daily_trade_limit": broad_slate_daily_trade_limit(learning),
        "paper_demo_broad_slate_trade_count": store.daily_order_count(
            ("paper_orders", "demo_orders"),
            broad_slate_only=True,
        ),
        "paper_demo_daily_trade_cap": int(getattr(
            ctx.settings,
            "paper_demo_daily_trade_cap",
            50,
        )),
        "qual_daily_trade_count": store.daily_order_count(
            ("paper_orders", "demo_orders"),
            signal_source="qual",
        ) + store.daily_order_count(
            ("paper_orders", "demo_orders"),
            signal_source="qual_activated_quant",
        ),
        "qual_daily_trade_cap": int(getattr(
            ctx.settings,
            "qual_daily_trade_cap",
            10,
        )),
    }


def _intent_from_row(
    ctx: Context,
    row: dict,
    meta: dict,
    group_counts: dict[str, int],
    unit_cents: int,
) -> TradeIntent | None:
    implied = row.get("implied")
    ticker = row.get("ticker")
    entry = row.get("entry_price_cents")
    captured_at = row.get("captured_at")
    prior = row.get("prior_p")
    if prior is None:
        prior = row.get("model_prob")
    if implied is None or prior is None or not ticker or not entry or not captured_at:
        return None

    group = str(
        meta.get("event_key")
        or meta.get("market_family")
        or meta.get("candidate_id")
        or row.get("scenario_id")
        or ticker
    )
    raw_edge = (
        float(row.get("raw_edge"))
        if row.get("raw_edge") is not None else
        float(prior) - float(implied)
    )
    net_edge = (
        float(row.get("net_edge"))
        if row.get("net_edge") is not None else
        float(row.get("edge"))
        if row.get("edge") is not None else
        raw_edge
    )
    edge = net_edge
    research_override = bool(row.get("research_override", False))
    signal_source = _row_signal_source(row, meta)
    min_edge = _effective_row_min_edge(ctx.settings, signal_source, row, meta)
    if edge < min_edge and not research_override:
        return None
    if research_override:
        target_units = min(
            float(row.get("research_override_stake_units", 0.25)),
            float(getattr(ctx.settings, "research_override_max_units", 1.0)),
            1.0,
        )
        contracts = int(target_units * unit_cents) // int(entry)
        if contracts <= 0:
            return None
        stake_units = (contracts * int(entry)) / max(unit_cents, 1)
        side = row.get("side") or "yes"
        entry_price_cents = int(entry)
    else:
        validated = bool(meta.get("validated") or meta.get("market_type_verdict") == "validated")
        size = capped_kelly(
            edge=edge,
            market_prob=float(implied),
            entry_price_cents=int(entry),
            unit_cents=unit_cents,
            max_units=guardrails.MAX_STAKE_UNITS,
            min_edge=min_edge,
            validated=validated,
            correlation_group_size=group_counts.get(group, 1),
        )
        if size.contracts <= 0:
            broad_slate = _candidate_is_broad_slate(meta, ctx.settings)
            if not (_paper_demo_mode(ctx.settings) and broad_slate):
                return None
            contracts = 1
            entry_price_cents = int(entry)
            stake_units = entry_price_cents / max(unit_cents, 1)
            side = row.get("side") or size.side
        else:
            contracts = size.contracts
            stake_units = size.stake_units
            side = row.get("side") or size.side
            entry_price_cents = size.entry_price_cents

    risk = int(row.get("risk", 0) or 0)
    validated = bool(meta.get("validated") or meta.get("market_type_verdict") == "validated")
    qaq_evidence = (
        row.get("qaq_evidence")
        if isinstance(row.get("qaq_evidence"), dict) else
        meta.get("qaq_evidence")
        if isinstance(meta.get("qaq_evidence"), dict) else
        {}
    )
    return TradeIntent(
        game_id=ctx.settings.game_id,
        scenario_id=str(row.get("scenario_id") or meta.get("candidate_id") or ticker),
        ticker=ticker,
        action="buy",
        side=side,
        contracts=contracts,
        price_cents=entry_price_cents,
        stake_units=stake_units,
        model_prob=float(prior),
        market_prob=float(implied),
        edge=edge,
        raw_edge=raw_edge,
        net_edge=net_edge,
        required_edge=row.get("required_edge"),
        expected_fee_cents=row.get("expected_fee_cents"),
        expected_fee_prob=row.get("expected_fee_prob"),
        liquidity_role=str(row.get("liquidity_role") or meta.get("liquidity_role") or "maker"),
        sgp_adjusted_prob=row.get("sgp_adjusted_prob"),
        risk=risk,
        hope_bet=guardrails.is_hope_bet(risk),
        captured_at=captured_at,
        bid_cents=row.get("bid"),
        ask_cents=row.get("ask"),
        rationale=f"{row.get('label')} prior edge {edge:+.3f}",
        research_override=research_override,
        research_override_reason=str(row.get("research_override_reason", "")),
        research_sources=tuple(row.get("research_sources", ()) or ()),
        research_approved_by=str(row.get("research_approved_by", "")),
        candidate_id=meta.get("candidate_id"),
        event_key=meta.get("event_key"),
        market_family=meta.get("market_family"),
        performance=(
            meta.get("performance")
            if isinstance(meta.get("performance"), dict)
            else {}
        ),
        validated=validated,
        market_type_verdict=str(meta.get("market_type_verdict", "")),
        broad_slate=_candidate_is_broad_slate(meta, ctx.settings),
        signal_source=signal_source,
        confluence_verdict=_row_confluence_verdict(row, meta),
        confluence=_row_confluence(row, meta),
        basket_id=row.get("basket_id") or meta.get("basket_id"),
        event_ids=tuple(
            row.get("event_ids")
            or meta.get("event_ids")
            or qaq_evidence.get("event_ids")
            or ()
        ),
        news_item_ids=tuple(
            row.get("news_item_ids")
            or meta.get("news_item_ids")
            or qaq_evidence.get("news_item_ids")
            or ()
        ),
        signal_engine=row.get("signal_engine") or meta.get("signal_engine"),
        qaq_evidence=qaq_evidence,
    )


def _candidate_intents(ctx: Context) -> list[TradeIntent]:
    snap = ctx.read_json("market_snapshot.json") or {}
    rows = snap.get("rows", [])
    research = ctx.read_json("research_bundle.json") or {}
    research_gate_active = "market_candidates" in research
    research_candidates = research.get("market_candidates") or []
    allowed = {
        (row.get("ticker"), row.get("scenario_id"), row.get("market"))
        for row in research_candidates
        if row.get("trade_eligible")
    }
    allowed_meta = {
        (row.get("ticker"), row.get("scenario_id"), row.get("market")): row
        for row in research_candidates
        if row.get("trade_eligible")
    }
    group_counts: dict[str, int] = {}
    for row in research_candidates:
        if not row.get("trade_eligible"):
            continue
        group = str(
            row.get("event_key")
            or row.get("market_family")
            or row.get("candidate_id")
            or row.get("scenario_id")
            or row.get("ticker")
        )
        group_counts[group] = group_counts.get(group, 0) + 1
    intents: list[TradeIntent] = []
    intent_keys = set()
    unit_cents = int(round(ctx.settings.unit_usd * 100))
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        key = (ticker, row.get("scenario_id"), row.get("market"))
        if research_gate_active and key not in allowed:
            continue
        meta = allowed_meta.get(key, {})
        intent = _intent_from_row(ctx, row, meta, group_counts, unit_cents)
        if intent is None:
            continue
        intents.append(intent)
        intent_keys.add(key)
    for meta in research_candidates:
        if not meta.get("trade_eligible"):
            continue
        key = (meta.get("ticker"), meta.get("scenario_id"), meta.get("market"))
        if key in intent_keys:
            continue
        intent = _intent_from_row(ctx, meta, meta, group_counts, unit_cents)
        if intent is None:
            continue
        intents.append(intent)
        intent_keys.add(key)
    intents.sort(key=lambda i: i.edge or 0, reverse=True)
    return intents


def _candidate_group_counts(rows: list[dict]) -> dict[str, int]:
    group_counts: dict[str, int] = {}
    for row in rows:
        if not (row.get("trade_eligible") or row.get("confluence_shadow")):
            continue
        group = str(
            row.get("event_key")
            or row.get("market_family")
            or row.get("candidate_id")
            or row.get("scenario_id")
            or row.get("ticker")
        )
        group_counts[group] = group_counts.get(group, 0) + 1
    return group_counts


def _shadow_intents(ctx: Context) -> list[TradeIntent]:
    research = ctx.read_json("research_bundle.json") or {}
    research_candidates = [
        row for row in research.get("market_candidates") or []
        if row.get("confluence_shadow")
        and str(row.get("signal_source") or "consensus") == "consensus"
    ]
    group_counts = _candidate_group_counts(research.get("market_candidates") or [])
    unit_cents = int(round(ctx.settings.unit_usd * 100))
    intents = []
    for row in research_candidates:
        intent = _intent_from_row(ctx, row, row, group_counts, unit_cents)
        if intent is not None:
            intents.append(intent)
    return intents


def record_shadow_intents(
    ctx: Context,
    store: ResearchStore,
    audit: AuditTrail,
    *,
    mode: str,
) -> int:
    inserted = 0
    for intent in _shadow_intents(ctx):
        shadow_id = client_order_id(intent, f"{mode}-shadow")
        if store.record_shadow_intent(
            game_id=ctx.settings.game_id,
            mode=mode,
            client_order_id=shadow_id,
            intent=intent,
            reason=VETO_REASON,
        ):
            inserted += 1
            audit.log(
                "CONFLUENCE_SHADOW_INTENT",
                {
                    "client_order_id": shadow_id,
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "contracts": intent.contracts,
                    "price_cents": intent.price_cents,
                    "stake_units": intent.stake_units,
                    "edge": intent.edge,
                    "signal_source": intent.signal_source,
                    "confluence_verdict": intent.confluence_verdict,
                    "reason": VETO_REASON,
                },
                ctx.settings.game_id,
            )
    return inserted


def refresh_research_for_execution(ctx: Context) -> dict:
    from . import research_agent

    return research_agent.run(ctx)


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    refresh_research_for_execution(ctx)
    shadow_inserted = record_shadow_intents(ctx, store, audit, mode="paper")
    intents = _candidate_intents(ctx)
    if not intents:
        msg = "[paper] no approved candidates; run snapshot-market or lower NBABOT_MIN_EDGE after research"
        audit.log("PAPER_NO_CANDIDATES", {"game_id": ctx.settings.game_id}, ctx.settings.game_id)
        deliver(msg, ctx.settings.deliver_to)
        return {"orders": [], "reason": "no-candidates", "shadow_intents_inserted": shadow_inserted}

    limits = execution_limits(ctx, store, "paper_orders")
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
        receipt = execute_paper(intent, decision, ctx.settings, store, audit)
        receipts.append({"intent": asdict(intent), "decision": asdict(decision), "receipt": asdict(receipt)})
        if decision.approved:
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
        f"stake={intent['stake_units']:.3f}u net_edge={intent['edge']:+.3f} "
        f"SGP-adjusted scenario p={intent['sgp_adjusted_prob']:.3f}{hope}"
    )
    deliver(guardrails.with_footer(out), ctx.settings.deliver_to)
    return {"orders": receipts, "shadow_intents_inserted": shadow_inserted}
