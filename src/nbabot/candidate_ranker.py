"""Rank matched Kalshi markets by model-vs-executable-price edge."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .confluence import VETO_REASON, evaluate_confluence
from .edge_engine import ExecutablePrice, evaluate_market
from .market_identity import (
    MarketIdentity,
    closest_identity_rejection,
    match_diagnostics,
    match_identities,
    resolve_kalshi_market,
    resolve_kalshi_legs,
    resolve_line_market,
    same_event_key,
)
from .odds_math import consensus_prob
from .research import utc_now
from .slate import STRUCTURED_BOOK_PROVIDERS


COMPOSITE_TRADE_BLOCKER = "composite/multi-leg Kalshi markets require validated performance track record"
EDGE_BELOW_BLOCKER = "edge below dynamic required edge"
QAQ_SIGNAL_SOURCE = "qual_activated_quant"
QAQ_MIN_NET_EDGE = 0.005
SIDES = ("yes", "no")


def _execution_min_edge(settings: Any) -> float:
    if hasattr(settings, "execution_min_edge"):
        return float(settings.execution_min_edge)
    mode = str(getattr(settings, "execution_mode", "paper") or "paper").lower()
    if mode in {"demo", "paper"}:
        return float(getattr(settings, "demo_min_edge", getattr(settings, "min_edge", 0.05)))
    return float(getattr(settings, "min_edge", 0.05))


def _qual_min_edge(settings: Any) -> float:
    return float(getattr(settings, "qual_min_edge", 0.06))


def _has_consensus(consensus: dict[str, Any]) -> bool:
    return (
        consensus.get("fair_prob") is not None
        and int(consensus.get("book_count") or 0) >= 2
    )


def _qual_signal_for(row: dict[str, Any], qual_signals: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    if not qual_signals:
        return None
    signal = qual_signals.get(str(row.get("ticker") or ""))
    return signal if isinstance(signal, dict) else None


def _qaq_verdict_for(row: dict[str, Any], qaq_verdicts: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    if not qaq_verdicts:
        return None
    verdict = qaq_verdicts.get(str(row.get("ticker") or ""))
    return verdict if isinstance(verdict, dict) else None


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def _qaq_eligible_verdict(verdict: dict[str, Any] | None) -> bool:
    if not isinstance(verdict, dict):
        return False
    citations = [url for url in verdict.get("citation_urls") or [] if str(url).strip()]
    try:
        confidence = float(verdict.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        _truthy(verdict.get("justified"))
        and _truthy(verdict.get("direction_consistent"))
        and confidence >= 0.60
        and bool(citations)
    )


def _qaq_required_edge(required: float, settings: Any) -> float:
    try:
        bonus = float(getattr(settings, "qaq_floor_bonus", 0.015))
    except (TypeError, ValueError):
        bonus = 0.015
    return round(max(float(required) - max(bonus, 0.0), QAQ_MIN_NET_EDGE), 5)


def _with_qaq_evidence(
    *,
    payload: dict[str, Any],
    verdict: dict[str, Any],
    consensus: dict[str, Any],
    executable: ExecutablePrice,
    row: dict[str, Any],
    match: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    net_edge = payload.get("net_edge")
    required = payload.get("required_edge")
    if net_edge is None or required is None or not _qaq_eligible_verdict(verdict):
        payload["qaq_investigation"] = verdict
        return payload
    lowered_required = _qaq_required_edge(float(required), settings)
    blockers = [b for b in payload.get("blockers") or [] if b != EDGE_BELOW_BLOCKER]
    if float(net_edge) < lowered_required:
        blockers.append(EDGE_BELOW_BLOCKER)
    evidence = {
        "news_item_ids": verdict.get("news_item_ids") or [],
        "first_seen_at": verdict.get("first_seen_at"),
        "source": verdict.get("source"),
        "investigation": verdict,
        "consensus_prob": consensus.get("fair_prob"),
        "kalshi_price_cents": executable.price_cents,
        "kalshi_price_prob": executable.price_prob,
        "gap": round(float(required) - float(net_edge), 6),
        "basket_id": row.get("basket_id"),
        "event_ids": [
            value for value in (
                row.get("event_ticker"),
                row.get("candidate_id"),
                match.get("candidate_id"),
            )
            if value
        ],
    }
    payload.update({
        "signal_source": QAQ_SIGNAL_SOURCE,
        "required_edge": lowered_required,
        "blockers": blockers,
        "passes_edge": not blockers,
        "qaq_investigation": verdict,
        "qaq_evidence": evidence,
        "signal_engine": verdict.get("engine") or "codex",
    })
    return payload


def _candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        not item.get("passes_edge"),
        -(item.get("net_edge") if item.get("net_edge") is not None else item.get("edge") if item.get("edge") is not None else -999),
        item.get("required_edge", 999),
    )


def select_near_miss_candidates(
    rows: list[dict[str, Any]],
    settings: Any,
) -> list[dict[str, Any]]:
    try:
        window = float(getattr(settings, "near_miss_window", 0.02))
    except (TypeError, ValueError):
        window = 0.02
    try:
        cap = int(getattr(settings, "near_miss_investigations_per_cycle", 5))
    except (TypeError, ValueError):
        cap = 5
    selected: list[dict[str, Any]] = []
    if window <= 0 or cap <= 0:
        return selected
    for row in rows:
        if row.get("passes_edge"):
            continue
        if str(row.get("signal_source") or "consensus") != "consensus":
            continue
        if str(row.get("match_type") or "") != "exact":
            continue
        blockers = set(row.get("blockers") or [])
        if EDGE_BELOW_BLOCKER not in blockers:
            continue
        if blockers - {EDGE_BELOW_BLOCKER}:
            continue
        net_edge = row.get("net_edge")
        required = row.get("required_edge")
        if net_edge is None or required is None:
            continue
        gap = round(float(required) - float(net_edge), 6)
        if 0 < gap <= window:
            selected.append({
                **row,
                "near_miss_gap": gap,
            })
    selected.sort(key=lambda item: (item["near_miss_gap"], -(item.get("net_edge") or -999)))
    return selected[:cap]


def _candidate_by_id(slate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        candidate.get("candidate_id"): candidate
        for candidate in slate.get("candidates") or []
        if candidate.get("candidate_id")
    }


def _line_identity_pool(slate: dict[str, Any]) -> list[dict[str, Any]]:
    pool = []
    for candidate in slate.get("candidates") or []:
        for line in candidate.get("line_markets") or []:
            identity = resolve_line_market(line, candidate)
            pool.append({
                "candidate_id": candidate.get("candidate_id"),
                "line": line,
                "identity": identity,
            })
    return pool


def _slate_input_diagnostics(slate: dict[str, Any]) -> dict[str, Any]:
    candidates = slate.get("candidates") or []
    with_lines = 0
    structured_count = 0
    kalshi_only = 0
    external_event_keys = 0
    for candidate in candidates:
        if candidate.get("line_markets"):
            with_lines += 1
        sources = set(candidate.get("structured_sources") or [])
        has_structured = bool(sources & STRUCTURED_BOOK_PROVIDERS)
        if has_structured:
            structured_count += 1
        if "kalshi" in sources and not has_structured:
            kalshi_only += 1
        if any(resolve_line_market(line, candidate).event_key for line in candidate.get("line_markets") or []):
            external_event_keys += 1
    return {
        "slate_candidate_count": len(candidates),
        "candidates_with_line_markets": with_lines,
        "candidates_with_structured_source": structured_count,
        "kalshi_only_candidates": kalshi_only,
        "external_candidates_with_event_key": external_event_keys,
    }


def _empty_match_diagnostics() -> dict[str, Any]:
    return {
        "exact": 0,
        "fuzzy": 0,
        "none": 0,
        "kalshi_event_key_resolved": 0,
        "line_event_key_resolved_pool_rows": 0,
        "composite_markets": 0,
        "composite_full_matches": 0,
        "composite_partial_matches": 0,
        "composite_unsupported": 0,
        "composite_legs_total": 0,
        "composite_legs_supported": 0,
        "composite_legs_exact": 0,
        "composite_legs_with_consensus": 0,
        "composite_trade_blocked": 0,
        "none_mismatch_breakdown": {},
        "fuzzy_mismatch_breakdown": {},
        "match_coverage": {
            "unmatched_candidate_count": 0,
            "closest_rejection_field_counts": {},
            "examples": [],
        },
        "confluence": {
            "overlap_count": 0,
            "agree_count": 0,
            "disagree_count": 0,
            "neutral_count": 0,
            "boost_count": 0,
            "veto_count": 0,
            "deltas": [],
        },
    }


def _merge_counts(target: dict[str, int], source: dict[str, Any]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value or 0)


def _compact_line(line: Any) -> dict[str, Any] | None:
    if not isinstance(line, dict):
        return None
    keys = (
        "provider",
        "book",
        "book_title",
        "market",
        "name",
        "point",
        "price",
        "side_id",
        "odd_id",
        "last_update",
    )
    return {key: line.get(key) for key in keys if line.get(key) not in (None, "")}


def _record_unmatched_coverage(
    diagnostics: dict[str, Any],
    *,
    ticker: str,
    match_type: str,
    kalshi_identity: MarketIdentity,
    row_diag: dict[str, Any],
) -> None:
    coverage = diagnostics.setdefault("match_coverage", {
        "unmatched_candidate_count": 0,
        "closest_rejection_field_counts": {},
        "examples": [],
    })
    coverage["unmatched_candidate_count"] = int(coverage.get("unmatched_candidate_count") or 0) + 1
    closest = row_diag.get("closest_rejection") if isinstance(row_diag, dict) else None
    if not isinstance(closest, dict):
        return
    fields = [
        str(field)
        for field in (closest.get("differing_fields") or [])
        if field
    ]
    counts = coverage.setdefault("closest_rejection_field_counts", {})
    for field in fields:
        counts[field] = int(counts.get(field) or 0) + 1
    examples = coverage.setdefault("examples", [])
    if len(examples) >= 8:
        return
    examples.append({
        "ticker": ticker,
        "match_type": match_type,
        "differing_fields": fields,
        "kalshi_identity": kalshi_identity.as_dict(),
        "sportsbook_candidate_id": closest.get("candidate_id"),
        "sportsbook_identity": closest.get("identity"),
        "sportsbook_line": _compact_line(closest.get("line")),
        "score": closest.get("score"),
    })


def _same_consensus_market(a: MarketIdentity, b: MarketIdentity) -> bool:
    if a.event_key and b.event_key and not same_event_key(a.event_key, b.event_key):
        return False
    if a.market_type != b.market_type:
        return False
    if a.market_type == "spread":
        if a.line is None or b.line is None:
            return a.line is None and b.line is None
        if a.side and b.side and a.side != b.side:
            return abs(a.line + b.line) <= 0.01
        return abs(a.line - b.line) <= 0.01
    if a.line is None or b.line is None:
        return a.line is None and b.line is None
    return abs(a.line - b.line) <= 0.01


def _consensus_rows(
    slate: dict[str, Any],
    identity: MarketIdentity,
    candidate_id: str | None,
) -> list[dict[str, Any]]:
    rows = []
    candidates = slate.get("candidates") or []
    for candidate in candidates:
        if candidate_id and candidate.get("candidate_id") != candidate_id:
            continue
        for line in candidate.get("line_markets") or []:
            line_identity = resolve_line_market(line, candidate)
            if _same_consensus_market(identity, line_identity):
                rows.append(line)
    return rows


def _kalshi_market_from_row(row: dict[str, Any]) -> dict[str, Any]:
    quote = row.get("kalshi_quote") or {}
    return {
        "ticker": row.get("ticker"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "title": row.get("title"),
        "sport": row.get("sport"),
        "components": row.get("components"),
        "custom_strike": row.get("custom_strike"),
        "mve_selected_legs": row.get("mve_selected_legs") or [],
        "mve_collection_ticker": row.get("mve_collection_ticker"),
        "is_composite": row.get("is_composite"),
        "yes_sub_title": row.get("yes_sub_title"),
        "no_sub_title": row.get("no_sub_title"),
        "rules_primary": row.get("rules_primary"),
        "rules_secondary": row.get("rules_secondary"),
        "strike_type": row.get("strike_type"),
        "floor_strike": row.get("floor_strike"),
        "cap_strike": row.get("cap_strike"),
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
        "mid": quote.get("mid"),
        "implied": quote.get("implied"),
        "spread_cents": quote.get("spread_cents"),
        "close_time": row.get("close_time"),
    }


def _generic_composite_haircut(legs: list[dict[str, Any]]) -> float:
    """Conservative generic SGP haircut until settlement learning can calibrate it."""
    leg_count = len(legs)
    if leg_count <= 1:
        return 1.0
    event_keys = [
        leg.get("identity", {}).get("event_key")
        for leg in legs
        if isinstance(leg.get("identity"), dict)
    ]
    same_event = len(set(event_keys)) < len(event_keys)
    per_extra_leg = 0.82 if same_event else 0.92
    return round(max(per_extra_leg ** (leg_count - 1), 0.25), 5)


def _leg_payload_identity(leg: dict[str, Any]) -> dict[str, Any] | None:
    identity = leg.get("identity")
    if isinstance(identity, MarketIdentity):
        return identity.as_dict()
    if isinstance(identity, dict):
        return identity
    return None


def _composite_match_and_consensus(
    slate: dict[str, Any],
    kalshi_market: dict[str, Any],
    identity_pool: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    raw_legs = resolve_kalshi_legs(kalshi_market)
    if not raw_legs:
        return None
    leg_rows = []
    exact_count = 0
    supported_count = 0
    consensus_count = 0
    leg_probs = []
    source_set: set[str] = set()
    excluded_set: set[str] = set()
    disagreements = []
    book_counts = []
    candidate_ids = []
    for leg in raw_legs:
        identity = leg.get("identity")
        leg_payload: dict[str, Any] = {
            **{key: value for key, value in leg.items() if key != "identity"},
            "identity": identity.as_dict() if isinstance(identity, MarketIdentity) else None,
            "match_type": "unsupported",
            "candidate_id": None,
            "consensus": None,
            "leg_prob": None,
        }
        if not isinstance(identity, MarketIdentity):
            leg_rows.append(leg_payload)
            continue
        supported_count += 1
        matches = match_identities(identity, identity_pool)
        match = matches[0] if matches else None
        if match:
            leg_payload["match_type"] = str(match.get("match_type") or "none")
            leg_payload["candidate_id"] = match.get("candidate_id")
            leg_payload["score"] = match.get("score")
            if match.get("candidate_id"):
                candidate_ids.append(match["candidate_id"])
        else:
            leg_payload["match_type"] = "none"
        if match and match.get("match_type") == "exact":
            exact_count += 1
            consensus_rows = _consensus_rows(slate, identity, match.get("candidate_id"))
            consensus = consensus_prob(consensus_rows, target_name=identity.side or identity.participant)
            leg_payload["consensus"] = consensus
            fair = consensus.get("fair_prob")
            if fair is not None:
                leg_prob = 1.0 - float(fair) if leg.get("leg_side") == "no" else float(fair)
                leg_prob = min(max(leg_prob, 0.0), 1.0)
                leg_payload["leg_prob"] = round(leg_prob, 6)
                leg_probs.append(leg_prob)
                consensus_count += 1
                source_set.update(consensus.get("sources") or [])
                excluded_set.update(consensus.get("excluded_books") or [])
                if consensus.get("disagreement_std") is not None:
                    disagreements.append(float(consensus["disagreement_std"]))
                book_counts.append(int(consensus.get("book_count") or 0))
        leg_rows.append(leg_payload)

    full_match = bool(raw_legs) and exact_count == len(raw_legs)
    partial_match = exact_count > 0 and not full_match
    raw_joint = math.prod(leg_probs) if len(leg_probs) == len(raw_legs) else None
    haircut = _generic_composite_haircut(leg_rows)
    adjusted_joint = raw_joint * haircut if raw_joint is not None else None
    match_type = "exact" if full_match else "fuzzy" if partial_match else "none"
    representative_identity = {
        "sport": kalshi_market.get("sport"),
        "league": str(kalshi_market.get("sport") or "").upper() or None,
        "event_key": None,
        "market_type": "composite",
        "line": None,
        "side": "yes",
        "start_date": None,
        "participant": None,
        "legs": [_leg_payload_identity(leg) for leg in leg_rows],
    }
    return (
        {
            "candidate_id": candidate_ids[0] if candidate_ids else None,
            "line": None,
            "identity": representative_identity,
            "match_type": match_type,
            "score": round(exact_count / len(raw_legs), 4) if raw_legs else 0.0,
            "composite_leg_count": len(raw_legs),
            "composite_supported_leg_count": supported_count,
            "composite_exact_leg_count": exact_count,
            "composite_consensus_leg_count": consensus_count,
            "sgp_adjusted": adjusted_joint is not None,
        },
        {
            "fair_prob": round(adjusted_joint, 6) if adjusted_joint is not None else None,
            "book_count": min(book_counts) if book_counts else 0,
            "sources": sorted(source_set),
            "excluded_books": sorted(excluded_set),
            "disagreement_std": max(disagreements) if disagreements else None,
            "raw_joint_prob": round(raw_joint, 6) if raw_joint is not None else None,
            "sgp_adjusted_prob": round(adjusted_joint, 6) if adjusted_joint is not None else None,
            "sgp_haircut": haircut,
            "leg_count": len(raw_legs),
            "exact_leg_count": exact_count,
            "consensus_leg_count": consensus_count,
        },
        {
            "compared": len(identity_pool),
            "composite": True,
            "leg_count": len(raw_legs),
            "supported_leg_count": supported_count,
            "exact_leg_count": exact_count,
            "consensus_leg_count": consensus_count,
            "full_match": full_match,
            "partial_match": partial_match,
            "legs": leg_rows,
            "mismatch_breakdown": {},
        },
    )


def build_candidate_rankings(
    ctx: Any,
    slate: dict[str, Any],
    market_matches: dict[str, Any],
    *,
    now: datetime | None = None,
    qual_signals: dict[str, dict[str, Any]] | None = None,
    qaq_verdicts: dict[str, dict[str, Any]] | None = None,
    liquidity_role: str = "maker",
) -> dict[str, Any]:
    candidate_lookup = _candidate_by_id(slate)
    identity_pool = _line_identity_pool(slate)
    diagnostics = {
        **_slate_input_diagnostics(slate),
        **_empty_match_diagnostics(),
    }
    diagnostics["line_identity_pool_rows"] = len(identity_pool)
    diagnostics["line_event_key_resolved_pool_rows"] = sum(
        1 for item in identity_pool
        if isinstance(item.get("identity"), MarketIdentity) and item["identity"].event_key
    )
    base_min_edge = _execution_min_edge(ctx.settings)
    ranked = []
    for row in market_matches.get("rows") or []:
        ticker = row.get("ticker")
        if not ticker:
            continue
        candidate = candidate_lookup.get(row.get("candidate_id"), {})
        kalshi_market = _kalshi_market_from_row(row)
        identity = resolve_kalshi_market(kalshi_market, candidate)
        if identity.event_key:
            diagnostics["kalshi_event_key_resolved"] += 1
        composite = _composite_match_and_consensus(slate, kalshi_market, identity_pool)
        if composite is not None:
            match, consensus, row_diag = composite
            diagnostics["composite_markets"] += 1
            diagnostics["composite_legs_total"] += int(row_diag.get("leg_count") or 0)
            diagnostics["composite_legs_supported"] += int(row_diag.get("supported_leg_count") or 0)
            diagnostics["composite_legs_exact"] += int(row_diag.get("exact_leg_count") or 0)
            diagnostics["composite_legs_with_consensus"] += int(row_diag.get("consensus_leg_count") or 0)
            if row_diag.get("full_match"):
                diagnostics["composite_full_matches"] += 1
            elif row_diag.get("partial_match"):
                diagnostics["composite_partial_matches"] += 1
            if int(row_diag.get("supported_leg_count") or 0) < int(row_diag.get("leg_count") or 0):
                diagnostics["composite_unsupported"] += 1
            diagnostics[str(match.get("match_type") or "none")] += 1
            if match.get("match_type") != "exact":
                closest = None
                for leg in row_diag.get("legs") or []:
                    leg_identity = leg.get("identity") if isinstance(leg, dict) else None
                    if not isinstance(leg_identity, dict):
                        continue
                    leg_obj = MarketIdentity(**leg_identity)
                    rejection = closest_identity_rejection(leg_obj, identity_pool)
                    if rejection and (closest is None or rejection.get("score", 0) > closest.get("score", 0)):
                        closest = rejection
                row_diag["closest_rejection"] = closest
                _record_unmatched_coverage(
                    diagnostics,
                    ticker=ticker,
                    match_type=str(match.get("match_type") or "none"),
                    kalshi_identity=identity,
                    row_diag=row_diag,
                )
        else:
            identity_matches = match_identities(identity, identity_pool)
            if identity_matches:
                match = identity_matches[0]
                diagnostics[str(match.get("match_type") or "none")] += 1
            else:
                fallback = (row.get("external_matches") or [{}])[0]
                match = {
                    "candidate_id": fallback.get("candidate_id"),
                    "line": None,
                    "identity": None,
                    "match_type": "fuzzy" if fallback else "none",
                    "score": fallback.get("score"),
                }
                diagnostics[match["match_type"]] += 1
            row_diag = match_diagnostics(
                identity,
                identity_pool,
                include_closest=match.get("match_type") != "exact",
            )
            if match.get("match_type") == "none":
                _merge_counts(diagnostics["none_mismatch_breakdown"], row_diag["mismatch_breakdown"])
                _record_unmatched_coverage(
                    diagnostics,
                    ticker=ticker,
                    match_type="none",
                    kalshi_identity=identity,
                    row_diag=row_diag,
                )
            elif match.get("match_type") == "fuzzy":
                _merge_counts(diagnostics["fuzzy_mismatch_breakdown"], row_diag["mismatch_breakdown"])
                _record_unmatched_coverage(
                    diagnostics,
                    ticker=ticker,
                    match_type="fuzzy",
                    kalshi_identity=identity,
                    row_diag=row_diag,
                )
            consensus_rows = (
                _consensus_rows(
                    slate,
                    identity,
                    match.get("candidate_id"),
                )
                if match.get("match_type") in {"exact", "fuzzy"}
                and match.get("candidate_id")
                else []
            )
            target = identity.side or identity.participant
            consensus = consensus_prob(consensus_rows, target_name=target)
        maker = str(liquidity_role or "maker").lower() != "taker"
        signal_source = "consensus"
        qual_signal = _qual_signal_for(row, qual_signals)
        qual_delta = None
        if qual_signal is not None and consensus.get("fair_prob") is not None:
            try:
                qual_delta = round(float(qual_signal["qual_prob"]) - float(consensus["fair_prob"]), 6)
            except (TypeError, ValueError):
                qual_delta = None
        eval_consensus = consensus
        eval_base_min_edge = base_min_edge
        require_consensus = True
        exact_consensus_match = bool(match.get("match_type") == "exact" and _has_consensus(consensus))
        yes_executable = ExecutablePrice.from_orderbook_metrics(
            row.get("orderbook") or {},
            "yes",
            maker=maker,
        )
        confluence = evaluate_confluence(
            consensus=consensus,
            qual_signal=qual_signal,
            price_prob=yes_executable.price_prob,
            settings=ctx.settings,
            base_min_edge=base_min_edge,
            exact_match=exact_consensus_match,
        )
        if confluence.consensus_fair_prob is not None and confluence.qual_prob is not None:
            confluence_diag = diagnostics["confluence"]
            confluence_diag["overlap_count"] += 1
            confluence_diag["deltas"].append(confluence.delta)
            if confluence.verdict == "agree":
                confluence_diag["agree_count"] += 1
            elif confluence.verdict == "disagree":
                confluence_diag["disagree_count"] += 1
            elif confluence.verdict == "neutral":
                confluence_diag["neutral_count"] += 1
            if confluence.edge_bonus > 0:
                confluence_diag["boost_count"] += 1
            if confluence.veto:
                confluence_diag["veto_count"] += 1
        if not _has_consensus(consensus) and qual_signal is not None:
            signal_source = "qual"
            eval_base_min_edge = _qual_min_edge(ctx.settings)
            eval_consensus = {
                "fair_prob": qual_signal.get("qual_prob"),
                "book_count": 0,
                "sources": qual_signal.get("citation_urls") or [],
                "providers": ["qual_research"],
                "excluded_books": [],
                "disagreement_std": None,
                "qual_confidence": qual_signal.get("confidence"),
                "qual_rationale": qual_signal.get("rationale"),
                "qual_model_run_id": qual_signal.get("model_run_id"),
                "qual_created_at": qual_signal.get("created_at"),
            }
            require_consensus = False
        side_payloads = []
        side_economics: dict[str, dict[str, Any]] = {}
        for side in SIDES:
            executable = ExecutablePrice.from_orderbook_metrics(
                row.get("orderbook") or {},
                side,
                maker=maker,
            )
            edge = evaluate_market(
                {
                    **match,
                    "ticker": ticker,
                    "close_time": row.get("close_time"),
                },
                eval_consensus,
                executable,
                ctx.settings,
                base_min_edge=eval_base_min_edge,
                now=now,
                require_consensus=require_consensus,
                require_exact_match=signal_source != "qual",
            )
            side_payload = edge.as_dict()
            side_payload["side"] = side
            side_payload["yes_model_prob"] = eval_consensus.get("fair_prob")
            side_payload["no_model_prob"] = (
                round(1.0 - float(eval_consensus["fair_prob"]), 6)
                if eval_consensus.get("fair_prob") is not None else None
            )
            side_payloads.append(side_payload)
            side_economics[side] = {
                key: side_payload.get(key)
                for key in (
                    "side",
                    "model_prob",
                    "executable_price",
                    "raw_edge",
                    "net_edge",
                    "edge",
                    "expected_fee_cents",
                    "expected_fee_prob",
                    "liquidity_role",
                    "required_edge",
                    "passes_edge",
                    "blockers",
                )
            }
        payload = sorted(side_payloads, key=_candidate_sort_key)[0]
        chosen_signal_source = signal_source
        would_pass_before_confluence = bool(payload["passes_edge"])
        confluence_shadow = False
        qaq_verdict = _qaq_verdict_for(row, qaq_verdicts)
        chosen_executable = ExecutablePrice.from_orderbook_metrics(
            row.get("orderbook") or {},
            str(payload.get("side") or "yes"),
            maker=maker,
        )
        if signal_source == "consensus" and qaq_verdict is not None and not confluence.veto:
            payload = _with_qaq_evidence(
                payload=payload,
                verdict=qaq_verdict,
                consensus=consensus,
                executable=chosen_executable,
                row=row,
                match=match,
                settings=ctx.settings,
            )
            chosen_signal_source = str(payload.get("signal_source") or chosen_signal_source)
            would_pass_before_confluence = bool(payload["passes_edge"])
        if signal_source == "consensus" and confluence.veto and would_pass_before_confluence:
            if VETO_REASON not in payload["blockers"]:
                payload["blockers"].append(VETO_REASON)
            payload["passes_edge"] = False
            confluence_shadow = True
        is_composite = composite is not None
        if is_composite:
            diagnostics["composite_trade_blocked"] += 1
            if COMPOSITE_TRADE_BLOCKER not in payload["blockers"]:
                payload["blockers"].append(COMPOSITE_TRADE_BLOCKER)
            payload["passes_edge"] = False
        side_economics[str(payload.get("side") or "yes")] = {
            **side_economics.get(str(payload.get("side") or "yes"), {}),
            "passes_edge": payload.get("passes_edge"),
            "blockers": payload.get("blockers"),
            "required_edge": payload.get("required_edge"),
            "signal_source": payload.get("signal_source", chosen_signal_source),
        }
        payload.update({
            "ticker": ticker,
            "candidate_id": row.get("candidate_id"),
            "market_identity": identity.as_dict(),
            "identity_match": {
                **match,
                "identity": match.get("identity"),
            },
            "identity_diagnostics": row_diag,
            "consensus": consensus,
            "qual_signal": qual_signal,
            "qual_vs_consensus_delta": qual_delta,
            "signal_source": chosen_signal_source,
            "side_economics": side_economics,
            "side_evaluation_count": len(side_payloads),
            "chosen_side": payload.get("side"),
            "confluence": confluence.as_dict(),
            "confluence_verdict": confluence.verdict if chosen_signal_source == "consensus" else "none",
            "confluence_shadow": confluence_shadow,
            "sgp_adjusted_prob": consensus.get("sgp_adjusted_prob"),
            "raw_joint_prob": consensus.get("raw_joint_prob"),
            "sgp_haircut": consensus.get("sgp_haircut"),
            "orderbook_delta": row.get("orderbook_delta"),
            "freshness": row.get("freshness"),
            "is_composite": is_composite,
            "trade_eligible": payload["passes_edge"],
            "source": "candidate-ranker",
        })
        if chosen_signal_source == "qual":
            payload["sgp_adjusted_prob"] = payload.get("model_prob")
            payload["model_prob_sources"] = list((qual_signal or {}).get("citation_urls") or [])
            payload["confluence_verdict"] = "none"
        ranked.append(payload)
    ranked.sort(key=_candidate_sort_key)
    return {
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "candidate_count": len(ranked),
        "edge_pass_count": sum(1 for row in ranked if row.get("passes_edge")),
        "trade_eligible_count": sum(1 for row in ranked if row.get("trade_eligible")),
        "signal_source_counts": {
            "consensus": sum(1 for row in ranked if row.get("signal_source") == "consensus"),
            "qual": sum(1 for row in ranked if row.get("signal_source") == "qual"),
            "qual_activated_quant": sum(1 for row in ranked if row.get("signal_source") == "qual_activated_quant"),
        },
        "confluence_counts": {
            "overlap": diagnostics["confluence"]["overlap_count"],
            "agree": diagnostics["confluence"]["agree_count"],
            "disagree": diagnostics["confluence"]["disagree_count"],
            "neutral": diagnostics["confluence"]["neutral_count"],
            "boost": diagnostics["confluence"]["boost_count"],
            "veto": diagnostics["confluence"]["veto_count"],
        },
        "diagnostics": diagnostics,
        "rows": ranked,
        "notes": [
            "candidate-ranker computes model probability from de-vigged sportsbook consensus",
            "fuzzy identity matches are never allowed to pass edge checks",
            "live execution still requires explicit live gates and risk.py approval",
        ],
    }
