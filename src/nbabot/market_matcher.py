"""Kalshi-first market matching and order-book deviation analysis."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .market_identity import match_identities, resolve_kalshi_market, resolve_line_market
from .odds_refresh import artifact_freshness, freshness_report
from .research import ResearchStore, utc_now

STOP_TOKENS = {
    "yes", "no", "over", "under", "reg", "time", "scored", "wins", "by",
    "more", "than", "goals", "runs", "points", "advances", "goal", "diff",
    "both", "teams", "to", "score", "the", "and",
}


def _tokens(text: Any) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if len(token) > 2 and token not in STOP_TOKENS
    }


def _num(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _latest_book_metrics(ctx: Any) -> dict[str, dict[str, Any]]:
    payload = ctx.read_json("book_watch.json") or {}
    snapshots = payload.get("snapshots") or []
    if not snapshots:
        return {}
    return snapshots[-1].get("metrics") or {}


def _previous_book_metrics(ctx: Any, current: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not current:
        return {}
    store = ResearchStore(ctx.settings.research_db_path)
    store.init_schema()
    tickers = set(current)
    previous: dict[str, dict[str, Any]] = {}
    seen_current: set[str] = set()
    with store.connect() as db:
        rows = db.execute(
            """
            SELECT ticker, metrics_json
            FROM orderbook_snapshots
            WHERE game_id = ?
            ORDER BY id DESC
            LIMIT 2000
            """,
            (ctx.settings.game_id,),
        ).fetchall()
    for row in rows:
        ticker = row["ticker"]
        if ticker not in tickers:
            continue
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if ticker not in seen_current:
            seen_current.add(ticker)
            continue
        if ticker not in previous:
            previous[ticker] = metrics
        if len(previous) == len(tickers):
            break
    return previous


def _side(metrics: dict[str, Any], side: str = "yes") -> dict[str, Any]:
    value = metrics.get(side) if isinstance(metrics, dict) else None
    return value if isinstance(value, dict) else {}


def _book_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    curr = _side(current, "yes")
    prev = _side(previous, "yes")
    fields = {
        "best_bid_cents": "best_bid_delta_cents",
        "best_ask_cents": "best_ask_delta_cents",
        "spread_cents": "spread_delta_cents",
        "top_bid_contracts": "top_bid_delta_contracts",
        "top_ask_contracts": "top_ask_delta_contracts",
        "depth_at_limit_contracts": "depth_delta_contracts",
        "fillable_contracts": "fillable_delta_contracts",
        "vwap_cents": "vwap_delta_cents",
    }
    out: dict[str, Any] = {"has_previous": bool(prev)}
    for src, dest in fields.items():
        c = _num(curr.get(src))
        p = _num(prev.get(src))
        out[dest] = round(c - p, 4) if c is not None and p is not None else None
    out["signals"] = []
    if out["best_bid_delta_cents"] is not None and out["best_bid_delta_cents"] > 0:
        out["signals"].append("bid_rising")
    if out["best_ask_delta_cents"] is not None and out["best_ask_delta_cents"] < 0:
        out["signals"].append("ask_falling")
    if out["spread_delta_cents"] is not None and out["spread_delta_cents"] < 0:
        out["signals"].append("spread_tightening")
    if out["fillable_delta_contracts"] is not None and out["fillable_delta_contracts"] > 0:
        out["signals"].append("fillable_depth_improving")
    if not out["signals"]:
        out["signals"].append("no_positive_orderbook_change" if prev else "baseline_snapshot")
    return out


def _sort_spread(row: dict[str, Any]) -> float:
    orderbook = row.get("orderbook") if isinstance(row, dict) else {}
    yes = orderbook.get("yes", {}) if isinstance(orderbook, dict) else {}
    spread = _num(yes.get("spread_cents")) if isinstance(yes, dict) else None
    return spread if spread is not None else 999.0


def _slate_markets(slate: dict[str, Any], sport_handoff: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for candidate in slate.get("candidates") or []:
        for market in candidate.get("kalshi_markets") or []:
            ticker = market.get("ticker")
            if not ticker:
                continue
            rows.setdefault(ticker, {
                **market,
                "candidate_id": candidate.get("candidate_id"),
                "sport": candidate.get("sport"),
                "structured_sources": candidate.get("structured_sources") or [],
                "line_markets": candidate.get("line_markets") or [],
            })
    for row in sport_handoff.get("rows") or []:
        ticker = row.get("ticker")
        if not ticker:
            continue
        rows.setdefault(ticker, row)
        rows[ticker].setdefault("candidate_id", row.get("candidate_id"))
        rows[ticker].setdefault("sport", row.get("sport"))
    return rows


def _external_pool(slate: dict[str, Any]) -> list[dict[str, Any]]:
    pool = []
    for candidate in slate.get("candidates") or []:
        sources = set(candidate.get("sources") or [])
        if "kalshi-open-markets" in sources and len(sources) <= 2:
            continue
        text_parts = [
            candidate.get("away_team"),
            candidate.get("home_team"),
            candidate.get("sport"),
        ]
        for line in candidate.get("line_markets") or []:
            text_parts.extend([
                line.get("market"),
                line.get("name"),
                line.get("point"),
                line.get("price"),
            ])
        pool.append({
            "candidate_id": candidate.get("candidate_id"),
            "sources": candidate.get("sources") or [],
            "structured_sources": candidate.get("structured_sources") or [],
            "text": " ".join(str(part) for part in text_parts if part is not None),
            "line_count": len(candidate.get("line_markets") or []),
        })
    return pool


def _best_external_matches(market: dict[str, Any], pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    market_tokens = _tokens(" ".join([
        str(market.get("title") or ""),
        " ".join(str(part) for part in (market.get("components") or [])),
    ]))
    if not market_tokens:
        return []
    matches = []
    for item in pool:
        item_tokens = _tokens(item.get("text"))
        overlap = sorted(market_tokens & item_tokens)
        if not overlap:
            continue
        score = len(overlap) / math.sqrt(max(len(market_tokens), 1) * max(len(item_tokens), 1))
        if score < 0.12:
            continue
        matches.append({
            "candidate_id": item.get("candidate_id"),
            "score": round(score, 4),
            "overlap_tokens": overlap[:12],
            "sources": item.get("sources"),
            "structured_sources": item.get("structured_sources"),
            "line_count": item.get("line_count"),
            "match_type": "fuzzy",
        })
    return sorted(matches, key=lambda row: row["score"], reverse=True)[:5]


def _candidate_by_id(slate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        candidate.get("candidate_id"): candidate
        for candidate in slate.get("candidates") or []
        if candidate.get("candidate_id")
    }


def _identity_pool(slate: dict[str, Any]) -> list[dict[str, Any]]:
    pool = []
    for candidate in slate.get("candidates") or []:
        for line in candidate.get("line_markets") or []:
            pool.append({
                "candidate_id": candidate.get("candidate_id"),
                "line": line,
                "identity": resolve_line_market(line, candidate),
                "sources": candidate.get("sources") or [],
                "structured_sources": candidate.get("structured_sources") or [],
                "line_count": len(candidate.get("line_markets") or []),
            })
    return pool


def _identity_external_matches(
    market: dict[str, Any],
    slate: dict[str, Any],
    pool: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidate = candidates.get(market.get("candidate_id"), {})
    identity = resolve_kalshi_market(market, candidate)
    matches = []
    for match in match_identities(identity, pool):
        candidate_id = match.get("candidate_id")
        candidate_doc = candidates.get(candidate_id, {})
        matches.append({
            "candidate_id": candidate_id,
            "score": match.get("score"),
            "match_type": match.get("match_type"),
            "identity": match.get("identity"),
            "sources": candidate_doc.get("sources") or [],
            "structured_sources": candidate_doc.get("structured_sources") or [],
            "line_count": len(candidate_doc.get("line_markets") or []),
        })
    return matches


def build_market_matches(ctx: Any) -> dict[str, Any]:
    slate = ctx.read_json("slate_candidates.json") or {"candidates": []}
    sport_handoff = ctx.read_json("sport_market_candidates.json") or {"rows": []}
    input_freshness = artifact_freshness(ctx, (
        "slate_candidates.json",
        "sport_market_candidates.json",
        "book_watch.json",
    ))
    current_books = _latest_book_metrics(ctx)
    previous_books = _previous_book_metrics(ctx, current_books)
    markets = _slate_markets(slate, sport_handoff)
    external = _external_pool(slate)
    candidates = _candidate_by_id(slate)
    identity_pool = _identity_pool(slate)
    rows = []
    for ticker, market in markets.items():
        current = current_books.get(ticker, {})
        previous = previous_books.get(ticker, {})
        yes = _side(current, "yes")
        spread = yes.get("spread_cents", market.get("spread_cents"))
        fillable = yes.get("fillable_contracts")
        book_report = freshness_report(
            ctx,
            "book_watch.json",
            {"captured_at": yes.get("captured_at")},
        )
        external_matches = _identity_external_matches(market, slate, identity_pool, candidates)
        if not external_matches:
            external_matches = _best_external_matches(market, external)
        blockers = []
        for report in input_freshness.values():
            if not report["fresh"]:
                blockers.append(f"{report['artifact']} {report['reason']}")
        if not book_report["fresh"]:
            blockers.append(f"{ticker} orderbook {book_report['reason']}")
        if not current:
            blockers.append("missing order-book snapshot")
        if spread is None:
            blockers.append("missing order-book spread")
        elif int(spread) > int(ctx.settings.max_spread_cents):
            blockers.append("spread wider than configured maximum")
        if fillable is None or int(fillable or 0) <= 0:
            blockers.append("no fillable YES depth at observed limit")
        if not external_matches:
            blockers.append("no comparable sportsbook/API market matched yet")
        blockers.extend([
            "candidate-ranker edge model required",
            "missing SGP-adjusted probability",
            "risk gate has not approved",
        ])
        inputs_fresh = all(report["fresh"] for report in input_freshness.values())
        execution_review = (
            inputs_fresh
            and book_report["fresh"]
            and bool(current)
            and spread is not None
            and int(spread) <= int(ctx.settings.max_spread_cents)
        )
        rows.append({
            "ticker": ticker,
            "event_ticker": market.get("event_ticker"),
            "series_ticker": market.get("series_ticker"),
            "candidate_id": market.get("candidate_id"),
            "sport": market.get("sport"),
            "title": market.get("title"),
            "components": market.get("components") or [],
            "custom_strike": market.get("custom_strike"),
            "strike_type": market.get("strike_type"),
            "floor_strike": market.get("floor_strike"),
            "cap_strike": market.get("cap_strike"),
            "mve_selected_legs": market.get("mve_selected_legs") or [],
            "mve_collection_ticker": market.get("mve_collection_ticker"),
            "is_composite": market.get("is_composite"),
            "yes_sub_title": market.get("yes_sub_title"),
            "no_sub_title": market.get("no_sub_title"),
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
            "close_time": market.get("close_time"),
            "captured_at": yes.get("captured_at") or market.get("captured_at"),
            "kalshi_quote": {
                "bid": market.get("bid"),
                "ask": market.get("ask"),
                "mid": market.get("mid"),
                "implied": market.get("implied"),
                "spread_cents": market.get("spread_cents"),
            },
            "orderbook": current,
            "orderbook_delta": _book_delta(current, previous),
            "freshness": {
                "inputs": input_freshness,
                "orderbook": book_report,
            },
            "external_matches": external_matches,
            "execution_review": execution_review,
            "trade_eligible": False,
            "blockers": blockers,
        })
    rows.sort(key=lambda row: (
        not row["execution_review"],
        _sort_spread(row),
        -(row["kalshi_quote"].get("bid") or 0),
    ))
    execution_rows = [
        row for row in rows
        if row["execution_review"]
    ]
    return {
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "input_freshness": input_freshness,
        "candidate_count": len(rows),
        "execution_review_count": len(execution_rows),
        "trade_eligible_count": 0,
        "rows": rows,
        "execution_slate": execution_rows,
        "notes": [
            "market-matcher is Kalshi-first and orderbook-aware",
            "execution_review is not approval to trade",
            "trade eligibility still requires model probability, SGP-adjusted probability, and risk.py approval",
        ],
    }
