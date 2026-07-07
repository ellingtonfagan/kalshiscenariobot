"""Phase: candidate-ranker. Compute model-vs-executable edge for matched markets."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..candidate_ranker import build_candidate_rankings
from ..odds_refresh import artifact_freshness, refresh_if_stale
from ..research import ResearchStore
from .base import Context, load_context


def _format(payload: dict) -> str:
    return (
        f"[candidate-ranker] {payload['game_id']}: candidates={payload.get('candidate_count', 0)} "
        f"edge_pass={payload.get('edge_pass_count', 0)} "
        f"trade_eligible={payload.get('trade_eligible_count', 0)}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    from . import market_matcher, slate_discovery

    live_updates = {
        "slate_candidates": refresh_if_stale(
            ctx,
            "slate_candidates.json",
            slate_discovery.run,
        ),
        "market_matches": refresh_if_stale(
            ctx,
            "market_matches.json",
            market_matcher.run,
        ),
    }
    slate = ctx.read_json("slate_candidates.json") or {"candidates": []}
    matches = ctx.read_json("market_matches.json") or {"rows": []}
    tickers = [
        str(row.get("ticker"))
        for row in matches.get("rows", [])
        if isinstance(row, dict) and row.get("ticker")
    ]
    qual_signals = ResearchStore(ctx.settings.research_db_path).latest_qual_signals(
        max_age_hours=getattr(ctx.settings, "qual_signal_max_age_hours", 12),
        tickers=tickers,
    )
    payload = build_candidate_rankings(ctx, slate, matches, qual_signals=qual_signals)
    payload["live_updates"] = live_updates
    payload["qual_signal_count"] = len(qual_signals)
    payload["input_freshness"] = artifact_freshness(ctx, (
        "slate_candidates.json",
        "market_matches.json",
        "book_watch.json",
        "qual_signals.json",
    ))
    ctx.write_json("candidate_ranker.json", payload)
    ctx.write_json("match_coverage.json", {
        "game_id": payload["game_id"],
        "source": "candidate-ranker",
        "generated_at": payload["generated_at"],
        **((payload.get("diagnostics") or {}).get("match_coverage") or {}),
    })
    ctx.write_json("edge_candidates.json", {
        "game_id": payload["game_id"],
        "source": "candidate-ranker",
        "generated_at": payload["generated_at"],
        "rows": [
            row for row in payload.get("rows", [])
            if row.get("passes_edge")
        ],
    })
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
