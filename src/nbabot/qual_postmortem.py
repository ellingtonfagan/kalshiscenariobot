"""Post-settlement learning pass for qualitative scenario analysis."""
from __future__ import annotations

import json
import re
import urllib.error
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .performance_learner import market_family
from .qual_research import USAGE_LIMIT_RE, invoke_codex_cli, invoke_claude_api, parse_strict_json, run_llm_with_fallback
from .research import utc_now
from .research_news import (
    ResearchTeam,
    feed_sources,
    fetch_url,
    item_matches_team,
    parse_feed,
)


POSTMORTEM_PROMPT_VERSION = "qual-postmortem-v1"
POSTMORTEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "which_scenario_occurred",
        "event_prediction_grade",
        "conditional_grade",
        "what_was_missed",
        "lessons",
    ],
    "properties": {
        "which_scenario_occurred": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
        "event_prediction_grade": {"type": "string"},
        "conditional_grade": {"type": "string"},
        "what_was_missed": {"type": "string"},
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["lesson", "evidence_cite"],
                "properties": {
                    "lesson": {"type": "string"},
                    "evidence_cite": {"type": "string"},
                },
            },
        },
    },
}
RECAP_TERMS = (
    "recap", "beat", "beats", "defeat", "defeats", "defeated", "top",
    "tops", "edge", "edges", "walk-off", "rout", "shut out", "wins",
    "win over", "falls", "fall to", "loss",
)
_TICKER_DATE_RE = re.compile(
    r"-(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<dd>\d{2})",
    re.I,
)
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_from_ticker(ticker: Any) -> date | None:
    match = _TICKER_DATE_RE.search(str(ticker or ""))
    if not match:
        return None
    try:
        return date(
            2000 + int(match.group("yy")),
            _MONTHS[match.group("mon").upper()],
            int(match.group("dd")),
        )
    except (KeyError, ValueError):
        return None


def settlement_game_date(record: dict[str, Any]) -> date | None:
    ticker_date = _date_from_ticker(record.get("ticker"))
    if ticker_date:
        return ticker_date
    payloads = [
        record,
        _loads(record.get("record_json"), {}) or {},
        _loads(record.get("market_json"), {}) or {},
    ]
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("close_time", "settled_at", "audited_at"):
            parsed = _parse_dt(payload.get(key))
            if parsed:
                return parsed.astimezone(ZoneInfo("America/New_York")).date()
        market = payload.get("market")
        if isinstance(market, dict):
            parsed = _parse_dt(market.get("close_time") or market.get("settled_at"))
            if parsed:
                return parsed.astimezone(ZoneInfo("America/New_York")).date()
    return None


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(v) for v in value)
    return str(value or "")


def settlement_search_text(record: dict[str, Any], signal: dict[str, Any] | None = None) -> str:
    pieces = [
        record.get("ticker"),
        record.get("game_id"),
        _flatten_text(_loads(record.get("record_json"), {}) or {}),
        _flatten_text(_loads(record.get("market_json"), {}) or {}),
    ]
    if signal:
        pieces.append(_flatten_text(signal.get("analysis") or signal))
    return " ".join(piece for piece in pieces if piece)


def settlement_team_keys(
    record: dict[str, Any],
    teams: list[ResearchTeam],
    signal: dict[str, Any] | None = None,
) -> list[str]:
    text = settlement_search_text(record, signal).lower()
    out = []
    for team in teams:
        if any(str(alias).lower() in text for alias in team.aliases if alias):
            out.append(team.key)
    return list(dict.fromkeys(out))


def _published_date(item: dict[str, Any]) -> date | None:
    parsed = _parse_dt(item.get("published_at"))
    if parsed is None:
        return None
    return parsed.astimezone(ZoneInfo("America/New_York")).date()


def _looks_like_recap(item: dict[str, Any]) -> bool:
    text = f"{item.get('title') or ''} {item.get('body') or ''}".lower()
    return any(term in text for term in RECAP_TERMS)


def recap_matches_item(item: dict[str, Any], team: ResearchTeam, game_date: date) -> bool:
    item_date = _published_date(item)
    if item_date is None or item_date not in {game_date, game_date + timedelta(days=1)}:
        return False
    if not item_matches_team(item, team):
        return False
    return _looks_like_recap(item)


