"""Cheap RSS news trigger detection for event-driven demo cycles."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yaml

from .research import utc_now
from .research_news import (
    ResearchTeam,
    _is_recent,
    fetch_url,
    fetch_with_retries,
    item_matches_team,
    parse_feed,
)

DEFAULT_HIGH_IMPACT_PATTERNS = (
    "scratched",
    "injury",
    "injured",
    "IL",
    "suspended",
    "out for",
    "doubtful",
    "questionable",
    "postponed",
    "rain delay",
    "lineup change",
)
DEFAULT_COOLDOWN_MINUTES = 45
DEFAULT_DAILY_CAP = 8
ET = ZoneInfo("America/New_York")


def load_high_impact_patterns(path: Any) -> list[str]:
    try:
        doc = yaml.safe_load(path.read_text()) if hasattr(path, "read_text") else yaml.safe_load(str(path))
    except Exception:
        return list(DEFAULT_HIGH_IMPACT_PATTERNS)
    raw = (
        (doc or {}).get("news_watch", {}).get("high_impact_patterns")
        or (doc or {}).get("event_triggers", {}).get("high_impact_patterns")
        or (doc or {}).get("high_impact_patterns")
        or DEFAULT_HIGH_IMPACT_PATTERNS
    )
    patterns = [str(item).strip() for item in raw if str(item).strip()]
    return patterns or list(DEFAULT_HIGH_IMPACT_PATTERNS)


def compile_keyword_pattern(patterns: Iterable[str]) -> re.Pattern[str]:
    terms = []
    for raw in patterns:
        term = str(raw).strip()
        if not term:
            continue
        parts = [re.escape(part) for part in term.split()]
        terms.append(r"\s+".join(parts))
    if not terms:
        terms = [re.escape(item) for item in DEFAULT_HIGH_IMPACT_PATTERNS]
    body = "|".join(sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{body})(?![A-Za-z0-9_])", re.IGNORECASE)


def keyword_hits(text: str, patterns: Iterable[str]) -> list[str]:
    regex = compile_keyword_pattern(patterns)
    return [match.group(0) for match in regex.finditer(text or "")]


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _history(prior_artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (prior_artifact or {}).get("trigger_history")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def trigger_decision(
    team: str,
    *,
    history: list[dict[str, Any]],
    now: datetime,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> dict[str, Any]:
    now_et = now.astimezone(ET)
    today = now_et.date()
    fired = [
        row for row in history
        if row.get("decision") == "fired" and _parse_dt(row.get("fired_at"))
    ]
    today_fired = [
        row for row in fired
        if _parse_dt(row.get("fired_at")).astimezone(ET).date() == today
    ]
    if len(today_fired) >= max(int(daily_cap), 0):
        return {
            "decision": "capped",
            "reason": f"daily cap {daily_cap} reached",
        }

    cooldown = timedelta(minutes=max(int(cooldown_minutes), 0))
    team_fired = [
        row for row in today_fired
        if str(row.get("team") or "") == str(team)
    ]
    if team_fired:
        latest = max(_parse_dt(row.get("fired_at")) for row in team_fired)
        if latest and now - latest < cooldown:
            return {
                "decision": "debounced",
                "reason": f"team cooldown {cooldown_minutes}m active",
                "last_fired_at": latest.isoformat(),
            }
    return {"decision": "fired", "reason": "trigger allowed"}


def _rss_sources(team: ResearchTeam) -> list[dict[str, Any]]:
    sources = []
    for feed in team.rss_feeds:
        source = dict(feed)
        source.setdefault("name", source.get("url"))
        source.setdefault("kind", "rss")
        sources.append(source)
    return sources


def fetch_new_hits(
    *,
    teams: list[ResearchTeam],
    store: Any,
    user_agent: str,
    window_hours: float,
    patterns: Iterable[str],
    fetcher: Any = fetch_url,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    store.init_schema()
    source_status: list[dict[str, Any]] = []
    team_counts = {
        team.key: {"parsed": 0, "inserted": 0, "new": 0, "hits": 0}
        for team in teams
    }
    hits: list[dict[str, Any]] = []
    parsed_items: list[dict[str, Any]] = []

    for team in teams:
        for source in _rss_sources(team):
            status = {
                "team": team.key,
                "canonical_name": team.canonical_name,
                "source": source.get("name"),
                "url": source.get("url"),
                "kind": source.get("kind", "rss"),
                "status": "unknown",
                "http_status": None,
                "parsed": 0,
                "inserted": 0,
                "new": 0,
                "hits": 0,
            }
            try:
                http_status, body, retry_meta = fetch_with_retries(
                    str(source["url"]),
                    user_agent=user_agent,
                    fetcher=fetcher,
                    attempts=3,
                    base_sleep_seconds=0.2,
                )
                status["http_status"] = http_status
                status["retry_after_seconds"] = retry_meta.get("retry_after_seconds")
                items = parse_feed(
                    body,
                    source=str(source.get("name") or source.get("url")),
                    team=team.key,
                    feed=source,
                )
            except Exception as exc:
                status["status"] = f"error:{exc.__class__.__name__}"
                status["error"] = str(exc)
                source_status.append(status)
                recorder = getattr(store, "record_news_source_poll", None)
                if callable(recorder):
                    recorder({
                        "source_key": f"{team.key}:{source.get('name') or source.get('url')}",
                        "team": team.key,
                        "status": status["status"],
                        "http_status": status.get("http_status"),
                        "parsed": 0,
                        "inserted": 0,
                        "error": status.get("error"),
                    })
                continue

            filtered = []
            for item in items:
                if bool(source.get("require_alias")) and not item_matches_team(item, team):
                    continue
                if not _is_recent(item, window_hours=window_hours, now=now):
                    continue
                filtered.append(item)
            existing = store.existing_news_content_hashes(
                item["content_hash"] for item in filtered
            )
            new_items = [
                item for item in filtered
                if item.get("content_hash") not in existing
            ]
            inserted = store.record_news_items(filtered)
            regex = compile_keyword_pattern(patterns)
            for item in new_items:
                text = f"{item.get('title') or ''} {item.get('body') or ''}"
                matches = [match.group(0) for match in regex.finditer(text)]
                if not matches:
                    continue
                hits.append({
                    "team": team.key,
                    "canonical_name": team.canonical_name,
                    "source": item.get("source"),
                    "headline": item.get("title") or item.get("body") or "",
                    "url": item.get("url"),
                    "published_at": item.get("published_at"),
                    "content_hash": item.get("content_hash"),
                    "matched_patterns": matches,
                })
            parsed_items.extend(filtered)
            status.update({
                "status": "ok",
                "parsed": len(filtered),
                "inserted": inserted,
                "new": len(new_items),
                "hits": sum(1 for hit in hits if hit.get("team") == team.key and hit.get("source") == source.get("name")),
            })
            recorder = getattr(store, "record_news_source_poll", None)
            if callable(recorder):
                recorder({
                    "source_key": f"{team.key}:{source.get('name') or source.get('url')}",
                    "team": team.key,
                    "status": status["status"],
                    "http_status": status.get("http_status"),
                    "parsed": status.get("parsed", 0),
                    "inserted": status.get("inserted", 0),
                    "retry_after_seconds": status.get("retry_after_seconds"),
                })
            team_counts[team.key]["parsed"] += len(filtered)
            team_counts[team.key]["inserted"] += inserted
            team_counts[team.key]["new"] += len(new_items)
            team_counts[team.key]["hits"] += status["hits"]
            source_status.append(status)

    return {
        "generated_at": utc_now(),
        "window_hours": float(window_hours),
        "patterns": list(patterns),
        "team_counts": team_counts,
        "source_status": source_status,
        "total_parsed": len(parsed_items),
        "total_inserted": sum(row["inserted"] for row in team_counts.values()),
        "new_item_count": sum(row["new"] for row in team_counts.values()),
        "hit_count": len(hits),
        "hits": hits,
    }
