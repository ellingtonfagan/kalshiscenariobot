"""Phase: market-matcher. Match Kalshi slate markets to books and order-book deltas."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..market_matcher import build_market_matches
from ..odds_refresh import artifact_freshness, refresh_if_stale
from .base import Context, load_context


def _format(payload: dict) -> str:
    return (
        f"[market-matcher] {payload['game_id']}: candidates={payload.get('candidate_count', 0)} "
        f"execution_review={payload.get('execution_review_count', 0)} "
        f"trade_eligible={payload.get('trade_eligible_count', 0)}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    from . import book_watch, slate_discovery

    live_updates = {
        "slate_candidates": refresh_if_stale(
            ctx,
            "slate_candidates.json",
            slate_discovery.run,
        ),
        "sport_market_candidates": refresh_if_stale(
            ctx,
            "sport_market_candidates.json",
            slate_discovery.run,
        ),
        "book_watch": refresh_if_stale(
            ctx,
            "book_watch.json",
            book_watch.run,
        ),
    }
    payload = build_market_matches(ctx)
    payload["live_updates"] = live_updates
    payload["input_freshness"] = artifact_freshness(ctx, (
        "slate_candidates.json",
        "sport_market_candidates.json",
        "book_watch.json",
    ))
    ctx.write_json("market_matches.json", payload)
    ctx.write_json("execution_slate.json", {
        "game_id": payload["game_id"],
        "source": "market-matcher",
        "generated_at": payload["generated_at"],
        "input_freshness": payload["input_freshness"],
        "rows": payload.get("execution_slate", []),
    })
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
