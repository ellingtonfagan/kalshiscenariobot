"""Phase: historical-backtest. Replay settled markets without feeding validation."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .. import guardrails
from ..alerts import deliver
from ..backtest_replay import (
    DEFAULT_DECISION_MINUTES_BEFORE_CLOSE,
    DEFAULT_MAX_ODDS_API_CALLS,
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_SPORTS,
    run_historical_backtest,
)
from ..research import ResearchStore
from .base import Context, load_context


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


def _run_id() -> str:
    return "historical-edge-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _format(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    cost = payload.get("api_cost") or {}
    return (
        "[historical-backtest] "
        f"records={payload.get('record_count', 0)} "
        f"positive_edge={summary.get('positive_edge_count', 0)} "
        f"model_brier={summary.get('entry_model_brier')} "
        f"kalshi_brier={summary.get('kalshi_price_baseline_brier')} "
        f"odds_api_credits={cost.get('the_odds_api_credits_charged_observed', 0)} "
        f"cached_calls={cost.get('the_odds_api_cached_calls', 0)}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    payload = run_historical_backtest(
        kalshi=ctx.kalshi,
        settings=ctx.settings,
        sample_size=_int_env("NBABOT_HISTORICAL_BACKTEST_SAMPLE_SIZE", DEFAULT_SAMPLE_SIZE),
        odds_sports=_csv_env("NBABOT_HISTORICAL_BACKTEST_SPORTS", DEFAULT_SPORTS),
        max_odds_api_calls=_int_env(
            "NBABOT_HISTORICAL_BACKTEST_MAX_ODDS_API_CALLS",
            DEFAULT_MAX_ODDS_API_CALLS,
        ),
        decision_minutes_before_close=_int_env(
            "NBABOT_HISTORICAL_BACKTEST_DECISION_MINUTES_BEFORE_CLOSE",
            DEFAULT_DECISION_MINUTES_BEFORE_CLOSE,
        ),
        regions=os.environ.get("NBABOT_HISTORICAL_BACKTEST_REGIONS", "us"),
        markets=os.environ.get("NBABOT_HISTORICAL_BACKTEST_MARKETS", "h2h"),
    )
    payload["run_id"] = _run_id()
    payload["game_id"] = ctx.settings.game_id
    store.record_backtest_edge_run(payload["run_id"], ctx.settings.game_id, payload)
    ctx.write_json("historical_backtest.json", payload)
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
