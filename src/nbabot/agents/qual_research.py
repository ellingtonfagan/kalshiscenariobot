"""Phase: qual-research. Produce cited qualitative probabilities for unpriced rows."""
from __future__ import annotations

from ..alerts import deliver
from ..qual_learning import format_calibration_lines
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


def _learning_context(store: ResearchStore, markets: list[dict], *, top_n: int) -> dict:
    lessons_by_key: dict[tuple[str, str, str], dict] = {}
    for market in markets:
        family = str(market.get("market_family") or "")
        if not family:
            continue
        for lesson in store.top_qual_lessons(
            teams=market.get("teams") or [],
            market_family=family,
            limit=top_n,
        ):
            key = (
                str(lesson.get("team") or ""),
                str(lesson.get("market_family") or ""),
                str(lesson.get("lesson_norm") or ""),
            )
            lessons_by_key[key] = {
                "team": lesson.get("team"),
                "market_family": lesson.get("market_family"),
                "lesson": lesson.get("lesson_text"),
                "evidence_cite": lesson.get("evidence_cite"),
                "hit_count": lesson.get("hit_count"),
            }
    calibration = store.qual_calibration_table()
    return {
        "lessons": list(lessons_by_key.values())[:top_n],
        "calibration": calibration,
        "calibration_lines": format_calibration_lines(calibration),
    }


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
    learning = _learning_context(
        store,
        markets,
        top_n=int(getattr(ctx.settings, "qual_lessons_top_n", 5)),
    )
    result = run_qual_model(
        command=ctx.settings.qual_llm_cmd,
        news_items=prompt_news,
        markets=markets,
        lessons=learning["lessons"],
        calibration_lines=learning["calibration_lines"],
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
        "learning_context": learning,
    }
    ctx.write_json("qual_signals.json", payload)
    deliver(_format(payload), ctx.settings.deliver_to)
    return payload
