"""Phase: qual-postmortem. Grade settled qualitative/confluence trades."""
from __future__ import annotations

import json
from typing import Any

from ..alerts import deliver
from ..qual_postmortem import (
    fetch_recap_for_settlement,
    run_postmortem_model,
    settlement_market_family,
    settlement_team_keys,
)
from ..research import ResearchStore, utc_now
from ..research_news import load_research_teams
from .base import Context, load_context


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _format(payload: dict[str, Any]) -> str:
    return (
        f"[qual-postmortem] checked={payload.get('checked_count', 0)} "
        f"completed={payload.get('completed_count', 0)} "
        f"missing_recaps={payload.get('missing_recap_count', 0)} "
        f"queued={payload.get('queued_count', 0)} "
        f"lessons={payload.get('lessons_upserted', 0)}"
    )


def _analysis_from_signal(signal: dict[str, Any]) -> dict[str, Any]:
    analysis = signal.get("analysis")
    if isinstance(analysis, dict) and analysis:
        return analysis
    return {
        "ticker": signal.get("ticker"),
        "base_rate": signal.get("base_rate"),
        "qual_prob": signal.get("qual_prob"),
        "confidence": signal.get("confidence"),
        "rationale": signal.get("rationale"),
        "citation_urls": signal.get("citation_urls") or [],
        "news_item_ids_used": signal.get("news_item_ids_used") or [],
        "scenarios": signal.get("scenarios") or [],
        "model_run_id": signal.get("model_run_id"),
        "prompt_version": signal.get("prompt_version"),
        "created_at": signal.get("created_at"),
    }


def _settlement_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["record"] = _loads(row.get("record_json"), {}) or {}
    payload["market"] = _loads(row.get("market_json"), {}) or {}
    payload["consensus"] = _loads(row.get("consensus_json"), {}) or {}
    return payload


def _lesson_family(record: dict[str, Any]) -> str:
    family = settlement_market_family(record)
    for prefix in ("qual ", "confluence_agree "):
        if family.startswith(prefix):
            return family.removeprefix(prefix)
    return family


def _recap_item_from_existing(store: ResearchStore, recap_row: dict[str, Any]) -> dict[str, Any] | None:
    item_id = recap_row.get("news_item_id")
    if not item_id:
        return None
    return store.news_item_by_id(str(item_id))


