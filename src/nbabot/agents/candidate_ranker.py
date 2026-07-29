"""Phase: candidate-ranker. Compute model-vs-executable edge for matched markets."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..candidate_ranker import build_candidate_rankings, select_near_miss_candidates
from ..odds_refresh import artifact_freshness, refresh_if_stale
from ..qual_learning import format_calibration_lines
from ..qual_research import news_for_prompt, run_near_miss_investigations
from ..research import ResearchStore
from ..research_news import load_research_teams
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
    active_basket = ctx.read_json("active_market_basket.json") or {}
    basket_tickers = {
        str(ticker)
        for ticker in (active_basket.get("tickers") or [])
        if str(ticker)
    }
    if basket_tickers:
        matches = {
            **matches,
            "rows": [
                row for row in matches.get("rows", [])
                if str(row.get("ticker") or "") in basket_tickers
            ],
        }
    tickers = [
        str(row.get("ticker"))
        for row in matches.get("rows", [])
        if isinstance(row, dict) and row.get("ticker")
    ]
    qual_signals = ResearchStore(ctx.settings.research_db_path).latest_qual_signals(
        max_age_hours=getattr(ctx.settings, "qual_signal_max_age_hours", 12),
        tickers=tickers,
    )
    store = ResearchStore(ctx.settings.research_db_path)
    payload = build_candidate_rankings(ctx, slate, matches, qual_signals=qual_signals)
    near_misses = select_near_miss_candidates(payload.get("rows", []), ctx.settings)
    investigations: list[dict] = []
    investigation_inserts = 0
    if near_misses:
        try:
            teams = load_research_teams(ctx.settings.research_teams_path)
            team_keys = sorted({
                team
                for row in near_misses
                for team in (row.get("teams") or [])
                if team
            }) or [team.key for team in teams]
            news_rows = store.recent_news_items(
                team_keys,
                window_hours=ctx.settings.news_window_hours,
            )
            prompt_news = news_for_prompt(news_rows, teams)
            calibration = store.qual_calibration_table()
            investigations = run_near_miss_investigations(
                command=ctx.settings.qual_llm_cmd,
                news_items=prompt_news,
                candidates=near_misses,
                timeout_seconds=ctx.settings.qual_llm_timeout_seconds,
                calibration_lines=format_calibration_lines(calibration),
                fallback_model=getattr(ctx.settings, "qual_fallback_model", "claude-opus-4-8"),
            ).get("investigations", [])
        except Exception as exc:
            investigations = [{
                "status": "unavailable",
                "reason": f"near-miss-investigation-error:{exc.__class__.__name__}",
            }]
        investigation_inserts = store.record_near_miss_investigations(ctx.settings.game_id, investigations)
        verdicts = {
            str(row.get("ticker")): row
            for row in investigations
            if isinstance(row, dict) and row.get("ticker")
        }
        if verdicts:
            payload = build_candidate_rankings(
                ctx,
                slate,
                matches,
                qual_signals=qual_signals,
                qaq_verdicts=verdicts,
            )
    payload["near_miss_count"] = len(near_misses)
    payload["near_miss_investigations"] = investigations
    payload["near_miss_investigations_inserted"] = investigation_inserts
    payload["confluence_records_inserted"] = store.record_confluence_records(
        ctx.settings.game_id,
        payload.get("rows", []),
    )
    payload["live_updates"] = live_updates
    payload["qual_signal_count"] = len(qual_signals)
    if basket_tickers:
        payload["market_basket"] = active_basket
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
