"""Lineups / inactives interface.

STUB — wire a real, licensed source (official injury feed or a sports API you are
allowed to use). Do NOT scrape paywalled or ToS-restricted feeds.

Until wired, get_inactives() returns an empty result and the lineups agent reports
"unconfirmed" rather than guessing. You can also hard-code a known scratch in the
game.yaml under `sources.known_inactives` for a one-off.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Inactives:
    out: list[str] = field(default_factory=list)          # ruled OUT
    questionable: list[str] = field(default_factory=list)  # GTD / limited
    confirmed: bool = False                                # did a real source answer?
    source: str = "stub"


def get_inactives(game: dict) -> Inactives:
    """Return inactives for the game. Override/extend with a real feed.

    Honors an optional manual override in game.yaml:
        sources:
          known_inactives:
            out: ["Some Player"]
            questionable: ["Another Player"]
    """
    manual = (game.get("sources", {}) or {}).get("known_inactives")
    if manual:
        return Inactives(
            out=list(manual.get("out", [])),
            questionable=list(manual.get("questionable", [])),
            confirmed=True,
            source="manual:game.yaml",
        )
    # No real source wired yet.
    return Inactives(confirmed=False, source="stub")
