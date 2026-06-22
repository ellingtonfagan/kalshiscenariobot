"""Phase: baseline (T-4h).

Pull every leg's Kalshi price, set entry_implied_p, and flag legs where the market
disagrees with the skill prior by more than 10 points (edge_flag).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import guardrails
from ..alerts import deliver
from ..scenarios import price_leg
from .base import Context, load_context

EDGE_THRESHOLD = 0.10


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    props = ctx.kalshi.prop_prices(ctx.game_tag)
    winners = ctx.kalshi.winner_prices(ctx.game_tag)

    board: dict[str, dict] = {}
    lines_out: list[str] = [f"[baseline] {ctx.settings.game_id}  game_tag={ctx.game_tag}"]

    for sc in ctx.scenarios:
        leg_records = []
        flags = []
        for leg in sc.legs:
            implied = price_leg(leg, props, winners)
            edge = None
            if implied is not None:
                edge = round(leg.prior_p - implied, 3)
                if abs(edge) >= EDGE_THRESHOLD:
                    flags.append(f"{leg.label()} prior {leg.prior_p:.2f} vs mkt {implied:.2f} (edge {edge:+.2f})")
            leg_records.append({
                "market": leg.market, "label": leg.label(), "op": leg.op,
                "line": leg.line, "prior_p": leg.prior_p,
                "entry_implied_p": implied, "edge": edge,
            })
        board[sc.id] = {"name": sc.name, "risk": sc.risk, "legs": leg_records}
        hope = "  [HOPE BET]" if guardrails.is_hope_bet(sc.risk) else ""
        lines_out.append(f"  {sc.id} {sc.name} (risk {sc.risk}){hope}")
        for fl in flags:
            lines_out.append(f"      edge: {fl}")

    snapshot = {
        "game_id": ctx.settings.game_id,
        "phase": "baseline",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "board": board,
    }
    ctx.write_json("board.json", snapshot)
    out = guardrails.with_footer("\n".join(lines_out))
    deliver(out, ctx.settings.deliver_to)
    return snapshot
