"""Sport adapter registry for broader Kalshi sports coverage."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class SportPort:
    key: str
    label: str
    status: str
    first_markets: tuple[str, ...]
    data_needs: tuple[str, ...]
    blockers: tuple[str, ...]
    modules: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return asdict(self)


SPORT_PORTS: dict[str, SportPort] = {
    "nba": SportPort(
        key="nba",
        label="NBA",
        status="active-runtime",
        first_markets=(
            "player points/rebounds/assists/threes/minutes",
            "game winner",
            "game total and spread after ticker mapping",
        ),
        data_needs=(
            "ESPN game state and box score",
            "licensed or manual lineups/inactives",
            "Kalshi prop and game markets",
        ),
        blockers=(
            "game_total and spread resolver wiring",
            "lineups feed is still a stub unless manually configured",
        ),
        modules=("scores.py", "scenarios.py", "triggers.py"),
    ),
    "soccer": SportPort(
        key="soccer",
        label="Soccer",
        status="research-only",
        first_markets=(
            "match winner",
            "draw/no-draw or double chance when listed",
            "team totals and game total goals",
            "scoreline-set watchlists",
        ),
        data_needs=(
            "fixture and final score source",
            "licensed lineup/injury/suspension source",
            "market rules for non-participation and voids",
            "expected-goals calibration inputs",
        ),
        blockers=(
            "three-way draw logic",
            "void/non-participation rules vary by market",
            "live xG and lineup feeds need explicit permission",
        ),
        modules=("soccer_research.py",),
    ),
    "mlb": SportPort(
        key="mlb",
        label="MLB",
        status="planned",
        first_markets=("moneyline", "run line", "game total", "pitcher strikeouts"),
        data_needs=(
            "probable and confirmed starters",
            "lineups",
            "weather/park context",
            "box score and final resolver",
        ),
        blockers=(
            "starting pitcher scratches materially change fair value",
            "bullpen and lineup state need reliable data",
            "extra-innings settlement assumptions must be verified",
        ),
    ),
    "nfl": SportPort(
        key="nfl",
        label="NFL",
        status="planned",
        first_markets=("moneyline", "spread", "game total", "selected player props"),
        data_needs=(
            "official injury/status reports",
            "drive and clock state",
            "depth chart and starter confirmation",
            "box score and final resolver",
        ),
        blockers=(
            "injury/news latency is high impact",
            "clock and drive state are central to live fair value",
            "garbage-time modeling is required for props",
        ),
    ),
    "nhl": SportPort(
        key="nhl",
        label="NHL",
        status="planned",
        first_markets=("moneyline", "puck line", "game total", "shots-on-goal props"),
        data_needs=(
            "starting goalie confirmation",
            "line combinations and scratches",
            "score/period/empty-net state",
            "box score and final resolver",
        ),
        blockers=(
            "goalie confirmation is critical",
            "empty-net states affect totals and puck lines",
            "low-scoring distributions make calibration sensitive",
        ),
    ),
    "cbb": SportPort(
        key="cbb",
        label="College Basketball",
        status="planned",
        first_markets=("moneyline", "spread", "game total"),
        data_needs=(
            "team schedule/event IDs",
            "tempo and efficiency inputs",
            "injury/availability source",
            "box score and final resolver",
        ),
        blockers=(
            "team identifiers and neutral sites need careful mapping",
            "player prop coverage is inconsistent",
            "data quality varies by competition",
        ),
    ),
    "tennis": SportPort(
        key="tennis",
        label="Tennis",
        status="planned",
        first_markets=("match winner", "set winner", "total games"),
        data_needs=(
            "match schedule and surface",
            "serve order and live score",
            "retirement/walkover settlement rules",
            "player injury/status context",
        ),
        blockers=(
            "retirement and walkover rules must be verified",
            "serve order dominates live fair value",
            "injury information is difficult to verify reliably",
        ),
    ),
}


def list_ports() -> list[dict]:
    return [port.as_dict() for port in SPORT_PORTS.values()]


def active_ports() -> list[dict]:
    return [
        port.as_dict()
        for port in SPORT_PORTS.values()
        if port.status in {"active-runtime", "research-only"}
    ]
