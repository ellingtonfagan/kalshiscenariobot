"""Phase: qual-research. Produce cited qualitative probabilities for unpriced rows."""
from __future__ import annotations

from ..alerts import deliver
from ..qual_learning import format_calibration_lines
from ..qual_research import (
    news_for_prompt,
    retrieve_contexts_for_market,
    run_qual_model,
    unpriced_team_market_rows,
)
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
    index_result = store.build_qual_rag_index()
    chunks = store.qual_rag_chunks()
    retrieval_contexts: dict[str, list[dict]] = {}
    retrieval_stats = {
        "market_count": len(markets),
        "hit_count": 0,
        "context_count": 0,
        "leaked_context_count": 0,
        "source_counts": {},
        "evidence_context_count": 0,
        "opinion_context_count": 0,
        "evidence_floor_failed_count": 0,
        "corpus": store.qual_rag_corpus_summary(),
    }
    # Retrieval failures should not make the research phase crash; the model
    # still receives the old recent-news context in that case.
    try:
        for market in markets:
            ticker = str(market.get("ticker") or "")
            result = retrieve_contexts_for_market(market=market, chunks=chunks)
            contexts = result.get("contexts") or []
            composition = result.get("composition") or {}
            retrieval_contexts[ticker] = contexts
            retrieval_stats["hit_count"] += 1 if contexts else 0
            retrieval_stats["context_count"] += len(contexts)
            retrieval_stats["leaked_context_count"] += int(result.get("leaked_context_count") or 0)
            retrieval_stats["evidence_context_count"] += int(composition.get("evidence_count") or 0)
            retrieval_stats["opinion_context_count"] += int(composition.get("opinion_count") or 0)
            if contexts and not composition.get("evidence_floor_met"):
                retrieval_stats["evidence_floor_failed_count"] += 1
            for source_type, count in (composition.get("source_counts") or {}).items():
                retrieval_stats["source_counts"][source_type] = (
                    int(retrieval_stats["source_counts"].get(source_type) or 0) + int(count or 0)
                )
            query = result.get("query")
            store.record_qual_signal_retrieval(
                model_run_id="pending",
                ticker=ticker,
                query={
                    "ticker": getattr(query, "ticker", ticker),
                    "text": getattr(query, "text", ""),
                    "teams": list(getattr(query, "teams", ()) or []),
                    "market_family": getattr(query, "market_family", ""),
                    "close_time": getattr(query, "close_time", market.get("close_time")),
                },
                contexts=contexts,
                close_time=market.get("close_time"),
                leaked_context_count=int(result.get("leaked_context_count") or 0),
            )
    except Exception as exc:
        retrieval_contexts = {}
        retrieval_stats["status"] = f"fallback:{exc.__class__.__name__}"
    else:
        retrieval_stats["status"] = "ok"
    learning = _learning_context(
        store,
        markets,
        top_n=int(getattr(ctx.settings, "qual_lessons_top_n", 5)),
    )
    result = run_qual_model(
        command=ctx.settings.qual_llm_cmd,
        news_items=prompt_news,
        markets=markets,
        retrieval_contexts=retrieval_contexts,
        min_groundedness=float(getattr(ctx.settings, "qual_min_groundedness", 0.6)),
        lessons=learning["lessons"],
        calibration_lines=learning["calibration_lines"],
        timeout_seconds=ctx.settings.qual_llm_timeout_seconds,
        fallback_model=getattr(ctx.settings, "qual_fallback_model", "claude-opus-4-8"),
    )
    accepted = result.get("signals") or []
    inserted = store.record_qual_signals(accepted)
    model_run_id = result.get("model_run_id")
    if model_run_id and retrieval_contexts:
        for market in markets:
            ticker = str(market.get("ticker") or "")
            if ticker not in retrieval_contexts:
                continue
            store.record_qual_signal_retrieval(
                model_run_id=str(model_run_id),
                ticker=ticker,
                query={
                    "ticker": ticker,
                    "text": " ".join(str(market.get(key) or "") for key in ("ticker", "title", "market_family")),
                    "teams": market.get("teams") or [],
                    "market_family": market.get("market_family"),
                    "close_time": market.get("close_time"),
                },
                contexts=retrieval_contexts[ticker],
                close_time=market.get("close_time"),
                leaked_context_count=0,
            )
    score_rows = []
    for signal in accepted:
        score = signal.get("groundedness_score") or {}
        if score:
            score_rows.append({
                **score,
                "ticker": signal.get("ticker"),
                "model_run_id": signal.get("model_run_id") or model_run_id,
                "created_at": signal.get("created_at"),
                "tradeable": signal.get("tradeable", True),
                "blocked_reason": signal.get("blocked_reason"),
            })
    store.record_qual_groundedness(score_rows)
    payload = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "model_run_id": result.get("model_run_id"),
        "attempts": result.get("attempts", 0),
        "engine": result.get("engine"),
        "market_count": len(markets),
        "news_count": len(prompt_news),
        "produced_count": len(accepted) + len(result.get("discarded") or []),
        "accepted_count": len(accepted),
        "inserted_count": inserted,
        "signals": accepted,
        "discarded": result.get("discarded") or [],
        "markets": markets,
        "index": index_result,
        "retrieval": retrieval_stats,
        "rag_corpus": store.qual_rag_corpus_summary(),
        "learning_context": learning,
    }
    ctx.write_json("qual_signals.json", payload)
    deliver(_format(payload), ctx.settings.deliver_to)
    return payload
