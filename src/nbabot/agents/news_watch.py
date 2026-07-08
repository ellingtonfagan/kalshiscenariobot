"""Phase: news-watch. Cheap RSS diff that can trigger one demo cycle."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..alerts import deliver
from ..news_watch import (
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_DAILY_CAP,
    fetch_new_hits,
    load_high_impact_patterns,
    trigger_decision,
)
from ..research import ResearchStore, utc_now
from ..research_news import fetch_url, load_research_teams
from ..slate import build_market_basket
from . import scheduled_demo_cycle
from .base import Context, load_context


def _history_from_artifact(ctx: Context) -> list[dict[str, Any]]:
    prior = ctx.read_json("news_watch.json") or {}
    rows = prior.get("trigger_history")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _format_trigger(team: str, headline: str) -> str:
    return f"news trigger: {team} — {headline}"


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    now = datetime.now(timezone.utc)
    try:
        teams = load_research_teams(ctx.settings.research_teams_path)
        patterns = load_high_impact_patterns(ctx.settings.research_teams_path)
    except Exception as exc:
        payload = {
            "status": "unavailable",
            "reason": f"team-config-error:{exc.__class__.__name__}",
            "generated_at": utc_now(),
            "new_item_count": 0,
            "hit_count": 0,
            "trigger_decisions": [],
            "trigger_history": _history_from_artifact(ctx),
        }
        ctx.write_json("news_watch.json", payload)
        return payload

    scan = fetch_new_hits(
        teams=teams,
        store=store,
        user_agent=ctx.settings.news_user_agent,
        window_hours=ctx.settings.news_window_hours,
        patterns=patterns,
        fetcher=fetch_url,
        now=now,
    )
    history = _history_from_artifact(ctx)
    trigger_history = list(history)
    decisions: list[dict[str, Any]] = []
    cycle_results: list[dict[str, Any]] = []
    market_baskets: list[dict[str, Any]] = []

    for hit in scan.get("hits") or []:
        team = str(hit.get("team") or "")
        decision = trigger_decision(
            team,
            history=trigger_history,
            now=now,
            cooldown_minutes=int(getattr(
                ctx.settings,
                "event_trigger_cooldown_minutes",
                DEFAULT_COOLDOWN_MINUTES,
            )),
            daily_cap=int(getattr(
                ctx.settings,
                "event_trigger_daily_cap",
                DEFAULT_DAILY_CAP,
            )),
        )
        row = {
            "team": team,
            "canonical_name": hit.get("canonical_name"),
            "headline": hit.get("headline"),
            "url": hit.get("url"),
            "matched_patterns": hit.get("matched_patterns") or [],
            "decision": decision["decision"],
            "reason": decision["reason"],
            "decided_at": now.isoformat(),
        }
        if decision["decision"] == "fired":
            alert = _format_trigger(
                str(hit.get("canonical_name") or team),
                str(hit.get("headline") or ""),
            )
            deliver(alert, ctx.settings.deliver_to)
            slate = ctx.read_json("slate_candidates.json") or {"candidates": []}
            matches = ctx.read_json("market_matches.json") or {"rows": []}
            basket = build_market_basket(
                team=team,
                hit=hit,
                slate=slate,
                market_matches=matches,
                reason="news-watch high-impact hit",
            )
            previous_active = ctx.read_json("active_market_basket.json") or {}
            if basket.get("empty"):
                cycle = scheduled_demo_cycle.run(ctx)
                row["basket_reason"] = "empty-basket-full-cycle-fallback"
            else:
                store.record_market_basket(ctx.settings.game_id, basket)
                market_baskets.append(basket)
                ctx.write_json("active_market_basket.json", basket)
                try:
                    cycle = scheduled_demo_cycle.run(ctx)
                finally:
                    ctx.write_json("active_market_basket.json", previous_active)
                row["basket_id"] = basket.get("basket_id")
                row["basket_ticker_count"] = len(basket.get("tickers") or [])
            cycle_results.append({
                "team": team,
                "headline": hit.get("headline"),
                "basket_id": row.get("basket_id"),
                "basket_reason": row.get("basket_reason"),
                "exit_code": cycle.get("exit_code") if isinstance(cycle, dict) else None,
            })
            row["fired_at"] = now.isoformat()
            trigger_history.append(row)
        decisions.append(row)

    payload = {
        "status": "ok",
        "generated_at": utc_now(),
        "new_item_count": scan.get("new_item_count", 0),
        "hit_count": scan.get("hit_count", 0),
        "triggered_count": sum(1 for row in decisions if row["decision"] == "fired"),
        "debounced_count": sum(1 for row in decisions if row["decision"] == "debounced"),
        "capped_count": sum(1 for row in decisions if row["decision"] == "capped"),
        "patterns": patterns,
        "team_counts": scan.get("team_counts") or {},
        "source_status": scan.get("source_status") or [],
        "hits": scan.get("hits") or [],
        "market_baskets": market_baskets,
        "trigger_decisions": decisions,
        "trigger_history": trigger_history[-200:],
        "cycle_results": cycle_results,
        "notes": [
            "No-hit runs do not call the qual LLM, send alerts, or run the demo cycle.",
            "This phase only scans configured RSS feeds from research_teams.yaml.",
        ],
    }
    ctx.write_json("news_watch.json", payload)
    return payload
