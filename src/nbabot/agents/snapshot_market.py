"""Phase: snapshot-market. Capture mapped Kalshi quotes for research/execution."""
from __future__ import annotations

from ..alerts import deliver
from ..marketdata import snapshot_rows
from ..research import ResearchStore
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    props = ctx.kalshi.prop_prices(ctx.game_tag)
    winners = ctx.kalshi.winner_prices(ctx.game_tag)
    rows = snapshot_rows(ctx.settings.game_id, ctx.scenarios, props, winners, ctx.haircut)

    store = ResearchStore(ctx.settings.research_db_path)
    store.upsert_game(ctx.settings.game_id, ctx.game_tag)
    stored = store.insert_market_snapshots(rows)
    payload = {"game_id": ctx.settings.game_id, "rows": rows, "stored": stored}
    ctx.write_json("market_snapshot.json", payload)
    priced = sum(1 for r in rows if r.get("ticker"))
    deliver(f"[snapshot-market] {ctx.settings.game_id}: {priced}/{len(rows)} legs priced; stored={stored}",
            ctx.settings.deliver_to)
    return payload
