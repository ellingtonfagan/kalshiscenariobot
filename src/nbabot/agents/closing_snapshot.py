"""Phase: closing-snapshot. Persist close-time consensus for open exposure."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .. import guardrails
from ..alerts import deliver
from ..research import ResearchStore, utc_now
from ..settlement_audit import (
    _parse_dt,
    _price_cents,
    _ranker_paths,
    _ranker_rows,
    _row_consensus_prob,
    market_resolution,
    normalized_order,
    normalized_shadow_intent,
)
from .base import Context, load_context


def _latest_candidate_rows(ctx: Context) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in _ranker_paths(ctx.settings.data_dir):
        for row in _ranker_rows(path):
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            row["_artifact"] = row.get("_artifact") or path.name
            rows[ticker] = row
    return rows


def _side_prices(market: dict[str, Any], side: str) -> tuple[float | None, float | None, float | None]:
    yes_bid = _price_cents(
        market.get("yes_bid")
        or market.get("yes_bid_cents")
        or market.get("bid")
        or market.get("bid_cents")
    )
    yes_ask = _price_cents(
        market.get("yes_ask")
        or market.get("yes_ask_cents")
        or market.get("ask")
        or market.get("ask_cents")
    )
    no_bid = _price_cents(market.get("no_bid") or market.get("no_bid_cents"))
    no_ask = _price_cents(market.get("no_ask") or market.get("no_ask_cents"))
    if no_bid is None and yes_ask is not None:
        no_bid = round(100.0 - yes_ask, 4)
    if no_ask is None and yes_bid is not None:
        no_ask = round(100.0 - yes_bid, 4)

    bid, ask = (yes_bid, yes_ask) if side == "yes" else (no_bid, no_ask)
    mid = round((bid + ask) / 2.0, 4) if bid is not None and ask is not None else None
    return bid, ask, mid


def _seconds_to_close(market: dict[str, Any], now: datetime) -> tuple[str | None, float | None]:
    close_dt = _parse_dt(market.get("close_time") or market.get("expected_close_time"))
    if close_dt is None:
        return None, None
    return close_dt.isoformat(), (close_dt - now).total_seconds()


def _approaching_close(market: dict[str, Any], now: datetime, window_minutes: int) -> tuple[bool, str | None, float | None, str]:
    close_time, seconds = _seconds_to_close(market, now)
    if seconds is None:
        return True, close_time, seconds, "no-close-time-open-exposure"
    if seconds <= float(window_minutes) * 60.0:
        return True, close_time, seconds, "within-close-window"
    return False, close_time, seconds, "outside-close-window"


def _snapshot_for_order(
    ctx: Context,
    order: dict[str, Any],
    market: dict[str, Any],
    candidate_rows: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    row = candidate_rows.get(order["ticker"])
    if not row:
        return None
    prob = _row_consensus_prob(row, order["side"])
    if prob is None:
        return None
    bid, _ask, mid = _side_prices(market, order["side"])
    close_time, seconds = _seconds_to_close(market, now)
    consensus = row.get("consensus") if isinstance(row.get("consensus"), dict) else {}
    return {
        "ticker": order["ticker"],
        "game_id": order["game_id"],
        "side": order["side"],
        "captured_at": now.isoformat(),
        "market_close_time": close_time,
        "seconds_to_close": seconds,
        "source": "candidate-ranker-closing-snapshot",
        "artifact": row.get("_artifact"),
        "consensus_prob": prob,
        "consensus_cents": round(prob * 100.0, 4),
        "kalshi_mid_cents": mid,
        "executable_cents": bid,
        "book_count": consensus.get("book_count") or row.get("book_count"),
        "sources": consensus.get("sources") or row.get("model_prob_sources") or [],
        "market": market,
        "candidate": row,
    }


def _format(payload: dict[str, Any]) -> str:
    return (
        f"[closing-snapshot] checked={payload.get('checked_count', 0)} "
        f"recorded={payload.get('recorded_count', 0)} "
        f"skipped={payload.get('skipped_count', 0)} "
        f"errors={payload.get('error_count', 0)}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    now = datetime.now(timezone.utc)
    candidate_rows = _latest_candidate_rows(ctx)
    raw_orders = store.list_execution_orders(unsettled_only=True)
    raw_shadows = store.list_shadow_intents(unsettled_only=True)
    exposures = []
    for raw in raw_orders:
        order = normalized_order(raw)
        if order is not None:
            exposures.append(order)
    for raw in raw_shadows:
        shadow = normalized_shadow_intent(raw)
        if shadow is not None:
            exposures.append(shadow)

    recorded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    window = int(getattr(ctx.settings, "closing_snapshot_window_minutes", 240))
    seen: set[str] = set()
    for order in exposures:
        ticker = order["ticker"]
        if ticker in seen:
            continue
        seen.add(ticker)
        try:
            market = ctx.kalshi.market(ticker)
        except Exception as exc:
            errors.append({"ticker": ticker, "error": str(exc)})
            continue
        if market_resolution(market).get("settled"):
            skipped.append({"ticker": ticker, "reason": "market already settled"})
            continue
        eligible, close_time, seconds, reason = _approaching_close(market, now, window)
        if not eligible:
            skipped.append({
                "ticker": ticker,
                "reason": reason,
                "market_close_time": close_time,
                "seconds_to_close": seconds,
            })
            continue
        snapshot = _snapshot_for_order(ctx, order, market, candidate_rows, now=now)
        if snapshot is None:
            skipped.append({"ticker": ticker, "reason": "no candidate-ranker consensus row"})
            continue
        store.record_closing_snapshot(snapshot)
        recorded.append({
            "ticker": ticker,
            "side": snapshot["side"],
            "consensus_cents": snapshot["consensus_cents"],
            "kalshi_mid_cents": snapshot.get("kalshi_mid_cents"),
            "executable_cents": snapshot.get("executable_cents"),
            "market_close_time": snapshot.get("market_close_time"),
            "seconds_to_close": snapshot.get("seconds_to_close"),
        })

    payload = {
        "game_id": ctx.settings.game_id,
        "generated_at": utc_now(),
        "checked_count": len(seen),
        "recorded_count": len(recorded),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "window_minutes": window,
        "candidate_row_count": len(candidate_rows),
        "records": recorded,
        "skipped": skipped,
        "errors": errors,
        "notes": [
            "Read-only snapshot pass; no orders are placed or modified.",
            "Consensus comes from the latest candidate-ranker artifact and is stored before settlement.",
        ],
    }
    ctx.write_json("closing_snapshot.json", payload)
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
