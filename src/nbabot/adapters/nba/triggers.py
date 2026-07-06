"""§5 live triggers: game signals → scenario activation/downgrade.

Each trigger is a pure predicate over the GameState (plus a little derived context).
evaluate() returns a list of TriggerHit; the heartbeat applies them as state overrides
and as alert lines. Triggers never mutate global state themselves.
"""
from __future__ import annotations

from dataclasses import dataclass

from .scores import GameState

# scenario ids referenced by the template triggers
S4, S6, S2, S5, S1, S3 = "S4", "S6", "S2", "S5", "S1", "S3"


@dataclass
class TriggerHit:
    scenario_id: str
    override_state: str | None   # force this scenario state, or None to just alert
    message: str


def _p(gs: GameState, name: str):
    return gs.player(name)


def evaluate(gs: GameState, halftime_total: int | None = None) -> list[TriggerHit]:
    hits: list[TriggerHit] = []
    towns = _p(gs, "Towns")
    wemby = _p(gs, "Wembanyama")

    # Towns foul math — the hinge of this game.
    if towns:
        if towns.fouls >= 2 and gs.period == 1:
            hits.append(TriggerHit(S6, "ON_TRACK",
                f"KAT {towns.fouls}F in Q1 → S6 activates"))
            hits.append(TriggerHit(S4, "AT_RISK",
                "KAT early foul trouble → S4 AT_RISK"))
        if towns.fouls >= 3 and gs.period <= 2:
            hits.append(TriggerHit(S4, "AT_RISK", "KAT 3F before half → S4 AT_RISK-high"))
            hits.append(TriggerHit(S6, "ON_TRACK", "KAT 3F before half → S6 LIVE"))
        if towns.fouls >= 6:
            hits.append(TriggerHit(S4, "DEAD", "KAT fouled out → S4 DEAD"))

    # Wemby passivity (G2 pattern) — first-half shot volume proxy via points.
    if wemby and gs.period == 2 and wemby.pts <= 6:
        hits.append(TriggerHit(S6, "AT_RISK", "Wemby passive first half → downgrade S6"))
        hits.append(TriggerHit(S2, "ON_TRACK", "Wemby passive → S2 (OG clamp) ON_TRACK"))

    # Pace / shootout boost.
    if halftime_total is not None and halftime_total > 118:
        hits.append(TriggerHit(S5, "ON_TRACK", f"Halftime total {halftime_total} → boost S5 (Over)"))
        hits.append(TriggerHit(S1, "ON_TRACK", f"Hot pace → boost S1 (transition)"))

    return hits
