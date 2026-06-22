"""Scenario model + the live state engine.

A scenario is one game-script with N correlated legs. This module:
  - loads scenarios + market_map + sgp_haircut from the YAML doc
  - prices each leg from Kalshi quotes  (entry/live implied prob)
  - reads live box score to mark each leg on/off-track and detect DEAD legs
  - rolls legs up into a scenario state + an SGP-adjusted live payout multiple
  - resolves legs to 1/0 at the final buzzer (for the learning log)

Pure module: it takes already-fetched prices + game state, returns dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kalshi import Quote
from .scores import GameState

STATES = ("ON_TRACK", "DRIFTING", "AT_RISK", "DEAD", "VOID")
REGULATION_MINUTES = 48
TEAM_ALIASES = {
    "BKN": "BK",
    "GSW": "GS",
    "NOP": "NO",
    "NYK": "NY",
    "SAS": "SA",
}


# ── leg / scenario models ──────────────────────────────────────────────────────
@dataclass
class Leg:
    market: str            # key into market_map
    op: str                # ">=", "<", "==", ">"
    line: float
    prior_p: float
    # resolution metadata, filled from market_map:
    player: str | None = None
    stat: str | None = None
    special: str | None = None    # win|cover|total|pra
    team: str | None = None
    resolvable: bool = True

    def label(self) -> str:
        if self.special in ("win",):
            return f"{self.team} win"
        if self.special == "cover":
            return f"{self.team} cover {self.line}"
        if self.special == "total":
            return f"total {self.op}{self.line}"
        if self.special == "pra":
            return f"{self.player} PRA {self.op}{self.line:g}"
        return f"{self.player} {self.stat} {self.op}{self.line:g}"


@dataclass
class Scenario:
    id: str
    name: str
    side: str
    trigger: str
    est_payout_x: float
    risk: int
    counter: str
    legs: list[Leg]


@dataclass
class LegLive:
    leg: Leg
    implied: float | None     # market implied prob (None if unpriced)
    actual: int | None        # current box-score stat value (None if N/A)
    on_track: bool
    dead: bool
    note: str = ""


@dataclass
class ScenarioState:
    id: str
    state: str
    legs_live: list[LegLive]
    live_payout_x: float | None
    hit_legs: int
    total_legs: int
    note: str = ""


# ── loading ─────────────────────────────────────────────────────────────────────
def load_scenarios(doc: dict[str, Any]) -> tuple[list[Scenario], dict, dict]:
    market_map = doc.get("market_map", {})
    haircut = doc.get("sgp_haircut", {})
    scenarios: list[Scenario] = []
    for s in doc.get("scenarios", []):
        legs = []
        for lg in s["legs"]:
            mm = market_map.get(lg["market"], {})
            legs.append(Leg(
                market=lg["market"], op=lg["op"], line=float(lg["line"]),
                prior_p=float(lg["p"]),
                player=mm.get("player"), stat=mm.get("stat"),
                special=mm.get("special"), team=mm.get("team"),
                resolvable=mm.get("resolvable", True),
            ))
        scenarios.append(Scenario(
            id=s["id"], name=s["name"], side=s["side"], trigger=s["trigger"],
            est_payout_x=float(s["est_payout_x"]), risk=int(s["risk"]),
            counter=s["counter"], legs=legs,
        ))
    return scenarios, market_map, haircut


# ── pricing ──────────────────────────────────────────────────────────────────────
def price_leg(leg: Leg, props: dict[tuple[str, str, int], Quote],
              winners: dict[str, Quote]) -> float | None:
    """Best available market implied prob for a leg, or None if unpriced."""
    if leg.special == "win" and leg.team:
        q = winners.get(leg.team)
        return q.implied if q else None
    if leg.special in ("cover", "total") or not leg.resolvable:
        return None  # series not wired (see AGENTS.md §4)
    if leg.special == "pra":
        # approximate PRA over via the player's points line at the same number
        q = props.get((leg.player, "points", int(leg.line)))
        return q.implied if q else None
    if leg.player and leg.stat:
        q = props.get((leg.player, leg.stat, int(leg.line)))
        if q:
            p = q.implied
            return p if leg.op in (">=", ">") else (1.0 - p)
    return None


# ── live evaluation ──────────────────────────────────────────────────────────────
def _actual_value(leg: Leg, gs: GameState) -> int | None:
    if leg.special == "pra":
        pl = gs.player(leg.player) if leg.player else None
        return (pl.pts + pl.reb) if pl else None
    if leg.special == "win":
        if gs.is_final and leg.team:
            team = _team_key(leg.team)
            if team == _team_key(gs.home_abbr):
                return int(gs.home_score > gs.away_score)
            if team == _team_key(gs.away_abbr):
                return int(gs.away_score > gs.home_score)
        return None
    if leg.special == "total":
        return gs.total if gs.is_live or gs.is_final else None
    if leg.player and leg.stat:
        pl = gs.player(leg.player)
        return pl.stat(leg.stat) if pl else None
    return None


def _op_ok(value: float, op: str, line: float) -> bool:
    return {">=": value >= line, ">": value > line,
            "<": value < line, "==": value == line}.get(op, False)


def _team_key(abbr: str | None) -> str | None:
    if not abbr:
        return None
    abbr = abbr.upper()
    return TEAM_ALIASES.get(abbr, abbr)


def _minutes_left(gs: GameState) -> float:
    """Rough regulation minutes remaining for pace projection."""
    if not gs.is_live or gs.period == 0:
        return float(REGULATION_MINUTES)
    elapsed = (gs.period - 1) * 12
    # parse mm:ss clock
    try:
        m, s = gs.clock.split(":")
        elapsed += 12 - (int(m) + int(s) / 60)
    except Exception:
        elapsed += 6
    return max(REGULATION_MINUTES - elapsed, 0.0)


def evaluate(scenario: Scenario, props: dict, winners: dict, gs: GameState,
             haircut: float = 1.0) -> ScenarioState:
    legs_live: list[LegLive] = []
    frac_game = 1.0 - (_minutes_left(gs) / REGULATION_MINUTES)  # 0 pre, 1 final
    for leg in scenario.legs:
        implied = price_leg(leg, props, winners)
        actual = _actual_value(leg, gs)
        dead = False
        on_track = True
        note = ""

        if actual is not None and leg.op in (">=", ">"):
            if _op_ok(actual, leg.op, leg.line):
                on_track = True  # already cleared
            elif frac_game > 0.05:
                pace = actual / max(frac_game, 1e-6)
                on_track = pace >= leg.line
                if not on_track and frac_game > 0.85 and actual < leg.line:
                    dead = True
                    note = f"{actual}<{leg.line:g} with game ~over"
        elif actual is not None and leg.op == "<":
            if actual >= leg.line:
                dead = True
                note = f"{actual}>= {leg.line:g} (under busted)"

        # foul-out kills counting-stat overs that aren't met
        if leg.player and leg.stat in ("points", "rebounds", "assists", "threes"):
            pl = gs.player(leg.player)
            if pl and pl.fouls >= 6 and actual is not None and not _op_ok(actual, leg.op, leg.line):
                dead, note = True, f"{leg.player} fouled out at {actual}"

        legs_live.append(LegLive(leg, implied, actual, on_track and not dead, dead, note))

    # roll up
    if any(l.dead for l in legs_live):
        state = "DEAD"
    elif sum(1 for l in legs_live if not l.on_track) >= 2:
        state = "AT_RISK"
    elif any(not l.on_track for l in legs_live):
        state = "DRIFTING"
    else:
        state = "ON_TRACK"

    priced = [l.implied for l in legs_live if l.implied is not None]
    if priced:
        joint = 1.0
        for p in priced:
            joint *= max(p, 1e-6)
        joint *= max(haircut, 1e-6)
        live_x = round(1.0 / joint, 1)
    else:
        live_x = None

    hit = sum(1 for l in legs_live
              if l.actual is not None and _op_ok(l.actual, l.leg.op, l.leg.line))
    return ScenarioState(scenario.id, state, legs_live, live_x, hit, len(legs_live))


# ── final resolution (for the learning log) ──────────────────────────────────────
def resolve_leg(leg: Leg, gs: GameState) -> int | None:
    if not leg.resolvable:
        return None
    actual = _actual_value(leg, gs)
    if actual is None:
        return None
    return int(_op_ok(actual, leg.op, leg.line))