def _recap_sources(team: ResearchTeam) -> list[dict[str, Any]]:
    sources = [
        source for source in feed_sources(team)
        if str(source.get("kind") or "rss") in {"rss", "atom"} or str(source.get("name") or "").startswith("espn_")
    ]
    if not any(str(source.get("name") or "") == "espn_mlb" for source in sources):
        sources.append({
            "name": "espn_mlb",
            "url": "https://www.espn.com/espn/rss/mlb/news",
            "kind": "rss",
            "require_alias": True,
        })
    return [source for source in sources if not str(source.get("name") or "").startswith("reddit_")]


def fetch_recap_for_settlement(
    *,
    record: dict[str, Any],
    signal: dict[str, Any] | None,
    teams: list[ResearchTeam],
    user_agent: str,
    fetcher: Any = fetch_url,
) -> dict[str, Any]:
    game_date = settlement_game_date(record)
    team_keys = settlement_team_keys(record, teams, signal)
    source_status: list[dict[str, Any]] = []
    if game_date is None:
        return {
            "recap_status": "missing",
            "reason": "game-date-unresolved",
            "team_keys": team_keys,
            "source_status": source_status,
        }
    if not team_keys:
        return {
            "recap_status": "missing",
            "reason": "team-unresolved",
            "game_date": game_date.isoformat(),
            "team_keys": [],
            "source_status": source_status,
        }

    teams_by_key = {team.key: team for team in teams}
    matches: list[dict[str, Any]] = []
    for team_key in team_keys:
        team = teams_by_key.get(team_key)
        if team is None:
            continue
        for source in _recap_sources(team):
            status = {
                "team": team.key,
                "source": source.get("name"),
                "url": source.get("url"),
                "status": "unknown",
                "http_status": None,
                "matched": 0,
            }
            try:
                http_status, body = fetcher(str(source["url"]), user_agent=user_agent)
                status["http_status"] = http_status
                items = parse_feed(body, source=str(source.get("name") or source.get("url")), team=team.key)
            except urllib.error.HTTPError as exc:
                status["http_status"] = exc.code
                status["status"] = f"http_{exc.code}"
                status["error"] = str(exc)
                source_status.append(status)
                continue
            except Exception as exc:
                status["status"] = f"error:{exc.__class__.__name__}"
                status["error"] = str(exc)
                source_status.append(status)
                continue
            if bool(source.get("require_alias")):
                items = [item for item in items if item_matches_team(item, team)]
            matched = [item for item in items if recap_matches_item(item, team, game_date)]
            status["status"] = "ok"
            status["parsed"] = len(items)
            status["matched"] = len(matched)
            source_status.append(status)
            for item in matched:
                matches.append({
                    "item": item,
                    "team": team.key,
                    "source": source.get("name"),
                    "priority": 0 if str(source.get("name") or "").startswith("mlb_") else 1,
                    "published_at": item.get("published_at") or "",
                })

    if not matches:
        return {
            "recap_status": "missing",
            "reason": "no-matching-recap",
            "game_date": game_date.isoformat(),
            "team_keys": team_keys,
            "source_status": source_status,
        }

    best = sorted(matches, key=lambda row: (row["priority"], str(row["published_at"])), reverse=False)[0]
    return {
        "recap_status": "found",
        "game_date": game_date.isoformat(),
        "team_keys": team_keys,
        "team": best["team"],
        "source": best["source"],
        "item": best["item"],
        "source_status": source_status,
    }


def build_postmortem_prompt(
    *,
    analysis: dict[str, Any],
    recap: dict[str, Any],
    settlement: dict[str, Any],
    retry_error: str | None = None,
) -> str:
    schema = (
        '{"which_scenario_occurred":0,'
        '"event_prediction_grade":"short assessment",'
        '"conditional_grade":"short assessment",'
        '"what_was_missed":"1-2 sentences",'
        '"lessons":[{"lesson":"general reusable lesson","evidence_cite":"specific recap evidence"}]}'
    )
    retry_line = f"\nPrevious output was invalid: {retry_error}\n" if retry_error else ""
    payload = {
        "original_analysis": analysis,
        "recap": {
            "title": recap.get("title"),
            "body": recap.get("body"),
            "url": recap.get("url"),
            "published_at": recap.get("published_at"),
        },
        "settlement": settlement,
        "scenario_indexing": "zero-based; use none-of-the-above if no saved branch occurred",
    }
    return (
        "You are grading a previously saved sports market forecast after settlement. "
        "Use only the original analysis, recap, and settled outcome below. "
        "Return STRICT JSON only, no markdown and no prose wrapper. "
        "Lessons must be general and reusable, but each lesson must cite the specific recap evidence. "
        "Do not invent recap facts.\n"
        f"Schema: {schema}\n"
        f"{retry_line}"
        f"Input JSON:\n{json.dumps(payload, sort_keys=True)}"
    )


