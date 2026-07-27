"""Phase: portfolio-sync. Mirror Kalshi balance and positions into local artifacts."""
from __future__ import annotations

from ..alerts import deliver
from ..exposure import reconcile_open_exposure, write_reconciliation
from ..research import ResearchStore, utc_now
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    store.upsert_game(ctx.settings.game_id, ctx.game_tag)

    mode = str(getattr(ctx.settings, "execution_mode", "paper") or "paper").lower()
    try:
        balance_usd_exact = None
        if mode == "demo":
            balance_payload = ctx.kalshi.demo_get_to_base(ctx.settings.demo_api_base, "/portfolio/balance")
            balance_cents = int(float(balance_payload.get("balance", 0)))
            if balance_payload.get("balance_dollars") is not None:
                balance_usd_exact = float(balance_payload["balance_dollars"])
            positions = ctx.kalshi.demo_positions(ctx.settings.demo_api_base)
            balance_source = "kalshi-demo"
        else:
            balance_cents = ctx.kalshi.balance_cents()
            positions = ctx.kalshi.positions()
            balance_source = "kalshi-live"
            balance_usd_exact = balance_cents / 100.0
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
        "mode": mode,
        "balance_source": balance_source,
        "balance_cents": balance_cents,
        "balance_usd": round(float(balance_usd_exact), 4),
        "balance_usd_exact": float(balance_usd_exact),
        "positions": positions,
        "open_positions": len(positions),
    }
    table = "demo_orders" if mode == "demo" else "live_orders"
    exposure_units = 0.0
    portfolio_exposure_units = 0.0
    exposure_reconciliation = {}
    try:
        exposure_reconciliation = reconcile_open_exposure(ctx, store, table, game_id=ctx.settings.game_id)
        write_reconciliation(ctx, exposure_reconciliation)
        exposure_units = float(exposure_reconciliation["authoritative_game_exposure_units"])
        portfolio_exposure_units = float(exposure_reconciliation["authoritative_portfolio_exposure_units"])
    except Exception:
        exposure_units = 0.0
        portfolio_exposure_units = 0.0
    payload["exposure_reconciliation"] = exposure_reconciliation
    store.record_risk_snapshot(ctx.settings.game_id, {
        "game_exposure_units": exposure_units,
        "portfolio_exposure_units": portfolio_exposure_units,
        "daily_pnl_units": 0.0,
        "open_positions": len(positions),
        "circuit_breaker_on": False,
        "balance_cents": balance_cents,
        "exposure_reconciliation": exposure_reconciliation,
    })
    ctx.write_json("portfolio_sync.json", payload)
    deliver(
        f"[portfolio-sync] balance=${payload['balance_usd']:.2f} "
        f"positions={len(positions)}",
        ctx.settings.deliver_to,
    )
    return payload
