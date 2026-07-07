"""Historical broad-slate replay using the live edge engine's pure core.

Network and cache handling live here; identity resolution, de-vigging, consensus,
and edge math stay in their existing pure modules.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from . import calibration
from .candidate_ranker import build_candidate_rankings
from .market_identity import MarketIdentity, resolve_kalshi_market
from .research import to_json, utc_now
from .settlement_audit import market_resolution


ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_ODDS_MARKETS = "h2h"
DEFAULT_ODDS_REGIONS = "us"
DEFAULT_SAMPLE_SIZE = 12
DEFAULT_MAX_ODDS_API_CALLS = 4
DEFAULT_DECISION_MINUTES_BEFORE_CLOSE = 120

ODDS_SPORTS = {
    "baseball_mlb": {"local_sport": "mlb", "kalshi_series": "KXMLBGAME"},
    "basketball_wnba": {"local_sport": "wnba", "kalshi_series": "KXWNBAGAME"},
    "basketball_nba": {"local_sport": "nba", "kalshi_series": "KXNBAGAME"},
    "americanfootball_nfl": {"local_sport": "nfl", "kalshi_series": "KXNFLGAME"},
}
DEFAULT_SPORTS = ("baseball_mlb", "basketball_wnba")


class LookAheadViolation(ValueError):
    """Raised when a historical snapshot is later than its decision time."""


@dataclass(frozen=True)
class HistoricalFetch:
    provider: str
    cache_key: str
    cache_path: str
    cached: bool
    body: Any
    headers: dict[str, str]
    credits_charged: int | None = None


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


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def _num(raw: Any) -> float | None:
    try:
        if raw in (None, ""):
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _cents(raw: Any) -> int | None:
    value = _num(raw)
    if value is None:
        return None
    if 0 <= value <= 1:
        value *= 100
    if value < 0 or value > 100:
        return None
    return int(round(value))


def _cache_key(provider: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"provider": provider, **payload},
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _cache_path(cache_dir: Path, prefix: str, key: str) -> Path:
    return cache_dir / f"{prefix}_{key}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(payload) + "\n")


def odds_cache_path(
    cache_dir: Path,
    *,
    sport: str,
    date: str,
    regions: str,
    markets: str,
    odds_format: str,
) -> Path:
    key = _cache_key("the_odds_api_historical_odds", {
        "sport": sport,
        "date": date,
        "regions": regions,
        "markets": markets,
        "odds_format": odds_format,
    })
    return _cache_path(cache_dir, "odds_api", key)


def fetch_historical_odds(
    *,
    api_key: str,
    sport: str,
    date: datetime,
    cache_dir: Path,
    regions: str = DEFAULT_ODDS_REGIONS,
    markets: str = DEFAULT_ODDS_MARKETS,
    odds_format: str = "american",
    session: Any = requests,
) -> HistoricalFetch:
    """Fetch/cache The Odds API's historical odds snapshot.

    The cache key intentionally excludes the API key so cached fixtures can be
    reused without writing secrets to disk.
    """
    date_iso = _iso(date)
    path = odds_cache_path(
        cache_dir,
        sport=sport,
        date=date_iso,
        regions=regions,
        markets=markets,
        odds_format=odds_format,
    )
    cached = _read_cache(path)
    if cached is not None:
        return HistoricalFetch(
            provider="the_odds_api",
            cache_key=path.stem,
            cache_path=str(path),
            cached=True,
            body=cached.get("body"),
            headers=dict(cached.get("headers") or {}),
            credits_charged=0,
        )

    if not api_key:
        raise RuntimeError("THE_ODDS_API_KEY is required for uncached historical odds")
    url = f"{ODDS_API_BASE}/historical/sports/{sport}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "date": date_iso,
    }
    response = session.get(url, params=params, timeout=12)
    if response.status_code >= 400:
        detail = response.text[:200].replace(api_key, "[redacted]")
        raise RuntimeError(f"The Odds API HTTP {response.status_code}: {detail}")
    body = response.json()
    headers = {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower().startswith("x-requests")
    }
    last = headers.get("x-requests-last")
    credits = int(last) if last and str(last).isdigit() else None
    _write_cache(path, {
        "provider": "the_odds_api",
        "fetched_at": utc_now(),
        "request": {
            "sport": sport,
            "date": date_iso,
            "regions": regions,
            "markets": markets,
            "odds_format": odds_format,
        },
        "headers": headers,
        "body": body,
    })
    return HistoricalFetch(
        provider="the_odds_api",
        cache_key=path.stem,
        cache_path=str(path),
        cached=False,
        body=body,
        headers=headers,
        credits_charged=credits,
    )


def fetch_kalshi_market(client: Any, ticker: str, cache_dir: Path) -> HistoricalFetch:
    key = _cache_key("kalshi_market", {"ticker": ticker})
    path = _cache_path(cache_dir, "kalshi_market", key)
    cached = _read_cache(path)
    if cached is not None:
        return HistoricalFetch("kalshi_market", path.stem, str(path), True, cached.get("body"), {})
    body = client.market(ticker)
    _write_cache(path, {
        "provider": "kalshi_market",
        "fetched_at": utc_now(),
        "request": {"ticker": ticker},
        "body": body,
    })
    return HistoricalFetch("kalshi_market", path.stem, str(path), False, body, {})


def fetch_kalshi_candlesticks(
    client: Any,
    *,
    ticker: str,
    series_ticker: str,
    decision_time: datetime,
    cache_dir: Path,
    lookback_hours: int = 24,
    period_interval: int = 1,
) -> HistoricalFetch:
    end_ts = _unix(decision_time)
    start_ts = _unix(decision_time - timedelta(hours=lookback_hours))
    key = _cache_key("kalshi_candlesticks", {
        "ticker": ticker,
        "series_ticker": series_ticker,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": period_interval,
    })
    path = _cache_path(cache_dir, "kalshi_candles", key)
    cached = _read_cache(path)
    if cached is not None:
        return HistoricalFetch("kalshi_candlesticks", path.stem, str(path), True, cached.get("body"), {})

    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
    live_path = f"/trade-api/v2/series/{series_ticker}/markets/{ticker}/candlesticks"
    historical_path = f"/trade-api/v2/historical/markets/{ticker}/candlesticks"
    errors: list[str] = []
    body: dict[str, Any] | None = None
    endpoint = live_path
    try:
        body = client._get(live_path, {**params, "include_latest_before_start": True})
    except Exception as exc:
        errors.append(f"live:{type(exc).__name__}:{exc}")
    if not body or not body.get("candlesticks"):
        try:
            endpoint = historical_path
            body = client._get(historical_path, params)
        except Exception as exc:
            errors.append(f"historical:{type(exc).__name__}:{exc}")
            if body is None:
                raise
    body = body or {"ticker": ticker, "candlesticks": []}
    _write_cache(path, {
        "provider": "kalshi_candlesticks",
        "fetched_at": utc_now(),
        "request": {
            "endpoint": endpoint,
            "ticker": ticker,
            "series_ticker": series_ticker,
            **params,
        },
        "errors": errors,
        "body": body,
    })
    return HistoricalFetch("kalshi_candlesticks", path.stem, str(path), False, body, {})


def select_candlestick_at_or_before(
    candlesticks: Iterable[dict[str, Any]],
    decision_time: datetime,
) -> dict[str, Any] | None:
    """Return the latest Kalshi candle whose end timestamp is <= decision time."""
    cutoff = _unix(decision_time)
    eligible = []
    for candle in candlesticks:
        try:
            end_ts = int(candle.get("end_period_ts"))
        except (TypeError, ValueError):
            continue
        if end_ts <= cutoff:
            eligible.append((end_ts, candle))
    if not eligible:
        return None
    return sorted(eligible, key=lambda row: row[0])[-1][1]


def _close_value(section: Any) -> Any:
    if not isinstance(section, dict):
        return None
    for key in ("close_dollars", "close", "previous_dollars", "previous"):
        if section.get(key) not in (None, ""):
            return section.get(key)
    return None


def executable_from_candlestick(
    candle: dict[str, Any],
    decision_time: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bid = _cents(_close_value(candle.get("yes_bid")))
    ask = _cents(_close_value(candle.get("yes_ask")))
    traded = _cents(_close_value(candle.get("price")))
    price = ask if ask is not None else traded
    if bid is None and traded is not None:
        bid = traded
    if ask is None and traded is not None:
        ask = traded
    spread = ask - bid if ask is not None and bid is not None else None
    captured_at = _iso(decision_time)
    metrics = {
        "yes": {
            "best_bid_cents": bid,
            "best_ask_cents": ask,
            "spread_cents": spread,
            "fillable_contracts": 1 if price is not None else 0,
            "vwap_cents": price,
            "captured_at": captured_at,
        }
    }
    quote = {
        "bid": bid,
        "ask": ask,
        "mid": round((bid + ask) / 2) if bid is not None and ask is not None else price,
        "implied": (price / 100.0) if price is not None else None,
        "spread_cents": spread,
        "price_source": "yes_ask_close" if _close_value(candle.get("yes_ask")) is not None else "price_close",
        "candle_end_period_ts": candle.get("end_period_ts"),
    }
    return metrics, quote


def _events_from_odds_payload(payload: Any) -> tuple[str | None, list[dict[str, Any]]]:
    if isinstance(payload, dict):
        events = payload.get("data")
        if events is None:
            events = payload.get("body")
        return payload.get("timestamp"), events if isinstance(events, list) else []
    return None, payload if isinstance(payload, list) else []


def historical_odds_to_slate(
    payload: Any,
    *,
    odds_sport: str,
    decision_time: datetime,
    markets: str = DEFAULT_ODDS_MARKETS,
) -> dict[str, Any]:
    """Convert a historical The Odds API snapshot into the live slate shape.

    Bookmaker or market rows with a last_update after decision_time are discarded
    even though the provider promises snapshots at or before the requested date.
    """
    snapshot_raw, events = _events_from_odds_payload(payload)
    snapshot_dt = _parse_dt(snapshot_raw)
    if snapshot_dt and snapshot_dt > decision_time.astimezone(timezone.utc):
        raise LookAheadViolation(
            f"odds snapshot {snapshot_raw} is after decision {_iso(decision_time)}"
        )
    sport_meta = ODDS_SPORTS.get(odds_sport, {})
    local_sport = sport_meta.get("local_sport", odds_sport)
    wanted = {part.strip() for part in str(markets or DEFAULT_ODDS_MARKETS).split(",") if part.strip()}
    candidates = []
    filtered_future_rows = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        candidate = {
            "candidate_id": f"{local_sport}:{event.get('id') or len(candidates)}",
            "sport": local_sport,
            "league": local_sport.upper(),
            "away_team": event.get("away_team"),
            "home_team": event.get("home_team"),
            "start_time": event.get("commence_time"),
            "structured_sources": ["the_odds_api"],
            "line_markets": [],
        }
        for book in event.get("bookmakers") or []:
            book_update = _parse_dt(book.get("last_update"))
            if book_update and book_update > decision_time:
                filtered_future_rows += 1
                continue
            for market in book.get("markets") or []:
                if wanted and market.get("key") not in wanted:
                    continue
                market_update = _parse_dt(market.get("last_update") or book.get("last_update"))
                if market_update and market_update > decision_time:
                    filtered_future_rows += len(market.get("outcomes") or []) or 1
                    continue
                for outcome in market.get("outcomes") or []:
                    candidate["line_markets"].append({
                        "book": book.get("key"),
                        "sportsbook": book.get("title") or book.get("key"),
                        "market": market.get("key"),
                        "name": outcome.get("name"),
                        "selection": outcome.get("name"),
                        "team": outcome.get("name"),
                        "price": outcome.get("price"),
                        "point": outcome.get("point"),
                        "last_update": (
                            _iso(market_update) if market_update
                            else _iso(snapshot_dt) if snapshot_dt else None
                        ),
                        "source": "the_odds_api_historical",
                    })
        if candidate["line_markets"]:
            candidates.append(candidate)
    return {
        "source": "the_odds_api_historical",
        "odds_sport": odds_sport,
        "sport": local_sport,
        "requested_decision_time": _iso(decision_time),
        "snapshot_time": _iso(snapshot_dt) if snapshot_dt else None,
        "filtered_future_rows": filtered_future_rows,
        "candidates": candidates,
    }


def _market_identity(market: dict[str, Any], local_sport: str) -> MarketIdentity:
    return resolve_kalshi_market(
        {
            **market,
            "series_ticker": market.get("series_ticker") or str(market.get("ticker", "")).split("-", 1)[0],
            "sport": local_sport,
        },
        {"sport": local_sport},
    )


def discover_settled_markets(
    kalshi: Any,
    *,
    odds_sports: Iterable[str] = DEFAULT_SPORTS,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    sports = [sport for sport in odds_sports if sport in ODDS_SPORTS]
    if not sports:
        return []
    per_sport = max(math.ceil(sample_size / len(sports)), 1)
    rows: list[dict[str, Any]] = []
    for odds_sport in sports:
        meta = ODDS_SPORTS[odds_sport]
        series = meta["kalshi_series"]
        local_sport = meta["local_sport"]
        count = 0
        for market in kalshi.list_markets(series=series, status="settled", max_pages=max_pages):
            resolution = market_resolution(market)
            if resolution.get("winning_side") not in {"yes", "no"}:
                continue
            identity = _market_identity(market, local_sport)
            if identity.market_type != "moneyline":
                continue
            close_dt = _parse_dt(market.get("close_time"))
            if close_dt is None:
                continue
            rows.append({
                **market,
                "odds_sport": odds_sport,
                "sport": local_sport,
                "series_ticker": series,
                "market_identity": identity.as_dict(),
            })
            count += 1
            if count >= per_sport or len(rows) >= sample_size:
                break
    return rows[:sample_size]


def _group_markets(markets: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market in markets:
        identity = market.get("market_identity") or {}
        start_date = identity.get("start_date") or "unknown-date"
        grouped.setdefault((str(market["odds_sport"]), str(start_date)), []).append(market)
    return grouped


def _decision_time(markets: list[dict[str, Any]], minutes_before_close: int) -> datetime | None:
    closes = [_parse_dt(market.get("close_time")) for market in markets]
    closes = [dt for dt in closes if dt is not None]
    if not closes:
        return None
    return min(closes) - timedelta(minutes=minutes_before_close)


class _ReplayContext:
    def __init__(self, settings: Any, game_id: str):
        class ReplaySettings:
            pass

        replay_settings = ReplaySettings()
        if hasattr(settings, "__dict__"):
            replay_settings.__dict__.update(settings.__dict__)
        for name in (
            "min_edge",
            "stale_market_seconds",
            "max_spread_cents",
        ):
            if hasattr(settings, name):
                setattr(replay_settings, name, getattr(settings, name))
        replay_settings.game_id = game_id
        self.settings = replay_settings


def _brier(prob: float | None, outcome: int | None) -> float | None:
    if prob is None or outcome is None:
        return None
    value = calibration.brier([{"prior_p": prob, "outcome": outcome}])
    return round(value, 6) if math.isfinite(value) else None


def _mean(values: Iterable[float | int | None]) -> float | None:
    rows = [float(v) for v in values if v is not None]
    return round(sum(rows) / len(rows), 6) if rows else None


def summarize_backtest_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [
        row for row in records
        if row.get("model_prob") is not None and row.get("outcome") in {0, 1}
    ]
    positive = [
        row for row in scored
        if row.get("edge") is not None and float(row["edge"]) > 0
    ]
    model_brier = calibration.brier([
        {"prior_p": row["model_prob"], "outcome": row["outcome"]}
        for row in scored
    ])
    market_brier = calibration.brier([
        {"prior_p": row["kalshi_price_prob"], "outcome": row["outcome"]}
        for row in scored
        if row.get("kalshi_price_prob") is not None
    ])
    return {
        "label": "BACKTESTED_HISTORICAL_NOT_LIVE_VALIDATION",
        "record_count": len(records),
        "scored_count": len(scored),
        "positive_edge_count": len(positive),
        "positive_edge_hit_rate": _mean(row.get("outcome") for row in positive),
        "all_rows_yes_hit_rate": _mean(row.get("outcome") for row in scored),
        "entry_model_brier": (
            round(model_brier, 6) if math.isfinite(model_brier) else None
        ),
        "kalshi_price_baseline_brier": (
            round(market_brier, 6) if math.isfinite(market_brier) else None
        ),
        "mean_edge": _mean(row.get("edge") for row in scored),
        "mean_positive_edge": _mean(row.get("edge") for row in positive),
        "mean_realized_value_vs_kalshi_price": _mean(
            (
                float(row["outcome"]) - float(row["kalshi_price_prob"])
                if row.get("kalshi_price_prob") is not None else None
            )
            for row in positive
        ),
        "mean_model_minus_outcome_bias": _mean(
            float(row["model_prob"]) - float(row["outcome"])
            for row in scored
        ),
    }


def run_historical_backtest(
    *,
    kalshi: Any,
    settings: Any,
    odds_api_key: str | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    odds_sports: Iterable[str] = DEFAULT_SPORTS,
    max_odds_api_calls: int = DEFAULT_MAX_ODDS_API_CALLS,
    decision_minutes_before_close: int = DEFAULT_DECISION_MINUTES_BEFORE_CLOSE,
    regions: str = DEFAULT_ODDS_REGIONS,
    markets: str = DEFAULT_ODDS_MARKETS,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    cache_dir = cache_dir or (settings.data_dir / "historical_cache")
    odds_api_key = odds_api_key if odds_api_key is not None else os.environ.get("THE_ODDS_API_KEY", "")
    selected = discover_settled_markets(
        kalshi,
        odds_sports=odds_sports,
        sample_size=sample_size,
    )
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    fetches: list[HistoricalFetch] = []
    odds_network_calls = 0

    for (odds_sport, start_date), group in sorted(_group_markets(selected).items()):
        decision = _decision_time(group, decision_minutes_before_close)
        if decision is None:
            skipped.extend({"ticker": row.get("ticker"), "reason": "missing close_time"} for row in group)
            continue
        odds_path = odds_cache_path(
            cache_dir,
            sport=odds_sport,
            date=_iso(decision),
            regions=regions,
            markets=markets,
            odds_format="american",
        )
        if not odds_path.exists() and odds_network_calls >= max_odds_api_calls:
            skipped.extend({
                "ticker": row.get("ticker"),
                "reason": "max historical odds API call limit reached",
                "odds_sport": odds_sport,
                "start_date": start_date,
            } for row in group)
            continue
        try:
            odds_fetch = fetch_historical_odds(
                api_key=odds_api_key or "",
                sport=odds_sport,
                date=decision,
                cache_dir=cache_dir,
                regions=regions,
                markets=markets,
            )
        except Exception as exc:
            skipped.extend({
                "ticker": row.get("ticker"),
                "reason": f"historical odds fetch failed: {type(exc).__name__}: {exc}",
                "odds_sport": odds_sport,
                "start_date": start_date,
            } for row in group)
            continue
        if not odds_fetch.cached:
            odds_network_calls += 1
        fetches.append(odds_fetch)

        try:
            slate = historical_odds_to_slate(
                odds_fetch.body,
                odds_sport=odds_sport,
                decision_time=decision,
                markets=markets,
            )
        except LookAheadViolation as exc:
            skipped.extend({
                "ticker": row.get("ticker"),
                "reason": str(exc),
                "odds_sport": odds_sport,
                "start_date": start_date,
            } for row in group)
            continue

        match_rows = []
        market_payloads: dict[str, dict[str, Any]] = {}
        candle_meta: dict[str, dict[str, Any]] = {}
        for raw_market in group:
            ticker = raw_market.get("ticker")
            if not ticker:
                continue
            try:
                market_fetch = fetch_kalshi_market(kalshi, ticker, cache_dir)
                fetches.append(market_fetch)
                market = {
                    **(market_fetch.body or raw_market),
                    "series_ticker": raw_market.get("series_ticker"),
                    "sport": raw_market.get("sport"),
                }
                candle_fetch = fetch_kalshi_candlesticks(
                    kalshi,
                    ticker=ticker,
                    series_ticker=str(raw_market.get("series_ticker")),
                    decision_time=decision,
                    cache_dir=cache_dir,
                )
                fetches.append(candle_fetch)
            except Exception as exc:
                skipped.append({
                    "ticker": ticker,
                    "reason": f"kalshi historical fetch failed: {type(exc).__name__}: {exc}",
                })
                continue
            candles = (candle_fetch.body or {}).get("candlesticks") or []
            candle = select_candlestick_at_or_before(candles, decision)
            if candle is None:
                skipped.append({
                    "ticker": ticker,
                    "reason": "no Kalshi candlestick at or before decision time",
                    "decision_time": _iso(decision),
                })
                continue
            metrics, quote = executable_from_candlestick(candle, decision)
            if quote.get("implied") is None:
                skipped.append({
                    "ticker": ticker,
                    "reason": "Kalshi candlestick had no usable price",
                    "decision_time": _iso(decision),
                })
                continue
            market_payloads[ticker] = market
            candle_meta[ticker] = {
                "candle_end_period_ts": candle.get("end_period_ts"),
                "candle_end_time": (
                    _iso(datetime.fromtimestamp(int(candle["end_period_ts"]), timezone.utc))
                    if candle.get("end_period_ts") is not None else None
                ),
                "kalshi_quote": quote,
            }
            match_rows.append({
                "ticker": ticker,
                "event_ticker": market.get("event_ticker") or raw_market.get("event_ticker"),
                "series_ticker": raw_market.get("series_ticker"),
                "title": market.get("title") or raw_market.get("title"),
                "sport": raw_market.get("sport"),
                "yes_sub_title": market.get("yes_sub_title") or raw_market.get("yes_sub_title"),
                "no_sub_title": market.get("no_sub_title") or raw_market.get("no_sub_title"),
                "kalshi_quote": quote,
                "orderbook": metrics,
                "close_time": market.get("close_time") or raw_market.get("close_time"),
                "source": "historical-backtest",
            })
        if not match_rows:
            continue

        replay_ctx = _ReplayContext(settings, "HISTORICAL-BACKTEST")
        ranking = build_candidate_rankings(
            replay_ctx,
            slate,
            {"rows": match_rows},
            now=decision,
        )
        by_ticker = {row.get("ticker"): row for row in ranking.get("rows", [])}
        snapshot_dt = _parse_dt(slate.get("snapshot_time")) if slate.get("snapshot_time") else decision
        for raw_market in group:
            ticker = raw_market.get("ticker")
            ranked = by_ticker.get(ticker)
            if not ranked:
                continue
            market = market_payloads.get(ticker) or raw_market
            resolution = market_resolution(market)
            outcome = (
                int(resolution["winning_side"] == "yes")
                if resolution.get("winning_side") in {"yes", "no"} else None
            )
            executable = ranked.get("executable_price", {}) or {}
            price_cents = executable.get("vwap_cents")
            if price_cents is None:
                price_cents = executable.get("ask_cents")
            price_cents = int(round(float(price_cents))) if price_cents is not None else None
            price_prob = (float(price_cents) / 100.0) if price_cents is not None else None
            identity = ranked.get("market_identity") or raw_market.get("market_identity") or {}
            record = {
                "source": "backtest_edge_records",
                "label": "BACKTESTED_HISTORICAL_NOT_LIVE_VALIDATION",
                "ticker": ticker,
                "event_ticker": market.get("event_ticker") or raw_market.get("event_ticker"),
                "sport": raw_market.get("sport"),
                "odds_sport": raw_market.get("odds_sport"),
                "market_family": f"{identity.get('sport')} {identity.get('market_type')}",
                "decision_time": _iso(decision),
                "odds_snapshot_time": _iso(snapshot_dt) if snapshot_dt else None,
                "kalshi_candle_time": candle_meta.get(ticker, {}).get("candle_end_time"),
                "model_prob": ranked.get("model_prob"),
                "model_prob_sources": ranked.get("model_prob_sources") or [],
                "book_count": ranked.get("book_count"),
                "disagreement_std": ranked.get("disagreement_std"),
                "kalshi_price_prob": price_prob,
                "kalshi_price_cents": price_cents,
                "kalshi_quote": candle_meta.get(ticker, {}).get("kalshi_quote"),
                "edge": ranked.get("edge"),
                "required_edge": ranked.get("required_edge"),
                "passes_edge": ranked.get("passes_edge"),
                "blockers": ranked.get("blockers") or [],
                "match_type": ranked.get("match_type"),
                "identity_match": ranked.get("identity_match"),
                "market_identity": identity,
                "outcome": outcome,
                "winning_side": resolution.get("winning_side"),
                "market_status": resolution.get("market_status"),
                "entry_model_brier": _brier(ranked.get("model_prob"), outcome),
                "kalshi_price_brier": _brier(price_prob, outcome),
                "lookahead_guard": {
                    "odds_snapshot_lte_decision": bool(not snapshot_dt or snapshot_dt <= decision),
                    "kalshi_candle_lte_decision": bool(
                        candle_meta.get(ticker, {}).get("candle_end_period_ts") is not None
                        and int(candle_meta[ticker]["candle_end_period_ts"]) <= _unix(decision)
                    ),
                    "outcome_added_after_edge_scoring": True,
                },
            }
            records.append(record)
            if len(records) >= sample_size:
                break
        if len(records) >= sample_size:
            break

    cost_values = [
        fetch.credits_charged for fetch in fetches
        if fetch.provider == "the_odds_api" and not fetch.cached and fetch.credits_charged is not None
    ]
    odds_fetches = [fetch for fetch in fetches if fetch.provider == "the_odds_api"]
    payload = {
        "source": "historical-backtest",
        "label": "BACKTESTED_HISTORICAL_NOT_LIVE_VALIDATION",
        "generated_at": utc_now(),
        "sample_requested": sample_size,
        "selected_market_count": len(selected),
        "record_count": len(records),
        "skipped_count": len(skipped),
        "summary": summarize_backtest_records(records),
        "api_cost": {
            "the_odds_api_credits_charged_observed": sum(cost_values),
            "the_odds_api_uncached_calls": sum(1 for fetch in odds_fetches if not fetch.cached),
            "the_odds_api_cached_calls": sum(1 for fetch in odds_fetches if fetch.cached),
            "the_odds_api_credit_header_missing_count": sum(
                1 for fetch in odds_fetches
                if not fetch.cached and fetch.credits_charged is None
            ),
            "kalshi_credit_cost": 0,
        },
        "cache_dir": str(cache_dir),
        "fetches": [
            {
                "provider": fetch.provider,
                "cached": fetch.cached,
                "cache_path": fetch.cache_path,
                "headers": fetch.headers,
                "credits_charged": fetch.credits_charged,
            }
            for fetch in fetches
        ],
        "records": records,
        "skipped": skipped,
        "notes": [
            "Historical backtest rows are stored separately from settlement_records.",
            "The Odds API snapshot timestamp and each bookmaker/market last_update must be at or before decision_time.",
            "Kalshi price uses the latest candlestick ending at or before decision_time.",
            "Actual outcomes are appended only after model probability and edge scoring.",
            "Candlesticks do not expose depth; backtest uses a one-contract synthetic depth only to exercise the existing edge path.",
        ],
    }
    return payload
