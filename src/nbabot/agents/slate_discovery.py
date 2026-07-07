"""Phase: slate-discovery. Discover candidate sports events and game lines."""
from __future__ import annotations

import os

from .. import guardrails
from ..alerts import deliver
from ..slate import discover_slate
from .base import Context, load_context


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _sport_market_candidates_limit() -> int:
    default = max(_int_env("NBABOT_KALSHI_SLATE_LIMIT", 200), 1)
    requested = max(_int_env("NBABOT_SPORT_MARKET_CANDIDATES_LIMIT", default), 1)
    ceiling = max(_int_env("NBABOT_SPORT_MARKET_CANDIDATES_MAX", 500), 1)
    return min(requested, ceiling)


def _format(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    top = candidates[0] if candidates else {}
    label = "none"
    if top:
        label = f"{top.get('away_team')} @ {top.get('home_team')} score={top.get('discovery_score')}"
    return (
        f"[slate-discovery] {payload['game_id']}: candidates={payload.get('candidate_count', 0)} "
        f"sports={','.join(payload.get('sports') or [])} top={label}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    payload = discover_slate(ctx)
    ctx.write_json("slate_candidates.json", payload)
    limit = _sport_market_candidates_limit()
    rows = []
    captured_at = payload.get("generated_at")
    for candidate in payload.get("candidates", []):
        for market in candidate.get("kalshi_markets", []):
            if not market.get("ticker"):
                continue
            rows.append({
                "ticker": market.get("ticker"),
                "event_ticker": market.get("event_ticker"),
                "series_ticker": market.get("series_ticker"),
                "candidate_id": candidate.get("candidate_id"),
                "sport": candidate.get("sport"),
                "title": market.get("title"),
                "is_composite": market.get("is_composite"),
                "bid": market.get("bid"),
                "ask": market.get("ask"),
                "implied": market.get("implied"),
                "spread_cents": market.get("spread_cents"),
                "source": "slate-discovery",
                "captured_at": captured_at,
                "trade_eligible": False,
                "reason": "open Kalshi sports slate candidate; research/risk gates still required",
            })
    ctx.write_json("sport_market_candidates.json", {
        "game_id": payload["game_id"],
        "source": "slate-discovery",
        "generated_at": captured_at,
        "rows": rows[:max(limit, 1)],
    })
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
