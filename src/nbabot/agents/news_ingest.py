"""Phase: news-ingest. Fetch team-scoped RSS/Atom research items."""
from __future__ import annotations

from ..alerts import deliver
from ..research import ResearchStore
from ..research_news import ingest_team_feeds, load_research_teams
from .base import Context, load_context


def _format(payload: dict) -> str:
    counts = payload.get("team_counts") or {}
    team_bits = [
        f"{team}:{row.get('inserted', 0)}/{row.get('parsed', 0)}"
        for team, row in counts.items()
        if isinstance(row, dict)
    ]
    return (
        f"[news-ingest] teams={len(counts)} inserted={payload.get('total_inserted', 0)} "
        f"parsed={payload.get('total_parsed', 0)} "
        f"team_counts={','.join(team_bits) if team_bits else 'none'}"
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
            "team_counts": {},
            "source_status": [],
            "total_inserted": 0,
            "total_parsed": 0,
        }
        ctx.write_json("news_ingest.json", payload)
        deliver(_format(payload), ctx.settings.deliver_to)
        return payload

    payload = ingest_team_feeds(
        teams=teams,
        store=store,
        user_agent=ctx.settings.news_user_agent,
        window_hours=ctx.settings.news_window_hours,
    )
    payload["status"] = "ok"
    ctx.write_json("news_ingest.json", payload)
    deliver(_format(payload), ctx.settings.deliver_to)
    return payload