def run(ctx: Context | None = None) -> dict[str, Any]:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    try:
        teams = load_research_teams(ctx.settings.research_teams_path)
    except Exception as exc:
        payload = {
            "status": "unavailable",
            "reason": f"team-config-error:{exc.__class__.__name__}",
            "checked_count": 0,
            "completed_count": 0,
            "queued_count": 0,
            "missing_recap_count": 0,
            "lessons_upserted": 0,
        }
        ctx.write_json("qual_postmortem.json", payload)
        deliver(_format(payload), ctx.settings.deliver_to)
        return payload

    settlements = store.pending_qual_postmortem_settlements()
    completed = []
    skipped = []
    queued = []
    recap_records = []
    lessons_upserted = 0

    for row in settlements:
        signal = store.latest_qual_signal_for_ticker(
            str(row.get("ticker") or ""),
            at_or_before=row.get("audited_at"),
        )
        if not signal:
            skipped.append({
                "client_order_id": row.get("client_order_id"),
                "ticker": row.get("ticker"),
                "reason": "saved-analysis-missing",
            })
            continue
        analysis = _analysis_from_signal(signal)
        recap_item = None
        recap = store.qual_recap_for_settlement(str(row["client_order_id"]))
        if recap and recap.get("recap_status") == "missing":
            skipped.append({
                "client_order_id": row.get("client_order_id"),
                "ticker": row.get("ticker"),
                "reason": "recap-missing",
                "recap": recap,
            })
            continue
        if recap and recap.get("recap_status") == "found":
            recap_item = _recap_item_from_existing(store, recap)
        if recap_item is None:
            recap_result = fetch_recap_for_settlement(
                record=row,
                signal=signal,
                teams=teams,
                user_agent=ctx.settings.news_user_agent,
            )
            if recap_result.get("recap_status") == "found":
                recap_item = recap_result["item"]
                store.record_news_items([recap_item])
                recap = {
                    "client_order_id": row["client_order_id"],
                    "game_id": row["game_id"],
                    "signal_id": signal.get("id"),
                    "recap_status": "found",
                    "news_item_id": recap_item.get("id"),
                    "team": recap_result.get("team"),
                    "source": recap_result.get("source"),
                    "recap_date": recap_result.get("game_date"),
                    "fetched_at": utc_now(),
                    "source_status": recap_result.get("source_status") or [],
                    "settlement": _settlement_payload(row),
                }
                store.record_qual_recap(recap)
                recap_records.append(recap)
            else:
                recap = {
                    "client_order_id": row["client_order_id"],
                    "game_id": row["game_id"],
                    "signal_id": signal.get("id"),
                    "recap_status": "missing",
                    "team": ",".join(recap_result.get("team_keys") or []),
                    "recap_date": recap_result.get("game_date"),
                    "fetched_at": utc_now(),
                    "reason": recap_result.get("reason"),
                    "source_status": recap_result.get("source_status") or [],
                    "settlement": _settlement_payload(row),
                }
                store.record_qual_recap(recap)
                recap_records.append(recap)
                skipped.append({
                    "client_order_id": row.get("client_order_id"),
                    "ticker": row.get("ticker"),
                    "reason": "recap-missing",
                    "recap": recap,
                })
                continue

        model = run_postmortem_model(
            command=ctx.settings.qual_llm_cmd,
            analysis=analysis,
            recap=recap_item,
            settlement=_settlement_payload(row),
            timeout_seconds=ctx.settings.qual_llm_timeout_seconds,
        )
        if model.get("status") != "ok":
            queued.append({
                "client_order_id": row.get("client_order_id"),
                "ticker": row.get("ticker"),
                "reason": model.get("reason") or model.get("status"),
            })
            continue

        postmortem = model["postmortem"]
        lessons = postmortem.get("lessons") or []
        postmortem_id = store.record_qual_postmortem({
            "client_order_id": row["client_order_id"],
            "game_id": row["game_id"],
            "signal_id": signal["id"],
            "ticker": row["ticker"],
            "created_at": postmortem.get("created_at") or utc_now(),
            "model_run_id": signal.get("model_run_id"),
            "recap_news_item_id": recap_item.get("id"),
            "outcome": row["outcome"],
            "postmortem": postmortem,
            "lessons": lessons,
        })
        family = _lesson_family(row)
        teams_for_lessons = (
            [str(recap.get("team"))] if recap and recap.get("team")
            else settlement_team_keys(row, teams, signal)
        )
        for team in teams_for_lessons:
            for lesson in lessons:
                if store.upsert_qual_lesson(
                    team=team,
                    market_family=family,
                    lesson_text=str(lesson.get("lesson") or ""),
                    evidence_cite=str(lesson.get("evidence_cite") or ""),
                    postmortem_id=postmortem_id,
                ):
                    lessons_upserted += 1
        completed.append({
            "client_order_id": row["client_order_id"],
            "ticker": row["ticker"],
            "signal_id": signal["id"],
            "postmortem_id": postmortem_id,
            "which_scenario_occurred": postmortem.get("which_scenario_occurred"),
            "lessons": lessons,
        })

    payload = {
        "status": "ok",
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "checked_count": len(settlements),
        "completed_count": len(completed),
        "queued_count": len(queued),
        "skipped_count": len(skipped),
        "missing_recap_count": sum(1 for row in skipped if row.get("reason") == "recap-missing"),
        "lessons_upserted": lessons_upserted,
        "completed": completed,
        "queued": queued,
        "skipped": skipped,
        "recaps": recap_records,
        "learning_summary": store.qual_learning_summary(),
    }
    ctx.write_json("qual_postmortem.json", payload)
    deliver(_format(payload), ctx.settings.deliver_to)
    return payload
