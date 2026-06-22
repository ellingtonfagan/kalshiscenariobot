"""ESPN box score + win probability. Ported/extended from live_signals.py.

get_game_state() is the single choke-point for live game data. If ESPN reshapes its
payload, fix the parser HERE and everything downstream keeps working.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

_S = requests.Session()
_S.headers.update({"User-Agent": "Mozilla/5.0 (nba-scenario-bot)"})

SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={id}"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


@dataclass
class PlayerLine:
    name: str
    minutes: int = 0
    pts: int = 0
    reb: int = 0
    ast: int = 0
    threes: int = 0
    fouls: int = 0

    def stat(self, which: str) -> int:
        return {
            "points": self.pts, "rebounds": self.reb, "assists": self.ast,
            "threes": self.threes, "minutes": self.minutes, "fouls": self.fouls,
        }.get(which, 0)


@dataclass
class GameState:
    state: str = "pre"            # pre | in | post
    period: int = 0
    clock: str = ""
    status_detail: str = ""
    home_abbr: str | None = None
    away_abbr: str | None = None
    home_score: int = 0
    away_score: int = 0
    home_wp: float | None = None  # 0..1 home win probability (ESPN model)
    players: dict[str, PlayerLine] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def is_final(self) -> bool:
        return self.state == "post"

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    @property
    def total(self) -> int:
        return self.home_score + self.away_score

    def player(self, full_or_surname: str) -> PlayerLine | None:
        key = full_or_surname.lower()
        if key in self.players:
            return self.players[key]
        # surname fallback
        for name, line in self.players.items():
            if name.split()[-1] == key or key.split()[-1] == name.split()[-1]:
                return line
        return None


def find_event(keywords: list[str]) -> str | None:
    """Resolve today's ESPN event id by matchup keyword (any keyword matches)."""
    try:
        d = _S.get(SCOREBOARD, timeout=5).json()
    except Exception:
        return None
    kw = [k.upper() for k in keywords]
    for ev in d.get("events", []):
        name = (ev.get("name", "") + " " + ev.get("shortName", "")).upper()
        if any(k in name for k in kw):
            return ev.get("id")
    return None


def _int(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def _parse_box(d: dict, gs: GameState) -> None:
    """Fill gs.players from summary['boxscore']['players'] using the stat name labels."""
    for team in d.get("boxscore", {}).get("players", []):
        for grp in team.get("statistics", []):
            names = [n.upper() for n in grp.get("names", [])]
            idx = {label: names.index(label) for label in
                   ("MIN", "3PT", "REB", "AST", "PF", "PTS") if label in names}
            for ath in grp.get("athletes", []):
                stats = ath.get("stats", [])
                if not stats:
                    continue
                full = ath.get("athlete", {}).get("displayName", "")
                threes = 0
                if "3PT" in idx and idx["3PT"] < len(stats):
                    made = str(stats[idx["3PT"]]).split("-")[0]
                    threes = _int(made)
                pl = PlayerLine(
                    name=full,
                    minutes=_int(stats[idx["MIN"]]) if "MIN" in idx and idx["MIN"] < len(stats) else 0,
                    pts=_int(stats[idx["PTS"]]) if "PTS" in idx and idx["PTS"] < len(stats) else 0,
                    reb=_int(stats[idx["REB"]]) if "REB" in idx and idx["REB"] < len(stats) else 0,
                    ast=_int(stats[idx["AST"]]) if "AST" in idx and idx["AST"] < len(stats) else 0,
                    fouls=_int(stats[idx["PF"]]) if "PF" in idx and idx["PF"] < len(stats) else 0,
                    threes=threes,
                )
                gs.players[full.lower()] = pl


def get_game_state(event_id: str) -> GameState:
    gs = GameState()
    try:
        d = _S.get(SUMMARY.format(id=event_id), timeout=6).json()
    except Exception as e:  # fail soft — caller downgrades scenarios, never crashes
        gs.errors.append(f"summary fetch: {e}")
        return gs

    try:
        comp = d.get("header", {}).get("competitions", [{}])[0]
        st = comp.get("status", {}).get("type", {})
        gs.state = st.get("state", "pre")
        gs.status_detail = st.get("shortDetail", "")
        gs.period = _int(comp.get("status", {}).get("period", 0))
        gs.clock = comp.get("status", {}).get("displayClock", "")
        for c in comp.get("competitors", []):
            abbr = c.get("team", {}).get("abbreviation")
            score = _int(c.get("score"))
            if c.get("homeAway") == "home":
                gs.home_abbr, gs.home_score = abbr, score
            else:
                gs.away_abbr, gs.away_score = abbr, score
        wp = d.get("winprobability", [])
        if wp:
            p = wp[-1].get("homeWinPercentage")
            if p is not None:
                gs.home_wp = float(p)
    except Exception as e:
        gs.errors.append(f"header parse: {e}")

    try:
        _parse_box(d, gs)
    except Exception as e:
        gs.errors.append(f"box parse: {e}")

    return gs


# ── de-vig helper kept from live_signals (for an optional market sanity check) ──
def devig(p_home: float | None, p_away: float | None) -> float | None:
    if p_home is None or p_away is None:
        return p_home
    tot = p_home + p_away
    return p_home / tot if tot > 0 else p_home
