"""Slate discovery, verification, and research candidate helpers."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from . import performance_learner
from .guardrails import GUARDRAIL_FOOTER
from .market_identity import KALSHI_SERIES_SPORTS
from .odds_refresh import artifact_freshness
from .research import ResearchStore, utc_now
from .sources.registry import load_source_config


_KALSHI_DISCOVERY_SERIES_BY_PREFIX = {
    "KXMLB": ("KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL"),
    "KXNBA": ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL"),
    "KXNFL": ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"),
    "KXWNBA": ("KXWNBAGAME", "KXWNBASPREAD", "KXWNBATOTAL"),
    "KXWC": ("KXWCGAME", "KXWCADVANCE", "KXWCTOTAL", "KXWCBTTS", "KXWCGOAL"),
    "KXUCL": ("KXUCLGAME", "KXUCLADVANCE", "KXUCLTOTAL", "KXUCLBTTS", "KXUCLGOAL"),
}

STRUCTURED_BOOK_PROVIDERS = {"sportsgameodds", "the_odds_api", "parlay_api"}
EXCLUDED_CONSENSUS_BOOKS_BY_PROVIDER = {
    "parlay_api": {"kalshi"},
}
BASKET_MARKET_TYPES = {"moneyline", "spread", "total", "team_total"}


def _csv(raw: str | None, default: Iterable[str]) -> list[str]:
    values = [item.strip().lower() for item in (raw or "").split(",") if item.strip()]
    return values or list(default)


def configured_sports(settings: Any, source_config: dict[str, Any] | None = None) -> list[str]:
    config = source_config or load_source_config()
    sports_config = config.get("sports") or {}
    supported = set(sports_config.keys())
    default = [
        key for key, sport in sports_config.items()
        if sport.get("default_discovery")
    ] or [getattr(settings, "sport", "nba")]
    requested = _csv(os.environ.get("NBABOT_SLATE_SPORTS"), default)
    return [sport for sport in requested if sport in supported]


def _odds_api_keys_for_sport(source_config: dict[str, Any], sport: str) -> list[str]:
    sport_keys = (source_config.get("sports", {}).get(sport, {}) or {}).get("odds_api_sport_key")
    if not sport_keys:
        return []
    if isinstance(sport_keys, str):
        return [sport_keys]
    return [str(key) for key in sport_keys]


def _row_matches_sport(row: dict[str, Any], source_config: dict[str, Any], sport: str) -> bool:
    row_sport = str(row.get("sport", "")).lower()
    if row_sport == sport:
        return True
    row_key = str(row.get("sport_key", "")).lower()
    return row_key in {key.lower() for key in _odds_api_keys_for_sport(source_config, sport)}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _the_odds_api_enabled() -> bool:
    if _bool_env("NBABOT_DISABLE_THE_ODDS_API", False):
        return False
    raw = os.environ.get("NBABOT_THE_ODDS_API_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _header_int(status: dict[str, Any], key: str) -> int | None:
    value = status.get(key)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _norm_text(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _basket_market_type(row: dict[str, Any]) -> str | None:
    series = str(row.get("series_ticker") or row.get("series") or "").upper()
    title = " ".join(str(part or "") for part in (
        row.get("title"),
        row.get("yes_sub_title"),
        row.get("no_sub_title"),
        row.get("rules_primary"),
        row.get("rules_secondary"),
    )).lower()
    if series.endswith("GAME") or " winner" in title or " to win" in title:
        return "moneyline"
    if series.endswith("SPREAD") or "spread" in title or "run line" in title:
        return "spread"
    if "team total" in title or series.endswith("GOAL") or "teamtotal" in series:
        return "team_total"
    if series.endswith("TOTAL") or "total" in title or "over" in title or "under" in title:
        return "total"
    return None


def _first(raw: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if raw.get(name) not in (None, ""):
            return raw[name]
    return None


def _team_name(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        names = raw.get("names")
        if isinstance(names, dict):
            nested = _first(names, ("long", "medium", "short", "display", "name"))
            if nested:
                return str(nested)
        return _first(raw, ("displayName", "name", "fullName", "abbreviation", "id"))
    return None


def _event_id(raw: dict[str, Any]) -> str:
    return str(_first(raw, ("eventID", "eventId", "gameID", "gameId", "id", "event_id")) or "")


def _start_time(raw: dict[str, Any]) -> str | None:
    value = _first(raw, ("commence_time", "startTime", "startTimeUTC", "start_time", "date", "scheduled"))
    return str(value) if value not in (None, "") else None


def _teams_from_event(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    home = _first(raw, ("home_team", "homeTeam", "home", "homeCompetitor"))
    away = _first(raw, ("away_team", "awayTeam", "away", "awayCompetitor"))
    if isinstance(raw.get("competitions"), list) and raw["competitions"]:
        competitors = raw["competitions"][0].get("competitors", [])
        for competitor in competitors:
            team = competitor.get("team", {})
            if competitor.get("homeAway") == "home":
                home = team
            elif competitor.get("homeAway") == "away":
                away = team
    if (not home or not away) and isinstance(raw.get("teams"), list) and len(raw["teams"]) >= 2:
        away, home = raw["teams"][0], raw["teams"][1]
    if isinstance(raw.get("teams"), dict):
        teams = raw["teams"]
        away = away or teams.get("away")
        home = home or teams.get("home")
    return _team_name(away), _team_name(home)


def _event_side_name(raw: dict[str, Any], side: Any) -> Any:
    """Translate provider home/away side IDs to actual team names."""
    side_text = str(side or "").strip().lower()
    if side_text not in {"home", "away"}:
        return side
    away, home = _teams_from_event(raw)
    return home if side_text == "home" else away


def _market_count(raw: dict[str, Any]) -> int:
    count = 0
    if isinstance(raw.get("bookmakers"), list):
        for book in raw["bookmakers"]:
            count += len(book.get("markets") or [])
    for key in ("odds", "markets", "lines"):
        value = raw.get(key)
        if isinstance(value, list):
            count += len(value)
        elif isinstance(value, dict):
            count += len(value)
    return count


def _book_key(raw: Any) -> str:
    return str(raw or "").strip().lower().replace(" ", "")


def _line_markets_from_bookmakers(
    provider: str,
    raw: dict[str, Any],
    *,
    price_format: str | None = None,
) -> list[dict[str, Any]]:
    markets = []
    excluded_books = EXCLUDED_CONSENSUS_BOOKS_BY_PROVIDER.get(provider, set())
    for book in raw.get("bookmakers") or []:
        book_key = book.get("key") or book.get("title")
        if _book_key(book_key) in excluded_books:
            continue
        for market in book.get("markets") or []:
            key = market.get("key")
            if key not in {"h2h", "spreads", "totals"}:
                continue
            for outcome in market.get("outcomes") or []:
                row = {
                    "provider": provider,
                    "book": book_key,
                    "book_title": book.get("title"),
                    "market": key,
                    "name": outcome.get("name"),
                    "price": outcome.get("price"),
                    "point": outcome.get("point"),
                    "last_update": market.get("last_update") or book.get("last_update"),
                    "last_update_ms": market.get("last_update_ms") or book.get("last_update_ms"),
                }
                if price_format:
                    row["price_format"] = price_format
                markets.append(row)
    return markets


def _line_markets_from_odds_api(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return _line_markets_from_bookmakers(
        "the_odds_api",
        raw,
        price_format=raw.get("_price_format"),
    )


def _line_markets_generic(
    provider: str,
    raw: dict[str, Any],
    *,
    price_format: str | None = None,
) -> list[dict[str, Any]]:
    price_format = price_format or raw.get("_price_format")
    if isinstance(raw.get("bookmakers"), list):
        return _line_markets_from_bookmakers(provider, raw, price_format=price_format)
    markets = []
    for key in ("odds", "markets", "lines"):
        value = raw.get(key)
        rows = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            by_book = row.get("byBookmaker")
            if isinstance(by_book, dict) and by_book:
                for book_key, book_row in by_book.items():
                    if not isinstance(book_row, dict):
                        continue
                    if book_row.get("available") is False and book_row.get("odds") in (None, ""):
                        continue
                    side_name = _first(row, ("sideID", "selection", "participant", "team", "name"))
                    item = {
                        "provider": provider,
                        "book": book_key,
                        "market": _first(row, ("marketName", "market", "type", "oddID", "name")),
                        "name": _event_side_name(raw, side_name),
                        "side_id": side_name,
                        "price": _first(book_row, ("odds", "price", "americanOdds", "decimalOdds")),
                        "point": _first(book_row, ("overUnder", "line", "point", "spread", "total")) or _first(row, ("bookOverUnder", "line", "point", "spread", "total")),
                        "last_update": book_row.get("lastUpdatedAt"),
                        "available": book_row.get("available"),
                        "odd_id": row.get("oddID"),
                        "opposing_odd_id": row.get("opposingOddID"),
                    }
                    if price_format:
                        item["price_format"] = price_format
                    markets.append(item)
                continue
            market = _first(row, ("market", "marketName", "type", "oddID", "name"))
            line = _first(row, ("line", "point", "spread", "total"))
            price = _first(row, ("price", "odds", "americanOdds", "decimalOdds"))
            item = {
                "provider": provider,
                "book": _first(row, ("sportsbook", "sportsBook", "book", "bookID")),
                "market": market,
                "name": _event_side_name(raw, _first(row, ("selection", "participant", "team", "name", "sideID"))),
                "price": price,
                "point": line,
            }
            if price_format:
                item["price_format"] = price_format
            markets.append(item)
    return markets


def _price_cents(raw: Any) -> int:
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0
    if value <= 1:
        value *= 100
    return int(round(value))


def _kalshi_quote_fields(market: dict[str, Any]) -> dict[str, Any]:
    bid = _price_cents(market.get("yes_bid_dollars") or market.get("yes_bid"))
    ask = _price_cents(market.get("yes_ask_dollars") or market.get("yes_ask"))
    mid = round((bid + ask) / 2) if bid and ask else bid or ask
    spread = (ask - bid) if bid and ask else None
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "implied": min(max(mid / 100.0, 0.0), 1.0) if mid else None,
        "spread_cents": spread,
        "has_live_quote": bool(bid or ask),
    }


def _sport_keywords(source_config: dict[str, Any]) -> dict[str, list[str]]:
    out = {}
    for sport, doc in (source_config.get("sports") or {}).items():
        out[sport] = [str(value).lower() for value in (doc.get("kalshi_keywords") or [])]
    return out


def _classify_kalshi_sports(title: str, source_config: dict[str, Any]) -> list[str]:
    text = title.lower()
    scored = []
    for sport, keywords in _sport_keywords(source_config).items():
        score = sum(1 for keyword in keywords if keyword and keyword in text)
        if score:
            scored.append((score, sport))
    scored.sort(reverse=True)
    return [sport for _, sport in scored] or ["kalshi_sports"]


def _kalshi_components(title: str) -> list[str]:
    return [
        part.strip()
        for part in str(title or "").split(",")
        if part.strip()
    ]


def _kalshi_series_for_sports(sports: Iterable[str]) -> list[tuple[str, str]]:
    selected = set(sports)
    pairs: list[tuple[str, str]] = []
    seen = set()
    for prefix, sport in KALSHI_SERIES_SPORTS.items():
        if sport not in selected:
            continue
        for series in _KALSHI_DISCOVERY_SERIES_BY_PREFIX.get(prefix, (f"{prefix}GAME",)):
            if series not in seen:
                pairs.append((series, sport))
                seen.add(series)
        if prefix not in seen:
            pairs.append((prefix, sport))
            seen.add(prefix)
    return pairs


def _kalshi_composite_market(market: dict[str, Any]) -> bool:
    ticker = str(market.get("ticker") or "")
    return (
        ticker.startswith("KXMVE")
        or bool(market.get("mve_selected_legs"))
        or bool(market.get("mve_collection_ticker"))
    )


def _candidate_key(sport: str, away: str | None, home: str | None, event_id: str) -> str:
    if away and home:
        return f"{sport}:{_norm_text(away)}:{_norm_text(home)}"
    return f"{sport}:event:{_norm_text(event_id)}"


def _empty_candidate(sport: str, key: str, away: str | None, home: str | None,
                     start_time: str | None) -> dict[str, Any]:
    return {
        "candidate_id": key,
        "sport": sport,
        "league": sport.upper(),
        "away_team": away,
        "home_team": home,
        "start_time": start_time,
        "source_ids": {},
        "sources": [],
        "structured_sources": [],
        "fallback_sources": [],
        "line_markets": [],
        "market_count": 0,
        "kalshi_markets": [],
        "mapped_kalshi_markets": [],
        "reasoning": [],
        "status": "watch",
        "confidence": "low",
    }


def _merge_provider_events(provider: str, sport: str, raw_events: list[dict[str, Any]],
                           candidates: dict[str, dict[str, Any]]) -> None:
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        away, home = _teams_from_event(raw)
        event_id = _event_id(raw)
        key = _candidate_key(sport, away, home, event_id)
        start_time = _start_time(raw)
        candidate = candidates.setdefault(key, _empty_candidate(sport, key, away, home, start_time))
        if not candidate.get("start_time") and start_time:
            candidate["start_time"] = start_time
        if provider not in candidate["sources"]:
            candidate["sources"].append(provider)
        if provider in STRUCTURED_BOOK_PROVIDERS and provider not in candidate["structured_sources"]:
            candidate["structured_sources"].append(provider)
        if provider == "espn_public_site_api" and provider not in candidate["fallback_sources"]:
            candidate["fallback_sources"].append(provider)
        if event_id:
            candidate["source_ids"][provider] = event_id
        candidate["market_count"] += _market_count(raw)
        candidate["line_markets"].extend(
            _line_markets_from_odds_api(raw)
            if provider == "the_odds_api"
            else _line_markets_generic(provider, raw)
        )


def _extract_response_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "events", "games"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _get_json(url: str, *, params: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        response = requests.get(url, params=params, headers=headers, timeout=8)
        status = {"ok": response.ok, "status_code": response.status_code, "url": url}
        for header in ("x-requests-used", "x-requests-remaining", "x-requests-last"):
            value = response.headers.get(header)
            if value is not None:
                status[header.replace("-", "_")] = value
        if not response.ok:
            return [], {**status, "error": "http-error"}
        return _extract_response_events(response.json()), status
    except requests.RequestException as exc:
        return [], {"ok": False, "url": url, "error": exc.__class__.__name__}
    except ValueError:
        return [], {"ok": False, "url": url, "error": "invalid-json"}


def fetch_structured_events(sports: list[str], source_config: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    providers = source_config.get("providers", {})
    sports_config = source_config.get("sports", {})
    out = {"sportsgameodds": [], "the_odds_api": [], "parlay_api": [], "espn_public_site_api": []}
    statuses: list[dict[str, Any]] = []

    sgo_key = os.environ.get("SPORTSGAMEODDS_API_KEY", "")
    if sgo_key:
        sgo = providers.get("sportsgameodds", {})
        league_to_sports: dict[str, list[str]] = {}
        for sport in sports:
            league = (sports_config.get(sport) or {}).get("sportsgameodds_league_id")
            if league:
                league_to_sports.setdefault(str(league), []).append(sport)
        for league, mapped_sports in sorted(league_to_sports.items()):
            params = dict((sgo.get("default_params", {}) or {}).get("events", {}) or {})
            params["leagueID"] = league
            rows, status = _get_json(
                sgo.get("base_url", "").rstrip("/") + sgo.get("endpoints", {}).get("events", "/events"),
                params=params,
                headers={"x-api-key": sgo_key},
            )
            for row in rows:
                if len(mapped_sports) == 1:
                    row.setdefault("sport", mapped_sports[0])
                row.setdefault("leagueID", league)
            out["sportsgameodds"].extend(rows)
            statuses.append({
                "provider": "sportsgameodds",
                "leagueID": league,
                "sports": mapped_sports,
                "rows": len(rows),
                **status,
            })
    else:
        statuses.append({"provider": "sportsgameodds", "rows": 0, "ok": False, "error": "missing-api-key"})

    parlay_key = os.environ.get("PARLAY_API_KEY", "")
    parlay = providers.get("parlay_api", {})
    if parlay_key:
        auth = parlay.get("auth") or {}
        auth_header = auth.get("header") or "X-API-Key"
        for sport in sports:
            sport_keys = _odds_api_keys_for_sport(source_config, sport)
            if not sport_keys:
                continue
            for sport_key in sport_keys:
                params = dict((parlay.get("default_params", {}) or {}).get("odds", {}) or {})
                path = parlay.get("endpoints", {}).get("odds", "/sports/{sport}/odds").format(sport=sport_key)
                rows, status = _get_json(
                    parlay.get("base_url", "").rstrip("/") + path,
                    params=params,
                    headers={auth_header: parlay_key},
                )
                price_format = str(params.get("oddsFormat") or "").lower() or None
                for row in rows:
                    row.setdefault("sport_key", sport_key)
                    row.setdefault("sport", sport)
                    if price_format:
                        row.setdefault("_price_format", price_format)
                out["parlay_api"].extend(rows)
                statuses.append({"provider": "parlay_api", "sport": sport, "sport_key": sport_key, "rows": len(rows), **status})
    else:
        statuses.append({"provider": "parlay_api", "rows": 0, "ok": False, "error": "missing-api-key"})

    odds_key = os.environ.get("THE_ODDS_API_KEY", "")
    odds = providers.get("the_odds_api", {})
    if not odds_key:
        statuses.append({"provider": "the_odds_api", "rows": 0, "ok": False, "error": "missing-api-key"})
    elif not _the_odds_api_enabled():
        statuses.append({
            "provider": "the_odds_api",
            "rows": 0,
            "ok": True,
            "skipped": True,
            "skip_reason": "disabled-by-env",
        })
    else:
        floor = max(_int_env("NBABOT_THE_ODDS_API_MIN_REMAINING", 30), 0)
        sports_path = odds.get("endpoints", {}).get("sports", "/sports/")
        _, preflight = _get_json(
            odds.get("base_url", "").rstrip("/") + sports_path,
            params={"apiKey": odds_key},
        )
        remaining = _header_int(preflight, "x_requests_remaining")
        preflight_status = {
            "provider": "the_odds_api",
            "stage": "quota-preflight",
            "rows": 0,
            "credit_floor": floor,
            "credits_remaining": remaining,
            **preflight,
        }
        allow_unknown = _bool_env("NBABOT_THE_ODDS_API_ALLOW_UNKNOWN_QUOTA", False)
        skip_reason = None
        if not preflight.get("ok"):
            skip_reason = "quota-preflight-failed"
        elif remaining is None and not allow_unknown:
            skip_reason = "quota-remaining-unknown"
        elif remaining is not None and remaining < floor:
            skip_reason = "credits-below-floor"
        if skip_reason:
            statuses.append({
                **preflight_status,
                "skipped": True,
                "skip_reason": skip_reason,
            })
        else:
            statuses.append({
                **preflight_status,
                "quota_ok": True,
            })
            for sport in sports:
                sport_keys = _odds_api_keys_for_sport(source_config, sport)
                if not sport_keys:
                    continue
                for sport_key in sport_keys:
                    params = dict((odds.get("default_params", {}) or {}).get("odds", {}) or {})
                    params["apiKey"] = odds_key
                    path = odds.get("endpoints", {}).get("odds", "/sports/{sport}/odds/").format(sport=sport_key)
                    rows, status = _get_json(odds.get("base_url", "").rstrip("/") + path, params=params)
                    for row in rows:
                        row.setdefault("sport_key", sport_key)
                        row.setdefault("sport", sport)
                    out["the_odds_api"].extend(rows)
                    statuses.append({
                        "provider": "the_odds_api",
                        "sport": sport,
                        "sport_key": sport_key,
                        "rows": len(rows),
                        "credit_floor": floor,
                        **status,
                    })

    espn = providers.get("espn_public_site_api", {})
    base = espn.get("base_url", "").rstrip("/")
    for sport in sports:
        espn_path = (sports_config.get(sport) or {}).get("espn_path")
        if not espn_path:
            continue
        rows, status = _get_json(f"{base}/{espn_path}/scoreboard")
        for row in rows:
            row.setdefault("sport", sport)
        out["espn_public_site_api"].extend(rows)
        statuses.append({"provider": "espn_public_site_api", "sport": sport, "rows": len(rows), **status})
    return out, statuses


def _configured_game_candidate(settings: Any) -> dict[str, Any] | None:
    sources = getattr(settings, "game", {}).get("sources", {})
    matchup = getattr(settings, "game", {}).get("game", {}).get("matchup", "")
    keywords = sources.get("espn_matchup_keywords", [])
    away = sources.get("away_abbr")
    home = sources.get("home_abbr")
    if not away and not home and len(keywords) >= 2:
        away, home = keywords[1], keywords[0]
    if not away and not home and not matchup:
        return None
    key = _candidate_key(getattr(settings, "sport", "unknown"), away, home, getattr(settings, "game_id", "configured"))
    candidate = _empty_candidate(getattr(settings, "sport", "unknown"), key, away, home, getattr(settings, "game", {}).get("game", {}).get("tip_iso"))
    candidate["source_ids"]["config"] = getattr(settings, "game_id", "")
    candidate["sources"].append("config")
    candidate["fallback_sources"].append("config")
    candidate["reasoning"].append("Configured game file provides the current runtime target.")
    return candidate


def configured_game_candidate_id(settings: Any) -> str | None:
    candidate = _configured_game_candidate(settings)
    return candidate.get("candidate_id") if candidate else None


def build_market_basket(
    *,
    team: str,
    hit: dict[str, Any],
    slate: dict[str, Any],
    market_matches: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    team_terms = {
        _norm_text(team),
        _norm_text(hit.get("canonical_name")),
        _norm_text(hit.get("team")),
    }
    team_terms = {term for term in team_terms if term}
    match_rows_by_ticker = {
        row.get("ticker"): row
        for row in market_matches.get("rows") or []
        if isinstance(row, dict) and row.get("ticker")
    }
    tickers: list[str] = []
    event_ids: list[str] = []
    market_rows: list[dict[str, Any]] = []
    for candidate in slate.get("candidates") or []:
        candidate_text = " ".join(str(part or "") for part in (
            candidate.get("candidate_id"),
            candidate.get("away_team"),
            candidate.get("home_team"),
            candidate.get("team"),
        ))
        if team_terms and not any(term in _norm_text(candidate_text) for term in team_terms):
            continue
        candidate_id = candidate.get("candidate_id")
        if candidate_id:
            event_ids.append(str(candidate_id))
        for market in candidate.get("kalshi_markets") or []:
            ticker = market.get("ticker")
            if not ticker:
                continue
            row = match_rows_by_ticker.get(ticker, market)
            market_type = _basket_market_type(row) or _basket_market_type(market)
            if market_type not in BASKET_MARKET_TYPES:
                continue
            if ticker not in tickers:
                tickers.append(str(ticker))
            event_ticker = row.get("event_ticker") or market.get("event_ticker")
            if event_ticker:
                event_ids.append(str(event_ticker))
            market_rows.append({
                "ticker": ticker,
                "candidate_id": candidate_id,
                "event_ticker": event_ticker,
                "market_type": market_type,
                "title": row.get("title") or market.get("title"),
            })
    tickers = sorted(dict.fromkeys(tickers))
    event_ids = sorted(dict.fromkeys(event_ids))
    news_ids = [
        str(value)
        for value in (
            hit.get("id"),
            hit.get("news_item_id"),
            hit.get("content_hash"),
        )
        if value
    ]
    created_at = utc_now()
    basket_id = f"basket-{_norm_text(team) or 'team'}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    return {
        "basket_id": basket_id,
        "team": team,
        "canonical_name": hit.get("canonical_name"),
        "created_at": created_at,
        "reason": reason,
        "hit": hit,
        "event_ids": event_ids,
        "news_item_ids": sorted(dict.fromkeys(news_ids)),
        "tickers": tickers,
        "market_rows": market_rows,
        "empty": not bool(tickers),
    }


def active_market_basket(ctx: Any) -> dict[str, Any] | None:
    basket = ctx.read_json("active_market_basket.json") or {}
    if not isinstance(basket, dict) or not basket.get("basket_id"):
        return None
    return basket


def basket_metadata_for_ticker(basket: dict[str, Any] | None, ticker: str | None) -> dict[str, Any]:
    if not basket or not ticker or ticker not in set(basket.get("tickers") or []):
        return {}
    return {
        "basket_id": basket.get("basket_id"),
        "event_ids": basket.get("event_ids") or [],
        "news_item_ids": basket.get("news_item_ids") or [],
    }


def _first_external_candidate_id(match: dict[str, Any]) -> str | None:
    for item in match.get("external_matches") or []:
        candidate_id = item.get("candidate_id") if isinstance(item, dict) else None
        if candidate_id:
            return str(candidate_id)
    return None


def _identity_event_key(identity: Any) -> str | None:
    if isinstance(identity, dict):
        value = identity.get("event_key")
        return str(value) if value else None
    return None


def _broad_slate_candidate(candidate_id: str | None, configured_id: str | None) -> bool:
    if not candidate_id:
        return False
    return not configured_id or candidate_id != configured_id


def _kalshi_catalog(ctx: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing = ctx.read_json("market_catalog.json") or {}
    if existing.get("rows"):
        return existing["rows"], {"source": "artifact", "rows": len(existing["rows"]), "ok": True}
    try:
        adapter = ctx.adapter
        series = adapter.discovery_series(ctx.settings)
        raw = adapter.fetch_raw_markets(ctx.kalshi, ctx.game_tag, series)
        rows = adapter.build_catalog(ctx.settings.game_id, ctx.game_tag, raw, ctx.scenarios)
        return rows, {"source": "kalshi-fetch", "rows": len(rows), "ok": True}
    except Exception as exc:
        return [], {"source": "kalshi-fetch", "rows": 0, "ok": False, "error": exc.__class__.__name__}


def _kalshi_open_slate(ctx: Any, source_config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    broad_max_pages = max(_int_env("NBABOT_KALSHI_SLATE_MAX_PAGES", 3), 1)
    series_max_pages = max(_int_env("NBABOT_KALSHI_SERIES_MAX_PAGES", 20), 1)
    sports_filter = set(configured_sports(ctx.settings, source_config))
    raw_entries: list[tuple[dict[str, Any], list[str], str, str | None]] = []
    series_status = []
    for series, sport in _kalshi_series_for_sports(sports_filter):
        try:
            series_rows = ctx.kalshi.list_markets(
                series=series,
                status="open",
                max_pages=series_max_pages,
            )
        except Exception as exc:
            series_status.append({
                "series": series,
                "sport": sport,
                "ok": False,
                "rows": 0,
                "error": exc.__class__.__name__,
                "max_pages": series_max_pages,
            })
            continue
        series_status.append({
            "series": series,
            "sport": sport,
            "ok": True,
            "rows": len(series_rows),
            "max_pages": series_max_pages,
        })
        raw_entries.extend(
            (market, [sport], "kalshi-series-scoped", series)
            for market in series_rows
        )
    broad_status: dict[str, Any]
    try:
        broad_rows = ctx.kalshi.list_open_markets(max_pages=broad_max_pages)
        broad_status = {
            "ok": True,
            "rows": len(broad_rows),
            "raw_rows_scanned": len(broad_rows),
            "max_pages": broad_max_pages,
        }
    except Exception as exc:
        broad_rows = []
        broad_status = {
            "ok": False,
            "rows": 0,
            "raw_rows_scanned": 0,
            "error": exc.__class__.__name__,
            "max_pages": broad_max_pages,
        }
    raw_entries.extend(
        (market, [], "kalshi-open-markets", None)
        for market in broad_rows
    )
    rows_by_ticker: dict[str, dict[str, Any]] = {}
    for market, scoped_sports, source, series in raw_entries:
        title = market.get("title") or ""
        sports = scoped_sports or [
            sport for sport in _classify_kalshi_sports(title, source_config)
            if sport in sports_filter or sport == "kalshi_sports"
        ]
        if not sports:
            continue
        q = _kalshi_quote_fields(market)
        if not q["has_live_quote"] and os.environ.get("NBABOT_INCLUDE_ZERO_QUOTE_MARKETS", "0") not in ("1", "true", "True"):
            continue
        ticker = market.get("ticker")
        if not ticker or ticker in rows_by_ticker:
            continue
        rows_by_ticker[ticker] = {
            "ticker": market.get("ticker"),
            "event_ticker": market.get("event_ticker"),
            "series_ticker": series or market.get("series_ticker") or market.get("series"),
            "title": title,
            "yes_sub_title": market.get("yes_sub_title"),
            "no_sub_title": market.get("no_sub_title"),
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
            "sport_keys": sports,
            "components": _kalshi_components(title),
            "custom_strike": market.get("custom_strike"),
            "strike_type": market.get("strike_type"),
            "floor_strike": market.get("floor_strike"),
            "cap_strike": market.get("cap_strike"),
            "mve_selected_legs": market.get("mve_selected_legs") or [],
            "mve_collection_ticker": market.get("mve_collection_ticker"),
            "close_time": market.get("close_time"),
            "expiration_time": market.get("expiration_time"),
            "status": market.get("status"),
            "market_type": market.get("market_type"),
            "is_composite": _kalshi_composite_market(market),
            "source": source,
            "mapping_status": "open_kalshi",
            "mapped_markets": [],
            "mapped_scenarios": [],
            **q,
        }
    rows = list(rows_by_ticker.values())
    rows.sort(
        key=lambda row: (
            row.get("source") != "kalshi-series-scoped",
            bool(row.get("is_composite")),
            not row.get("has_live_quote"),
            row.get("spread_cents") if row.get("spread_cents") is not None else 999,
            -(row.get("bid") or 0),
        )
    )
    limit = max(_int_env("NBABOT_KALSHI_SLATE_LIMIT", 200), 1)
    limited = rows[:limit]
    ok = bool(rows) or any(item.get("ok") for item in series_status) or broad_status.get("ok")
    return rows[:limit], {
        "source": "kalshi-open-markets",
        "ok": ok,
        "rows": len(limited),
        "raw_rows_scanned": len(raw_entries),
        "series_rows_scanned": sum(int(item.get("rows") or 0) for item in series_status),
        "broad_rows_scanned": len(broad_rows),
        "series_queries": series_status,
        "series_max_pages": series_max_pages,
        "broad_scan": broad_status,
        "max_pages": broad_max_pages,
    }


def _attach_open_kalshi_candidates(
    candidates: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        sport = (row.get("sport_keys") or ["kalshi_sports"])[0]
        key = f"kalshi:{ticker}"
        candidate = candidates.setdefault(
            key,
            _empty_candidate(sport, key, None, None, row.get("close_time")),
        )
        candidate["source_ids"]["kalshi"] = ticker
        for source in ("kalshi", "kalshi-open-markets"):
            if source not in candidate["sources"]:
                candidate["sources"].append(source)
        if "kalshi" not in candidate["structured_sources"]:
            candidate["structured_sources"].append("kalshi")
        market = {
            "ticker": ticker,
            "event_ticker": row.get("event_ticker"),
            "series_ticker": row.get("series_ticker"),
            "title": row.get("title"),
            "yes_sub_title": row.get("yes_sub_title"),
            "no_sub_title": row.get("no_sub_title"),
            "rules_primary": row.get("rules_primary"),
            "rules_secondary": row.get("rules_secondary"),
            "sport_keys": row.get("sport_keys"),
            "components": row.get("components"),
            "custom_strike": row.get("custom_strike"),
            "strike_type": row.get("strike_type"),
            "floor_strike": row.get("floor_strike"),
            "cap_strike": row.get("cap_strike"),
            "mve_selected_legs": row.get("mve_selected_legs") or [],
            "mve_collection_ticker": row.get("mve_collection_ticker"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "mid": row.get("mid"),
            "implied": row.get("implied"),
            "spread_cents": row.get("spread_cents"),
            "has_live_quote": row.get("has_live_quote"),
            "close_time": row.get("close_time"),
            "mapping_status": row.get("mapping_status"),
            "is_composite": row.get("is_composite"),
            "source": row.get("source"),
        }
        candidate["kalshi_markets"].append(market)
        candidate["open_kalshi_market_count"] = len(candidate["kalshi_markets"])
        candidate["reasoning"].append("Open Kalshi sports market discovered from scoped/broad exchange scan.")


def attach_kalshi_markets(ctx: Any, candidates: dict[str, dict[str, Any]],
                          source_config: dict[str, Any]) -> dict[str, Any]:
    rows, status = _kalshi_catalog(ctx)
    open_rows, open_status = _kalshi_open_slate(ctx, source_config)
    _attach_open_kalshi_candidates(candidates, open_rows)
    mapped = [row for row in rows if row.get("ticker")]
    configured = _configured_game_candidate(ctx.settings)
    if configured:
        candidate = candidates.setdefault(configured["candidate_id"], configured)
        if "config" not in candidate["sources"]:
            candidate["sources"].append("config")
            candidate["fallback_sources"].append("config")
    if candidates and mapped:
        target = next(iter(candidates.values()))
        if configured and configured["candidate_id"] in candidates:
            target = candidates[configured["candidate_id"]]
        target["kalshi_markets"].extend([
            {
                "ticker": row.get("ticker"),
                "title": row.get("title"),
                "market": row.get("mapped_markets"),
                "scenario_ids": row.get("mapped_scenarios"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "implied": row.get("implied"),
                "mapping_status": row.get("mapping_status"),
            }
            for row in mapped[:200]
        ])
        target["mapped_kalshi_markets"] = [
            row for row in target["kalshi_markets"]
            if row.get("mapping_status") == "mapped"
        ]
        if "kalshi" not in target["sources"]:
            target["sources"].append("kalshi")
        if "kalshi" not in target["structured_sources"]:
            target["structured_sources"].append("kalshi")
    return {
        "configured_catalog": status,
        "open_slate": open_status,
    }


def score_slate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for candidate in candidates:
        structured = set(candidate.get("structured_sources") or [])
        kalshi_count = len(candidate.get("kalshi_markets") or [])
        mapped_count = len(candidate.get("mapped_kalshi_markets") or [])
        line_count = len(candidate.get("line_markets") or [])
        has_book = bool(STRUCTURED_BOOK_PROVIDERS & structured)
        has_kalshi = "kalshi" in structured
        open_kalshi = bool(candidate.get("open_kalshi_market_count"))
        score = (4 if has_kalshi else 0) + (3 if has_book else 0) + min(mapped_count, 5) + min(line_count, 5) / 10
        if open_kalshi:
            score += 1
        candidate["discovery_score"] = round(score, 3)
        candidate["confidence"] = (
            "high" if has_kalshi and has_book and mapped_count
            else "medium" if has_kalshi and has_book
            else "low"
        )
        candidate["status"] = "candidate" if has_kalshi and has_book else "watch_only"
        if has_kalshi:
            candidate["reasoning"].append("Kalshi market mapping makes the event tradable after risk approval.")
        if has_book:
            candidate["reasoning"].append("At least one structured odds feed is present for line comparison.")
        if not has_book:
            candidate["reasoning"].append("No structured odds feed matched this event; do not use for execution.")
        if open_kalshi:
            candidate["reasoning"].append(f"{candidate['open_kalshi_market_count']} open Kalshi sports markets were found.")
        if mapped_count:
            candidate["reasoning"].append(f"{mapped_count} mapped Kalshi scenario markets are available.")
        elif kalshi_count:
            candidate["reasoning"].append("Kalshi markets exist, but none are mapped to scenario legs yet.")
    return sorted(candidates, key=lambda c: c.get("discovery_score", 0), reverse=True)


def discover_slate(ctx: Any) -> dict[str, Any]:
    source_config = load_source_config()
    sports = configured_sports(ctx.settings, source_config)
    provider_events, provider_status = fetch_structured_events(sports, source_config)
    candidates: dict[str, dict[str, Any]] = {}
    for provider, rows in provider_events.items():
        for sport in sports:
            sport_rows = [
                row for row in rows
                if _row_matches_sport(row, source_config, sport)
                or (provider == "sportsgameodds" and not row.get("sport") and not row.get("sport_key"))
            ]
            _merge_provider_events(provider, sport, sport_rows, candidates)
    kalshi_status = attach_kalshi_markets(ctx, candidates, source_config)
    ranked = score_slate_candidates(list(candidates.values()))
    return {
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "sports": sports,
        "provider_status": provider_status,
        "kalshi_status": kalshi_status,
        "candidate_count": len(ranked),
        "candidates": ranked,
        "guardrail_footer": GUARDRAIL_FOOTER,
    }


def verify_slate(ctx: Any, slate: dict[str, Any]) -> dict[str, Any]:
    candidates = slate.get("candidates") or []
    duplicate_keys = {c["candidate_id"] for c in candidates if sum(x["candidate_id"] == c["candidate_id"] for x in candidates) > 1}
    learning_log = ctx.settings.data_path("log.jsonl")
    backtest_artifact = ctx.settings.data_path("backtest.json")
    rows = []
    for candidate in candidates:
        structured = set(candidate.get("structured_sources") or [])
        hidden_only = structured <= set() and candidate.get("fallback_sources")
        has_structured_book = bool(STRUCTURED_BOOK_PROVIDERS & structured)
        has_kalshi = "kalshi" in structured or bool(candidate.get("kalshi_markets"))
        mapped = bool(candidate.get("mapped_kalshi_markets"))
        reasons = []
        if not has_structured_book:
            reasons.append("missing structured sportsbook source")
        if not has_kalshi:
            reasons.append("missing Kalshi market mapping")
        if not mapped:
            reasons.append("missing mapped scenario market")
        if hidden_only:
            reasons.append("fallback/hidden sources only")
        if candidate["candidate_id"] in duplicate_keys:
            reasons.append("duplicate candidate identity")
        needs_backtest = not learning_log.exists() or not backtest_artifact.exists()
        if needs_backtest:
            reasons.append("needs local learning-log/backtest coverage before autonomous sizing confidence")
        approved = has_structured_book and has_kalshi and mapped and not hidden_only and candidate["candidate_id"] not in duplicate_keys
        rows.append({
            "candidate_id": candidate["candidate_id"],
            "status": "verified" if approved else "needs_review",
            "approved_for_research": approved,
            "approved_for_execution": approved and not needs_backtest,
            "requires_more_backtest_data": needs_backtest,
            "structured_sources": sorted(structured),
            "reasons": reasons,
        })
    return {
        "game_id": ctx.settings.game_id,
        "verified_at": utc_now(),
        "candidate_count": len(rows),
        "verified_count": sum(1 for row in rows if row["approved_for_research"]),
        "execution_ready_count": sum(1 for row in rows if row["approved_for_execution"]),
        "learning_log_exists": learning_log.exists(),
        "backtest_artifact_exists": backtest_artifact.exists(),
        "rows": rows,
        "guardrail_footer": GUARDRAIL_FOOTER,
    }


def _book_metrics(ctx: Any) -> dict[str, dict[str, Any]]:
    payload = ctx.read_json("book_watch.json") or {}
    snapshots = payload.get("snapshots") or []
    metrics: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        for ticker, side_metrics in (snap.get("metrics") or {}).items():
            metrics[ticker] = side_metrics
    return metrics


def _rank_entry_price(rank: dict[str, Any], fallback: Any) -> Any:
    executable = rank.get("executable_price") if isinstance(rank.get("executable_price"), dict) else {}
    if executable.get("price_cents") is not None:
        return executable.get("price_cents")
    for key in ("vwap_cents", "ask_cents", "bid_cents"):
        if executable.get(key) is not None:
            return executable.get(key)
    return fallback


def _rank_edge_fields(rank: dict[str, Any], fallback_edge: float | None) -> dict[str, Any]:
    raw_edge = rank.get("raw_edge")
    if raw_edge is None:
        raw_edge = fallback_edge
    net_edge = rank.get("net_edge")
    if net_edge is None:
        net_edge = rank.get("edge", fallback_edge)
    return {
        "raw_edge": round(float(raw_edge), 5) if raw_edge is not None else None,
        "net_edge": round(float(net_edge), 5) if net_edge is not None else None,
        "edge": round(float(net_edge), 5) if net_edge is not None else None,
        "expected_fee_cents": rank.get("expected_fee_cents"),
        "expected_fee_prob": rank.get("expected_fee_prob"),
        "liquidity_role": rank.get("liquidity_role") or "maker",
    }


def research_bundle(ctx: Any, slate: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    snapshot = ctx.read_json("market_snapshot.json") or {}
    rows = snapshot.get("rows") or []
    matcher = ctx.read_json("market_matches.json") or {}
    match_by_ticker = {
        row.get("ticker"): row
        for row in matcher.get("rows", [])
        if row.get("ticker")
    }
    ranker = ctx.read_json("candidate_ranker.json") or {}
    rank_by_ticker = {
        row.get("ticker"): row
        for row in ranker.get("rows", [])
        if row.get("ticker")
    }
    active_basket = active_market_basket(ctx)
    input_freshness = artifact_freshness(ctx, (
        "market_snapshot.json",
        "slate_candidates.json",
        "slate_verification.json",
        "book_watch.json",
        "market_matches.json",
        "candidate_ranker.json",
    ))
    verified_ids = {
        row["candidate_id"]
        for row in verification.get("rows", [])
        if row.get("approved_for_research")
    }
    slate_candidates = slate.get("candidates") or []
    verified_sources = sorted({
        source
        for candidate in slate_candidates
        if candidate.get("candidate_id") in verified_ids
        for source in (candidate.get("structured_sources") or [])
    })
    metrics = _book_metrics(ctx)
    configured_candidate_id = configured_game_candidate_id(ctx.settings)
    execution_mode = str(getattr(ctx.settings, "execution_mode", "paper") or "paper").lower()
    paper_demo_mode = execution_mode in {"paper", "demo"}
    try:
        performance_learning = performance_learner.learn_from_store(
            ResearchStore(ctx.settings.research_db_path),
            default_sport=getattr(ctx.settings, "sport", None),
        )
    except Exception as exc:
        performance_learning = performance_learner.learn(
            [],
            default_sport=getattr(ctx.settings, "sport", None),
        )
        performance_learning["error"] = exc.__class__.__name__
    market_candidates = []
    seen_tickers = set()
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        seen_tickers.add(ticker)
        implied = row.get("implied")
        prior = row.get("prior_p")
        edge = (float(prior) - float(implied)) if implied is not None and prior is not None else None
        book = metrics.get(ticker, {})
        match = match_by_ticker.get(ticker, {})
        rank = rank_by_ticker.get(ticker, {}) or {}
        edge_fields = _rank_edge_fields(rank, edge)
        candidate_id = (
            row.get("candidate_id")
            or rank.get("candidate_id")
            or _first_external_candidate_id(match)
        )
        market_identity = rank.get("market_identity")
        spread = row.get("ask") - row.get("bid") if row.get("ask") is not None and row.get("bid") is not None else None
        blockers = []
        for artifact, report in input_freshness.items():
            if artifact == "candidate_ranker.json":
                continue
            if not report["fresh"]:
                blockers.append(f"{report['artifact']} {report['reason']}")
        if not verified_ids:
            blockers.append("slate verifier has no approved research candidates")
        min_edge = float(getattr(
            ctx.settings,
            "execution_min_edge",
            getattr(ctx.settings, "min_edge", 0.05),
        ))
        if edge_fields["edge"] is None:
            blockers.append("missing model-vs-market edge")
        elif float(edge_fields["edge"]) < min_edge:
            blockers.append(f"edge below configured minimum {min_edge:.3f}")
        if row.get("sgp_adjusted_prob") is None:
            blockers.append("missing SGP-adjusted scenario probability")
        if spread is None or spread > int(ctx.settings.max_spread_cents):
            blockers.append("spread missing or wider than configured max")
        if not book:
            blockers.append("missing order-book verification")
        side_metrics = book.get(row.get("side") or "yes") if isinstance(book, dict) else None
        if isinstance(side_metrics, dict) and side_metrics.get("fillable_contracts", 0) == 0:
            blockers.append("no fillable contracts at observed limit")
        trade_eligible = not blockers
        candidate_row = {
            "ticker": ticker,
            "candidate_id": candidate_id,
            "event_key": _identity_event_key(market_identity),
            "broad_slate": _broad_slate_candidate(candidate_id, configured_candidate_id),
            "scenario_id": row.get("scenario_id"),
            "market": row.get("market"),
            "label": row.get("label"),
            "side": row.get("side"),
            "captured_at": row.get("captured_at"),
            "entry_price_cents": _rank_entry_price(rank, row.get("entry_price_cents")),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "implied": implied,
            "prior_p": prior,
            **edge_fields,
            "sgp_adjusted_prob": row.get("sgp_adjusted_prob"),
            "risk": row.get("risk"),
            "book_metrics": book,
            "orderbook_delta": match.get("orderbook_delta"),
            "external_matches": match.get("external_matches") or [],
            "market_identity": market_identity,
            "is_composite": bool(rank.get("is_composite")),
            "signal_source": rank.get("signal_source") or "consensus",
            "qual_signal": rank.get("qual_signal"),
            "qual_vs_consensus_delta": rank.get("qual_vs_consensus_delta"),
            "qaq_investigation": rank.get("qaq_investigation"),
            "qaq_evidence": rank.get("qaq_evidence"),
            "signal_engine": rank.get("signal_engine"),
            "confluence": rank.get("confluence"),
            "confluence_verdict": rank.get("confluence_verdict") or "none",
            "confluence_shadow": bool(rank.get("confluence_shadow")),
            "freshness": {
                "inputs": input_freshness,
                "market_match": match.get("freshness"),
            },
            "structured_sources": verified_sources,
            "trade_eligible": trade_eligible,
            "blockers": blockers,
            "reasoning": [
                "Candidate comes from mapped Kalshi scenario market.",
                "Structured slate verification is required before execution.",
                "Existing risk.py remains the final execution gate.",
            ],
            **basket_metadata_for_ticker(active_basket, ticker),
        }
        performance_learner.annotate_candidate(
            candidate_row,
            performance_learning,
            default_sport=getattr(ctx.settings, "sport", None),
        )
        market_candidates.append(candidate_row)
    for candidate in slate_candidates:
        for market in candidate.get("kalshi_markets") or []:
            ticker = market.get("ticker")
            if not ticker or ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)
            book = metrics.get(ticker, {})
            match = match_by_ticker.get(ticker, {})
            rank = rank_by_ticker.get(ticker, {})
            candidate_id = candidate.get("candidate_id") or rank.get("candidate_id")
            model_prob = rank.get("model_prob")
            rank_edge = rank.get("edge")
            edge_fields = _rank_edge_fields(rank, rank.get("raw_edge"))
            sgp_adjusted_prob = rank.get("sgp_adjusted_prob")
            if sgp_adjusted_prob is None and model_prob is not None and not rank.get("is_composite"):
                sgp_adjusted_prob = model_prob
            blockers = []
            if not paper_demo_mode:
                blockers.extend([
                    "not mapped to a configured scenario leg",
                    "missing SGP-adjusted scenario probability",
                    "slate verifier has not approved this market for execution",
                ])
            if model_prob is None or rank_edge is None:
                blockers.append("edge engine has not evaluated this market")
            elif not rank.get("passes_edge"):
                blockers.extend(rank.get("blockers") or ["edge engine did not pass this market"])
            elif sgp_adjusted_prob is None:
                blockers.append("missing SGP-adjusted scenario probability")
            for report in input_freshness.values():
                if not report["fresh"]:
                    blockers.append(f"{report['artifact']} {report['reason']}")
            if not book:
                blockers.append("missing order-book verification")
            trade_eligible = paper_demo_mode and not blockers
            candidate_row = {
                "ticker": ticker,
                "candidate_id": candidate_id,
                "event_key": _identity_event_key(rank.get("market_identity")),
                "broad_slate": _broad_slate_candidate(candidate_id, configured_candidate_id),
                "scenario_id": None,
                "market": "open_kalshi_sports",
                "label": market.get("title"),
                "side": "yes",
                "captured_at": match.get("captured_at") or market.get("captured_at"),
                "entry_price_cents": _rank_entry_price(rank, market.get("ask") or market.get("mid")),
                "bid": market.get("bid"),
                "ask": market.get("ask"),
                "implied": market.get("implied"),
                "prior_p": model_prob,
                "model_prob": model_prob,
                "model_prob_sources": rank.get("model_prob_sources") or [],
                "signal_source": rank.get("signal_source") or "consensus",
                "qual_signal": rank.get("qual_signal"),
                "qual_vs_consensus_delta": rank.get("qual_vs_consensus_delta"),
                "qaq_investigation": rank.get("qaq_investigation"),
                "qaq_evidence": rank.get("qaq_evidence"),
                "signal_engine": rank.get("signal_engine"),
                "confluence": rank.get("confluence"),
                "confluence_verdict": rank.get("confluence_verdict") or "none",
                "confluence_shadow": bool(rank.get("confluence_shadow")),
                **edge_fields,
                "required_edge": rank.get("required_edge"),
                "book_count": rank.get("book_count"),
                "disagreement_std": rank.get("disagreement_std"),
                "sgp_adjusted_prob": sgp_adjusted_prob,
                "risk": None,
                "book_metrics": book,
                "orderbook_delta": match.get("orderbook_delta"),
                "external_matches": match.get("external_matches") or [],
                "market_identity": rank.get("market_identity"),
                "is_composite": bool(rank.get("is_composite") or market.get("is_composite")),
                "execution_review": bool(match.get("execution_review")),
                "freshness": {
                    "inputs": input_freshness,
                    "market_match": match.get("freshness"),
                },
                "structured_sources": candidate.get("structured_sources") or [],
                "trade_eligible": trade_eligible,
                "blockers": blockers,
                "reasoning": [
                    "Open Kalshi sports market discovered by broad slate scan.",
                    "Single-market paper/demo execution uses the ranker model probability as the SGP-adjusted probability.",
                    "Live execution still requires explicit live gates and risk.py approval.",
                ],
                **basket_metadata_for_ticker(active_basket, ticker),
            }
            performance_learner.annotate_candidate(
                candidate_row,
                performance_learning,
                default_sport=getattr(ctx.settings, "sport", None),
            )
            market_candidates.append(candidate_row)
    market_candidates.sort(key=lambda item: item.get("edge") if item.get("edge") is not None else -999, reverse=True)
    return {
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "input_freshness": input_freshness,
        "candidate_count": len(market_candidates),
        "trade_eligible_count": sum(1 for row in market_candidates if row["trade_eligible"]),
        "structured_sources": verified_sources,
        "performance_learning": performance_learning,
        "market_candidates": market_candidates,
        "notes": [
            "Research-agent does not place orders.",
            "Social and hidden ESPN fallback sources are context only.",
            "Live execution still requires explicit live gates and risk approval.",
        ],
        "guardrail_footer": GUARDRAIL_FOOTER,
    }