def validate_postmortem_output(raw_output: str, *, scenario_count: int) -> dict[str, Any]:
    parsed = parse_strict_json(raw_output)
    if not isinstance(parsed, dict):
        raise ValueError("top-level JSON must be an object")
    scenario = parsed.get("which_scenario_occurred")
    if isinstance(scenario, str) and scenario.strip().isdigit():
        scenario = int(scenario.strip())
    if scenario != "none-of-the-above":
        if not isinstance(scenario, int) or scenario < 0 or scenario >= scenario_count:
            raise ValueError("which_scenario_occurred out of range")
    lessons_raw = parsed.get("lessons")
    if not isinstance(lessons_raw, list) or not 1 <= len(lessons_raw) <= 3:
        raise ValueError("lessons must contain 1-3 entries")
    lessons = []
    for idx, row in enumerate(lessons_raw):
        if not isinstance(row, dict):
            raise ValueError(f"lesson {idx} is not an object")
        lesson = str(row.get("lesson") or "").strip()
        cite = str(row.get("evidence_cite") or "").strip()
        if not lesson or not cite:
            raise ValueError(f"lesson {idx} missing lesson/evidence_cite")
        lessons.append({
            "lesson": lesson[:240],
            "evidence_cite": cite[:300],
        })
    out = {
        "which_scenario_occurred": scenario,
        "event_prediction_grade": str(parsed.get("event_prediction_grade") or "").strip()[:400],
        "conditional_grade": str(parsed.get("conditional_grade") or "").strip()[:400],
        "what_was_missed": str(parsed.get("what_was_missed") or "").strip()[:600],
        "lessons": lessons,
        "prompt_version": POSTMORTEM_PROMPT_VERSION,
        "created_at": utc_now(),
    }
    for key in ("event_prediction_grade", "conditional_grade", "what_was_missed"):
        if not out[key]:
            raise ValueError(f"missing {key}")
    return out


def run_postmortem_model(
    *,
    command: str,
    analysis: dict[str, Any],
    recap: dict[str, Any],
    settlement: dict[str, Any],
    timeout_seconds: int,
    invoker: Any = invoke_codex_cli,
    claude_invoker: Any = invoke_claude_api,
    fallback_model: str = "claude-opus-4-8",
    anthropic_api_key: str | None = None,
) -> dict[str, Any]:
    scenarios = analysis.get("scenarios") if isinstance(analysis.get("scenarios"), list) else []
    scenario_count = len(scenarios)
    if scenario_count <= 0:
        return {"status": "skipped", "reason": "analysis-has-no-scenarios", "postmortem": None}

    retry_error = None
    last_reason = None
    last_output = ""
    for attempt in (1, 2):
        prompt = build_postmortem_prompt(
            analysis=analysis,
            recap=recap,
            settlement=settlement,
            retry_error=retry_error,
        )
        run = run_llm_with_fallback(
            command=command,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            schema=POSTMORTEM_SCHEMA,
            fallback_model=fallback_model,
            invoker=invoker,
            claude_invoker=claude_invoker,
            anthropic_api_key=anthropic_api_key,
        )
        output = str(run.get("output") or "")
        last_output = output
        if not run.get("ok"):
            return {
                "status": "unavailable",
                "reason": run.get("reason") or "codex-cli-failed",
                "attempts": attempt,
                "engine": run.get("engine"),
                "postmortem": None,
            }
        try:
            postmortem = validate_postmortem_output(output, scenario_count=scenario_count)
        except ValueError as exc:
            last_reason = str(exc)
            retry_error = last_reason
            continue
        return {
            "status": "ok",
            "reason": None,
            "attempts": attempt,
            "engine": run.get("engine"),
            "postmortem": postmortem,
        }
    if USAGE_LIMIT_RE.search(last_output):
        last_reason = "usage-limit"
    return {
        "status": "unavailable",
        "reason": f"malformed-json-after-retry: {last_reason or 'invalid output'}",
        "attempts": 2,
        "engine": "codex",
        "postmortem": None,
    }


def settlement_market_family(record: dict[str, Any]) -> str:
    return market_family(record)
