"""Market snapshot helpers for scenario legs."""
from __future__ import annotations

from .kalshi import Quote
from .research import utc_now
from .scenarios import Leg, Scenario


def quote_for_leg(leg: Leg, props: dict, winners: dict) -> Quote | None:
    if leg.special == "win" and leg.team:
        return winners.get(leg.team)
    if leg.special in ("cover", "total") or not leg.resolvable:
        return None
    if leg.special == "pra":
        return props.get((leg.player, "points", int(leg.line)))
    if leg.player and leg.stat:
        return props.get((leg.player, leg.stat, int(leg.line)))
    return None


def directional_prices(leg: Leg, quote: Quote) -> tuple[str, int, int, int, float]:
    """Return side, bid, ask, entry price, implied for the leg's direction."""
    if leg.op in (">=", ">", "=="):
        return "yes", quote.bid, quote.ask, quote.ask or quote.mid, quote.implied
    no_bid = 100 - quote.ask if quote.ask else 0
    no_ask = 100 - quote.bid if quote.bid else 0
    no_mid = round((no_bid + no_ask) / 2) if no_bid and no_ask else no_bid or no_ask
    return "no", no_bid, no_ask, no_ask or no_mid, min(max(no_mid / 100.0, 0.0), 1.0)


def scenario_joint_prior(scenario: Scenario) -> float:
    joint = 1.0
    for leg in scenario.legs:
        if leg.resolvable:
            joint *= leg.prior_p
    return joint


def snapshot_rows(game_id: str, scenarios: list[Scenario], props: dict, winners: dict,
                  haircut: dict[str, float]) -> list[dict]:
    captured_at = utc_now()
    rows: list[dict] = []
    for scenario in scenarios:
        joint = scenario_joint_prior(scenario) * float(haircut.get(scenario.id, 1.0))
        for leg in scenario.legs:
            quote = quote_for_leg(leg, props, winners)
            if not quote:
                rows.append({
                    "game_id": game_id,
                    "captured_at": captured_at,
                    "scenario_id": scenario.id,
                    "market": leg.market,
                    "label": leg.label(),
                    "ticker": None,
                    "bid": None,
                    "ask": None,
                    "mid": None,
                    "implied": None,
                    "prior_p": leg.prior_p,
                    "line": leg.line,
                    "risk": scenario.risk,
                    "side": None,
                    "entry_price_cents": None,
                    "sgp_adjusted_prob": round(joint, 5),
                    "source": "kalshi",
                })
                continue
            side, bid, ask, entry, implied = directional_prices(leg, quote)
            rows.append({
                "game_id": game_id,
                "captured_at": captured_at,
                "scenario_id": scenario.id,
                "market": leg.market,
                "label": leg.label(),
                "ticker": quote.ticker,
                "bid": bid,
                "ask": ask,
                "mid": round((bid + ask) / 2) if bid and ask else bid or ask,
                "implied": implied,
                "prior_p": leg.prior_p,
                "line": leg.line,
                "risk": scenario.risk,
                "side": side,
                "entry_price_cents": entry,
                "sgp_adjusted_prob": round(joint, 5),
                "source": "kalshi",
            })
    return rows
