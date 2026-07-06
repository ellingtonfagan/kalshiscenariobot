"""Phase: portfolio-sync. Mirror Kalshi balance and positions into local artifacts."""
from __future__ import annotations

from ..alerts import deliver
from ..research import ResearchStore, utc_now
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    store.upsert_game(ctx.settings.game_id, ctx.game_tag)

    try:
        balance_cents = ctx.kalshi.balance_cents()
        positions = ctx.kalshi.positions()
    except Exception as e:
        payload = {
            "game_id": ctx.settings.game_id,
            "synced_at": utc_now(),
            "ok": False,
            "error": str(e),
        }
        ctx.write_json("portfolio_sync.json", payload)
        deliver(f"[portfolio-sync] failed: {e}", ctx.settings.deliver_to)
        return payload

    payload = {
        "game_id": ctx.settings.game_id,
        "synced_at": utc_now(),
        "ok": True,
        "balance_cents": balance_cents,
        "balance_usd": round(balance_cents / 100, 2),
        "positions": positions,
        "open_positions": len(positions),
    }
    exposure_units = 0.0
    try:
        exposure_units = store.game_order_exposure_units("live_orders", ctx.settings.game_id)
    except Exception:
        exposure_units = 0.0
    store.record_risk_snapshot(ctx.settings.game_id, {
        "game_exposure_units": exposure_units,
        "daily_pnl_units": 0.0,
        "open_positions": len(positions),
        "circuit_breaker_on": False,
        "balance_cents": balance_cents,
    })
    ctx.write_json("portfolio_sync.json", payload)
    deliver(
        f"[portfolio-sync] balance=${payload['balance_usd']:.2f} "
        f"positions={len(positions)}",
        ctx.settings.deliver_to,
    )
    return payload
