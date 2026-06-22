"""Phase: lock (T-30m). Re-pull prices and freeze the live board.

The locked board is the reference the heartbeat and reconcile compare against:
entry price + skill prior per leg, captured just before tip.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..alerts import deliver
from ..scenarios import price_leg
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    props = ctx.kalshi.prop_prices(ctx.game_tag)
    winners = ctx.kalshi.winner_prices(ctx.game_tag)

    locked: dict[str, list[dict]] = {}
    for sc in ctx.scenarios:
        rows = []
        for leg in sc.legs:
            implied = price_leg(leg, props, winners)
            rows.append({"market": leg.market, "label": leg.label(),
                         "prior_p": leg.prior_p, "locked_implied_p": implied})
        locked[sc.id] = rows

    voids = (ctx.read_json("lineups.json") or {}).get("void_scenarios", {})
    payload = {
        "game_id": ctx.settings.game_id,
        "phase": "lock",
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "void_scenarios": list(voids.keys()),
        "legs": locked,
    }
    ctx.write_json("locked_board.json", payload)
    n = sum(len(v) for v in locked.values())
    deliver(f"[lock] {ctx.settings.game_id}: froze {len(locked)} scenarios / {n} legs"
            + (f"; void={list(voids)}" if voids else ""), ctx.settings.deliver_to)
    return payload
