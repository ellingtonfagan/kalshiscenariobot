"""Phase: qual-research. Produce cited qualitative probabilities for unpriced rows."""
from __future__ import annotations

from ..alerts import deliver
from ..qual_research import news_for_prompt, run_qual_model, unpriced_team_market_rows
from ..research import ResearchStore
from ..research_news import load_research_teams
from .base import Context, load_context


def _format(payload: dict) -> str:
    reason = payload.get("reason")
    reason_text = f" reason={reason}" if reason else ""
    return (
        f"[qual-research] status={payload.get('status')} "
        f"markets={payload.get('market_count', 0)} news={payload.get('news_count', 0)} "
        f"accepted={payload.get('accepted_count', 0)} produced={payload.get('produced_count', 0)}"
        f"{reason_text}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    try:
        teams = load_research_teams(ctx.settings.research_teams_path)
    except Exception as exc:
        payload = {
            "status": "unavailable",
            "reason": f"team-config-error:{exc.__class__.__name__}",
            "market_count": 0,
            "news_count": 0,
            "produced_count": 0,
            "accepted_count": 0,
            "signals": [],
            "discarded": [],
        }
        ctx.write_json("qual_signals.json", payload)
        deliver(_format(payload), ctx.settings.deliver_to)
        return payload

    slate = ctx.read_json("slate_candidates.json") or {"candidates": []}
    matches = ctx.read_json("market_matches.json") or {"rows": []}
    markets = unpriced_team_market_rows(teams=teams, slate=slate, market_matches=matches)
    news_rows = store.recent_news_items(
        [team.key for team in teams],
        window_hours=ctx.settings.news_window_hours,
    )
    prompt_news = news_for_prompt(news_rows, teams)
    result = run_qual_model(
        command=ctx.settings.qual_llm_cmd,
        news_items=prompt_news,
        markets=markets,
        timeout_seconds=ctx.settings.qual_llm_timeout_seconds,
    )
    accepted = result.get("signals") or []
    inserted = store.record_qual_signals(accepted)
    payload = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "model_run_id": result.get("model_run_id"),
        "attempts": result.get("attempts", 0),
        "market_count": len(markets),
        "news_count": len(prompt_news),
        "produced_count": len(accepted) + len(result.get("discarded") or []),
        "accepted_count": len(accepted),
        "inserted_count": inserted,
        "signals": accepted,
        "discarded": result.get("discarded") or [],
        "markets": markets,
    }
    ctx.write_json("qual_signals.json", payload)
    deliver(_format(payload), ctx.settings.deliver_to)
    return payload
