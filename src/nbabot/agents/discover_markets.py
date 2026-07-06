"""Phase: discover-markets. Catalog open Kalshi NBA markets for this game."""
from __future__ import annotations

from ..alerts import deliver
from ..research import ResearchStore
from .base import Context, adapter_for, load_context


def _series_from_config(ctx: Context) -> list[str]:
    return adapter_for(ctx).discovery_series(ctx.settings)


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    adapter = adapter_for(ctx)
    series = _series_from_config(ctx)
    raw = adapter.fetch_raw_markets(ctx.kalshi, ctx.game_tag, series)
    rows = adapter.build_catalog(ctx.settings.game_id, ctx.game_tag, raw, ctx.scenarios)

    store = ResearchStore(ctx.settings.research_db_path)
    store.upsert_game(ctx.settings.game_id, ctx.game_tag)
    stored = store.record_market_catalog(rows)
    payload = {
        "game_id": ctx.settings.game_id,
        "game_tag": ctx.game_tag,
        "series": series,
        "rows": rows,
        "stored": stored,
    }
    ctx.write_json("market_catalog.json", payload)

    mapped = sum(1 for row in rows if row.get("mapping_status") == "mapped")
    deliver(
        f"[discover-markets] {ctx.settings.game_id}: {mapped}/{len(rows)} markets mapped; stored={stored}",
        ctx.settings.deliver_to,
    )
    return payload
