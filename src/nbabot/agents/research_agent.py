"""Phase: research-agent. Build evidence-backed market candidates."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..odds_refresh import artifact_freshness, refresh_if_stale
from ..slate import research_bundle
from .base import Context, load_context


def _format(payload: dict) -> str:
    return (
        f"[research-agent] {payload['game_id']}: market_candidates={payload.get('candidate_count', 0)} "
        f"trade_eligible={payload.get('trade_eligible_count', 0)}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    from . import market_matcher, slate_verify, snapshot_market

    live_updates = {
        "market_snapshot": refresh_if_stale(
            ctx,
            "market_snapshot.json",
            snapshot_market.run,
        ),
        "slate_verification": refresh_if_stale(
            ctx,
            "slate_verification.json",
            slate_verify.run,
        ),
        "market_matches": refresh_if_stale(
            ctx,
            "market_matches.json",
            market_matcher.run,
        ),
    }
    slate = ctx.read_json("slate_candidates.json") or {"candidates": []}
    verification = ctx.read_json("slate_verification.json") or {"rows": []}
    payload = research_bundle(ctx, slate, verification)
    payload["live_updates"] = live_updates
    payload["input_freshness"] = artifact_freshness(ctx, (
        "market_snapshot.json",
        "slate_candidates.json",
        "slate_verification.json",
        "book_watch.json",
        "market_matches.json",
    ))
    ctx.write_json("research_bundle.json", payload)
    ctx.write_json("market_candidates.json", {
        "game_id": payload["game_id"],
        "source": "research-agent",
        "rows": [
            row for row in payload.get("market_candidates", [])
            if row.get("trade_eligible")
        ],
    })
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
