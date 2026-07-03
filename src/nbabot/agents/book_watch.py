"""Phase: book-watch. Capture REST order book snapshots for mapped markets."""
from __future__ import annotations

import time
from typing import Any

from ..alerts import deliver
from ..orderbook import OrderBook
from ..research import ResearchStore
from .base import Context, load_context


def _artifact_tickers(ctx: Context) -> list[str]:
    tickers: set[str] = set()
    for row in (ctx.read_json("market_snapshot.json") or {}).get("rows", []):
        if row.get("ticker"):
            tickers.add(row["ticker"])
    for row in (ctx.read_json("market_catalog.json") or {}).get("rows", []):
        if row.get("ticker") and row.get("mapping_status") == "mapped":
            tickers.add(row["ticker"])
    return sorted(tickers)


def _ensure_tickers(ctx: Context) -> list[str]:
    tickers = _artifact_tickers(ctx)
    if tickers:
        return tickers

    # Snapshot-market is the canonical mapped-leg quote phase. Use it as a
    # fallback so book-watch can run on a clean workspace.
    from . import snapshot_market

    snapshot_market.run(ctx)
    return _artifact_tickers(ctx)


def _capture(ctx: Context, tickers: list[str]) -> tuple[list[OrderBook], dict[str, Any]]:
    books = [ctx.kalshi.orderbook(ticker, ctx.settings.orderbook_depth) for ticker in tickers]
    metrics = {
        book.ticker: {
            "yes": book.metrics("yes"),
            "no": book.metrics("no"),
        }
        for book in books
    }
    return books, metrics


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    tickers = _ensure_tickers(ctx)
    if not tickers:
        msg = "[book-watch] no mapped tickers; run discover-markets/snapshot-market first"
        deliver(msg, ctx.settings.deliver_to)
        return {"reason": "no-tickers", "snapshots": []}

    store = ResearchStore(ctx.settings.research_db_path)
    store.upsert_game(ctx.settings.game_id, ctx.game_tag)

    snapshots = []
    iterations = max(int(ctx.settings.book_watch_iterations), 1)
    interval = max(float(ctx.settings.book_watch_interval_seconds), 0.0)
    for i in range(iterations):
        books, metrics = _capture(ctx, tickers)
        stored = store.record_orderbook_snapshots(ctx.settings.game_id, books, metrics)
        snapshots.append({
            "iteration": i + 1,
            "stored": stored,
            "books": [book.as_dict() for book in books],
            "metrics": metrics,
        })
        if i + 1 < iterations and interval:
            time.sleep(interval)

    payload = {
        "game_id": ctx.settings.game_id,
        "game_tag": ctx.game_tag,
        "tickers": tickers,
        "snapshots": snapshots,
    }
    ctx.write_json("book_watch.json", payload)
    deliver(
        f"[book-watch] {ctx.settings.game_id}: {len(tickers)} tickers, "
        f"{len(snapshots)} iterations, stored={sum(s['stored'] for s in snapshots)}",
        ctx.settings.deliver_to,
    )
    return payload
