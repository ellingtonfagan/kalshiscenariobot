"""Normalize Kalshi NBA markets into a local discovery catalog."""
from __future__ import annotations

from typing import Any

from .kalshi import GAME_SERIES, STAT_SERIES, KalshiClient, _TITLE_RE, _norm_stat
from .research import utc_now
from .scenarios import Scenario

DEFAULT_DISCOVERY_SERIES = tuple(dict.fromkeys([*STAT_SERIES.values(), GAME_SERIES]))


def _price_cents(raw: Any) -> int:
    try:
        return int(round(float(raw) * 100)) if raw else 0
    except (TypeError, ValueError):
        return 0


def _quote_fields(market: dict[str, Any]) -> dict[str, int | float]:
    bid = _price_cents(market.get("yes_bid_dollars"))
    ask = _price_cents(market.get("yes_ask_dollars"))
    mid = round((bid + ask) / 2) if bid and ask else bid or ask
    return {"bid": bid, "ask": ask, "mid": mid, "implied": min(max(mid / 100.0, 0.0), 1.0)}


def _team_from_game_ticker(ticker: str) -> str | None:
    parts = ticker.split("-")
    return parts[-1] if len(parts) >= 2 else None


def normalize_market(game_id: str, game_tag: str, series: str,
                     market: dict[str, Any], captured_at: str) -> dict[str, Any]:
    """Return one catalog row from a raw Kalshi market dict."""
    title = market.get("title", "")
    ticker = market.get("ticker", "")
    parsed = _TITLE_RE.match(title)
    player = parsed.group("name").strip() if parsed else None
    line = float(parsed.group("line")) if parsed else None
    stat = _norm_stat(parsed.group("stat")) if parsed else None
    team = _team_from_game_ticker(ticker) if series == GAME_SERIES else None
    q = _quote_fields(market)
    return {
        "game_id": game_id,
        "game_tag": game_tag,
        "captured_at": captured_at,
        "series": series,
        "ticker": ticker,
        "title": title,
        "player": player,
        "player_key": player.split()[-1].lower() if player else None,
        "stat": stat,
        "line": line,
        "team": team,
        "bid": q["bid"],
        "ask": q["ask"],
        "mid": q["mid"],
        "implied": q["implied"],
        "mapped_scenarios": [],
        "mapped_markets": [],
        "mapping_status": "unmatched",
        "source": "kalshi",
    }


def _matches_leg(row: dict[str, Any], scenario: Scenario, market_key: str) -> bool:
    for leg in scenario.legs:
        if leg.market != market_key:
            continue
        if leg.special == "win":
            return bool(row.get("team") and leg.team == row.get("team"))
        if leg.player and leg.stat:
            same_player = leg.player == row.get("player_key")
            same_stat = leg.stat == row.get("stat")
            same_line = row.get("line") is not None and int(leg.line) == int(row["line"])
            return same_player and same_stat and same_line
    return False


def apply_scenario_matches(rows: list[dict[str, Any]],
                           scenarios: list[Scenario]) -> list[dict[str, Any]]:
    for row in rows:
        markets: set[str] = set()
        scenario_ids: set[str] = set()
        for scenario in scenarios:
            for leg in scenario.legs:
                if _matches_leg(row, scenario, leg.market):
                    markets.add(leg.market)
                    scenario_ids.add(scenario.id)
        if markets:
            row["mapped_markets"] = sorted(markets)
            row["mapped_scenarios"] = sorted(scenario_ids)
            row["mapping_status"] = "mapped"
    return rows


def build_catalog(game_id: str, game_tag: str, raw_markets: list[dict[str, Any]],
                  scenarios: list[Scenario]) -> list[dict[str, Any]]:
    captured_at = utc_now()
    rows = [
        normalize_market(game_id, game_tag, m.get("series", ""), m, captured_at)
        for m in raw_markets
    ]
    apply_scenario_matches(rows, scenarios)
    rows.sort(key=lambda r: (r["mapping_status"] != "mapped", r["series"], r["ticker"]))
    return rows


def fetch_raw_markets(kalshi: KalshiClient, game_tag: str,
                      series: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for ticker in series:
        for market in kalshi.list_markets(ticker, game_tag):
            row = dict(market)
            row["series"] = ticker
            raw.append(row)
    return raw
