"""Market identity normalization for Kalshi and sportsbook rows.

This module is intentionally pure: it consumes slate/matcher artifacts and returns
deterministic identities without fetching new data.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

from .sources.registry import load_source_config


def _alias_map(rows: list[tuple[str, tuple[str, ...]]]) -> dict[str, str]:
    out = {}
    for canonical, aliases in rows:
        out[canonical.lower()] = canonical
        out[canonical.replace("_", " ").lower()] = canonical
        out[canonical.replace("_", "").lower()] = canonical
        for alias in aliases:
            out[str(alias).lower()] = canonical
    return out


TEAM_ALIASES = {
    "soccer_world_cup": _alias_map([
        ("unitedstates", ("usa", "u.s.a", "us", "united states", "united states of america")),
        ("england", ("eng",)),
        ("spain", ("esp", "españa")),
        ("portugal", ("por",)),
        ("argentina", ("arg",)),
        ("france", ("fra",)),
        ("belgium", ("bel",)),
        ("colombia", ("col",)),
        ("norway", ("nor",)),
        ("germany", ("ger", "deutschland")),
        ("brazil", ("bra",)),
        ("mexico", ("mex",)),
        ("canada", ("can",)),
        ("croatia", ("cro",)),
        ("italy", ("ita",)),
        ("netherlands", ("ned", "holland")),
        ("uruguay", ("uru",)),
        ("japan", ("jpn",)),
        ("southkorea", ("south korea", "korea republic", "kor")),
        ("morocco", ("mar",)),
        ("switzerland", ("sui",)),
        ("denmark", ("den",)),
        ("sweden", ("swe",)),
        ("poland", ("pol",)),
        ("australia", ("aus",)),
    ]),
    "nba": _alias_map([
        ("atlantahawks", ("atl", "atlanta", "hawks")),
        ("bostonceltics", ("bos", "boston", "celtics")),
        ("brooklynnets", ("bkn", "brooklyn", "nets")),
        ("charlottehornets", ("cha", "charlotte", "hornets")),
        ("chicagobulls", ("chi", "chicago", "bulls")),
        ("clevelandcavaliers", ("cle", "cleveland", "cavaliers", "cavs")),
        ("dallasmavericks", ("dal", "dallas", "mavericks", "mavs")),
        ("denvernuggets", ("den", "denver", "nuggets")),
        ("detroitpistons", ("det", "detroit", "pistons")),
        ("goldenstatewarriors", ("gs", "gsw", "golden state", "warriors")),
        ("houstonrockets", ("hou", "houston", "rockets")),
        ("indianapacers", ("ind", "indiana", "pacers")),
        ("laclippers", ("lac", "la clippers", "los angeles clippers", "clippers")),
        ("lalakers", ("lal", "la lakers", "los angeles lakers", "lakers")),
        ("memphisgrizzlies", ("mem", "memphis", "grizzlies")),
        ("miamiheat", ("mia", "miami", "heat")),
        ("milwaukeebucks", ("mil", "milwaukee", "bucks")),
        ("minnesotatimberwolves", ("min", "minnesota", "timberwolves", "wolves")),
        ("neworleanspelicans", ("no", "nop", "new orleans", "pelicans")),
        ("newyorkknicks", ("ny", "nyk", "new york", "knicks")),
        ("oklahomacitythunder", ("okc", "oklahoma city", "thunder")),
        ("orlandomagic", ("orl", "orlando", "magic")),
        ("philadelphia76ers", ("phi", "philadelphia", "76ers", "sixers")),
        ("phoenixsuns", ("phx", "phoenix", "suns")),
        ("portlandtrailblazers", ("por", "portland", "trail blazers", "blazers")),
        ("sacramentokings", ("sac", "sacramento", "kings")),
        ("sanantoniospurs", ("sa", "sas", "san antonio", "spurs")),
        ("torontoraptors", ("tor", "toronto", "raptors")),
        ("utahjazz", ("uta", "utah", "jazz")),
        ("washingtonwizards", ("was", "washington", "wizards")),
    ]),
    "wnba": _alias_map([
        ("atlantadream", ("atl", "atlanta", "dream")),
        ("chicagosky", ("chi", "chicago", "sky")),
        ("connecticutsun", ("conn", "connecticut", "sun")),
        ("dallaswings", ("dal", "dallas", "wings")),
        ("goldenstatevalkyries", ("gs", "gsv", "golden state", "valkyries")),
        ("indianafever", ("ind", "indiana", "fever")),
        ("lasvegasaces", ("lv", "las vegas", "aces")),
        ("losangelessparks", ("la", "los angeles", "sparks")),
        ("minnesotalynx", ("min", "minnesota", "lynx")),
        ("newyorkliberty", ("ny", "new york", "liberty")),
        ("phoenixmercury", ("phx", "phoenix", "mercury")),
        ("seattlestorm", ("sea", "seattle", "storm")),
        ("washingtonmystics", ("was", "washington", "mystics")),
    ]),
    "mlb": _alias_map([
        ("arizonadiamondbacks", ("ari", "az", "arizona", "diamondbacks", "d-backs")),
        ("athletics", ("ath", "oakland athletics", "a's")),
        ("atlantabraves", ("atl", "atlanta", "braves")),
        ("baltimoreorioles", ("bal", "baltimore", "orioles")),
        ("bostonredsox", ("bos", "boston", "red sox")),
        ("chicagocubs", ("chc", "chicago cubs", "cubs")),
        ("chicagowhitesox", ("chw", "cws", "chicago white sox", "white sox")),
        ("cincinnatireds", ("cin", "cincinnati", "reds")),
        ("clevelandguardians", ("cle", "cleveland", "guardians")),
        ("coloradorockies", ("col", "colorado", "rockies")),
        ("detroittigers", ("det", "detroit", "tigers")),
        ("houstonastros", ("hou", "houston", "astros")),
        ("kansascityroyals", ("kc", "kansas city", "royals")),
        ("losangelesangels", ("la angels", "los angeles angels", "anaheim", "angels")),
        ("losangelesdodgers", ("lad", "los angeles d", "la dodgers", "los angeles dodgers", "dodgers")),
        ("miamimarlins", ("mia", "miami", "marlins")),
        ("milwaukeebrewers", ("mil", "milwaukee", "brewers")),
        ("minnesotatwins", ("min", "minnesota", "twins")),
        ("newyorkmets", ("nym", "new york m", "new york mets", "mets")),
        ("newyorkyankees", ("nyy", "new york y", "new york yankees", "yankees")),
        ("philadelphiaphillies", ("phi", "philadelphia", "phillies")),
        ("pittsburghpirates", ("pit", "pittsburgh", "pirates")),
        ("sandiegopadres", ("sd", "san diego", "padres")),
        ("sanfranciscogiants", ("sf", "san francisco", "giants")),
        ("seattlemariners", ("sea", "seattle", "mariners")),
        ("stlouiscardinals", ("stl", "st louis", "st. louis", "cardinals")),
        ("tampabayrays", ("tb", "tampa bay", "rays")),
        ("texasrangers", ("tex", "texas", "rangers")),
        ("torontobluejays", ("tor", "toronto", "blue jays")),
        ("washingtonnationals", ("was", "washington", "nationals")),
    ]),
    "nfl": _alias_map([
        ("arizonacardinals", ("ari", "arizona", "cardinals")),
        ("atlantafalcons", ("atl", "atlanta", "falcons")),
        ("baltimoreravens", ("bal", "baltimore", "ravens")),
        ("buffalobills", ("buf", "buffalo", "bills")),
        ("carolinapanthers", ("car", "carolina", "panthers")),
        ("chicagobears", ("chi", "chicago", "bears")),
        ("cincinnatibengals", ("cin", "cincinnati", "bengals")),
        ("clevelandbrowns", ("cle", "cleveland", "browns")),
        ("dallascowboys", ("dal", "dallas", "cowboys")),
        ("denverbroncos", ("den", "denver", "broncos")),
        ("detroitlions", ("det", "detroit", "lions")),
        ("greenbaypackers", ("gb", "green bay", "packers")),
        ("houstontexans", ("hou", "houston", "texans")),
        ("indianapoliscolts", ("ind", "indianapolis", "colts")),
        ("jacksonvillejaguars", ("jax", "jacksonville", "jaguars")),
        ("kansascitychiefs", ("kc", "kansas city", "chiefs")),
        ("lasvegasraiders", ("lv", "las vegas", "raiders")),
        ("losangeleschargers", ("lac", "la chargers", "los angeles chargers", "chargers")),
        ("losangelesrams", ("lar", "la rams", "los angeles rams", "rams")),
        ("miamidolphins", ("mia", "miami", "dolphins")),
        ("minnesotavikings", ("min", "minnesota", "vikings")),
        ("newenglandpatriots", ("ne", "new england", "patriots")),
        ("neworleanssaints", ("no", "new orleans", "saints")),
        ("newyorkgiants", ("nyg", "new york giants", "giants")),
        ("newyorkjets", ("nyj", "new york jets", "jets")),
        ("philadelphiaeagles", ("phi", "philadelphia", "eagles")),
        ("pittsburghsteelers", ("pit", "pittsburgh", "steelers")),
        ("sanfrancisco49ers", ("sf", "san francisco", "49ers", "niners")),
        ("seattleseahawks", ("sea", "seattle", "seahawks")),
        ("tampabaybuccaneers", ("tb", "tampa bay", "buccaneers", "bucs")),
        ("tennesseetitans", ("ten", "tennessee", "titans")),
        ("washingtoncommanders", ("was", "washington", "commanders")),
    ]),
    "nhl": _alias_map([
        ("anaheimducks", ("ana", "anaheim", "ducks")),
        ("bostonbruins", ("bos", "boston", "bruins")),
        ("buffalosabres", ("buf", "buffalo", "sabres")),
        ("calgaryflames", ("cgy", "calgary", "flames")),
        ("carolinahurricanes", ("car", "carolina", "hurricanes", "canes")),
        ("chicagoblackhawks", ("chi", "chicago", "blackhawks")),
        ("coloradoavalanche", ("col", "colorado", "avalanche", "avs")),
        ("columbusbluejackets", ("cbj", "columbus", "blue jackets")),
        ("dallasstars", ("dal", "dallas", "stars")),
        ("detroitredwings", ("det", "detroit", "red wings")),
        ("edmontonoilers", ("edm", "edmonton", "oilers")),
        ("floridapanthers", ("fla", "florida", "panthers")),
        ("losangeleskings", ("la", "lak", "los angeles", "kings")),
        ("minnesotawild", ("min", "minnesota", "wild")),
        ("montrealcanadiens", ("mtl", "montreal", "canadiens", "habs")),
        ("nashvillepredators", ("nsh", "nashville", "predators")),
        ("newjerseydevils", ("nj", "new jersey", "devils")),
        ("newyorkislanders", ("nyi", "new york islanders", "islanders")),
        ("newyorkrangers", ("nyr", "new york rangers", "rangers")),
        ("ottawasenators", ("ott", "ottawa", "senators")),
        ("philadelphiaflyers", ("phi", "philadelphia", "flyers")),
        ("pittsburghpenguins", ("pit", "pittsburgh", "penguins")),
        ("sanjosesharks", ("sj", "san jose", "sharks")),
        ("seattlekraken", ("sea", "seattle", "kraken")),
        ("stlouisblues", ("stl", "st louis", "st. louis", "blues")),
        ("tampabaylightning", ("tb", "tampa bay", "lightning", "bolts")),
        ("torontomapleleafs", ("tor", "toronto", "maple leafs", "leafs")),
        ("utahmammoth", ("uta", "utah", "mammoth", "utah hockey club")),
        ("vancouvercanucks", ("van", "vancouver", "canucks")),
        ("vegasgoldenknights", ("vgk", "vegas", "las vegas", "golden knights")),
        ("washingtoncapitals", ("was", "washington", "capitals", "caps")),
        ("winnipegjets", ("wpg", "winnipeg", "jets")),
    ]),
    "tennis": {},
    "mma": {},
}
TEAM_ALIASES["nba_summer_league"] = TEAM_ALIASES["nba"]

TICKER_TEAM_ABBREVIATIONS = {
    "mlb": {
        "ARI": "arizonadiamondbacks",
        "AZ": "arizonadiamondbacks",
        "ATH": "athletics",
        "ATL": "atlantabraves",
        "BAL": "baltimoreorioles",
        "BOS": "bostonredsox",
        "CHC": "chicagocubs",
        "CWS": "chicagowhitesox",
        "CHW": "chicagowhitesox",
        "CIN": "cincinnatireds",
        "CLE": "clevelandguardians",
        "COL": "coloradorockies",
        "DET": "detroittigers",
        "HOU": "houstonastros",
        "KC": "kansascityroyals",
        "KCR": "kansascityroyals",
        "LAA": "losangelesangels",
        "LAD": "losangelesdodgers",
        "MIA": "miamimarlins",
        "MIL": "milwaukeebrewers",
        "MIN": "minnesotatwins",
        "NYM": "newyorkmets",
        "NYY": "newyorkyankees",
        "PHI": "philadelphiaphillies",
        "PIT": "pittsburghpirates",
        "SD": "sandiegopadres",
        "SF": "sanfranciscogiants",
        "SEA": "seattlemariners",
        "STL": "stlouiscardinals",
        "TB": "tampabayrays",
        "TOR": "torontobluejays",
        "TEX": "texasrangers",
        "WSH": "washingtonnationals",
        "WSN": "washingtonnationals",
    },
    "wnba": {
        "ATL": "atlantadream",
        "CHI": "chicagosky",
        "CONN": "connecticutsun",
        "DAL": "dallaswings",
        "GS": "goldenstatevalkyries",
        "GSW": "goldenstatevalkyries",
        "IND": "indianafever",
        "LA": "losangelessparks",
        "LV": "lasvegasaces",
        "MIN": "minnesotalynx",
        "NY": "newyorkliberty",
        "PHX": "phoenixmercury",
        "SEA": "seattlestorm",
        "WAS": "washingtonmystics",
        "WSH": "washingtonmystics",
    },
    "nba": {
        "ATL": "atlantahawks",
        "BOS": "bostonceltics",
        "BKN": "brooklynnets",
        "CHA": "charlottehornets",
        "CHI": "chicagobulls",
        "CLE": "clevelandcavaliers",
        "DAL": "dallasmavericks",
        "DEN": "denvernuggets",
        "DET": "detroitpistons",
        "GS": "goldenstatewarriors",
        "GSW": "goldenstatewarriors",
        "HOU": "houstonrockets",
        "IND": "indianapacers",
        "LAC": "laclippers",
        "LAL": "lalakers",
        "MEM": "memphisgrizzlies",
        "MIA": "miamiheat",
        "MIL": "milwaukeebucks",
        "MIN": "minnesotatimberwolves",
        "NO": "neworleanspelicans",
        "NOP": "neworleanspelicans",
        "NY": "newyorkknicks",
        "NYK": "newyorkknicks",
        "OKC": "oklahomacitythunder",
        "ORL": "orlandomagic",
        "PHI": "philadelphia76ers",
        "PHX": "phoenixsuns",
        "POR": "portlandtrailblazers",
        "SAC": "sacramentokings",
        "SA": "sanantoniospurs",
        "SAS": "sanantoniospurs",
        "TOR": "torontoraptors",
        "UTA": "utahjazz",
        "WAS": "washingtonwizards",
    },
    "nba_summer_league": {},
    "nfl": {
        "ARI": "arizonacardinals",
        "ATL": "atlantafalcons",
        "BAL": "baltimoreravens",
        "BUF": "buffalobills",
        "CAR": "carolinapanthers",
        "CHI": "chicagobears",
        "CIN": "cincinnatibengals",
        "CLE": "clevelandbrowns",
        "DAL": "dallascowboys",
        "DEN": "denverbroncos",
        "DET": "detroitlions",
        "GB": "greenbaypackers",
        "HOU": "houstontexans",
        "IND": "indianapoliscolts",
        "JAX": "jacksonvillejaguars",
        "KC": "kansascitychiefs",
        "LV": "lasvegasraiders",
        "LAC": "losangeleschargers",
        "LAR": "losangelesrams",
        "MIA": "miamidolphins",
        "MIN": "minnesotavikings",
        "NE": "newenglandpatriots",
        "NO": "neworleanssaints",
        "NYG": "newyorkgiants",
        "NYJ": "newyorkjets",
        "PHI": "philadelphiaeagles",
        "PIT": "pittsburghsteelers",
        "SF": "sanfrancisco49ers",
        "SEA": "seattleseahawks",
        "TB": "tampabaybuccaneers",
        "TEN": "tennesseetitans",
        "WAS": "washingtoncommanders",
    },
    "soccer_world_cup": {
        "ARG": "argentina",
        "AUS": "australia",
        "BEL": "belgium",
        "BRA": "brazil",
        "CAN": "canada",
        "COL": "colombia",
        "CRO": "croatia",
        "DEN": "denmark",
        "ENG": "england",
        "ESP": "spain",
        "FRA": "france",
        "GER": "germany",
        "ITA": "italy",
        "JPN": "japan",
        "KOR": "southkorea",
        "MAR": "morocco",
        "MEX": "mexico",
        "NED": "netherlands",
        "NOR": "norway",
        "POL": "poland",
        "POR": "portugal",
        "SUI": "switzerland",
        "SWE": "sweden",
        "URU": "uruguay",
        "USA": "unitedstates",
    },
}
TICKER_TEAM_ABBREVIATIONS["nba_summer_league"] = TICKER_TEAM_ABBREVIATIONS["nba"]

KALSHI_SERIES_SPORTS = {
    "KXMLB": "mlb",
    "KXWNBA": "wnba",
    "KXNBA": "nba",
    "KXNFL": "nfl",
    "KXWC": "soccer_world_cup",
    "KXUCL": "soccer_world_cup",
}

MARKET_ALIASES = {
    "h2h": "moneyline",
    "moneyline": "moneyline",
    "match_winner": "moneyline",
    "winner": "moneyline",
    "win": "moneyline",
    "advances": "team_advances",
    "team_advances": "team_advances",
    "spreads": "spread",
    "spread": "spread",
    "totals": "total",
    "total": "total",
    "over_under": "total",
    "points": "player_points",
    "player_points": "player_points",
    "rebounds": "player_rebounds",
    "player_rebounds": "player_rebounds",
    "assists": "player_assists",
    "player_assists": "player_assists",
    "threes": "player_threes",
    "corners": "corners",
}


@dataclass(frozen=True)
class MarketIdentity:
    sport: str
    league: str | None
    event_key: str | None
    market_type: str
    line: float | None
    side: str | None
    start_date: str | None
    participant: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _words(raw: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(raw or "").lower())


def _float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _date_bucket(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def normalize_team(name: Any, sport: str) -> str | None:
    if not name:
        return None
    text = str(name).strip().lower()
    aliases = TEAM_ALIASES.get(sport, {})
    return aliases.get(text) or _norm(text)


def _sport_from_kalshi_series(series: str) -> str | None:
    for prefix, sport in sorted(KALSHI_SERIES_SPORTS.items(), key=lambda row: len(row[0]), reverse=True):
        if series.startswith(prefix):
            return sport
    return None


def _canonical_from_code(code: str, sport: str) -> str | None:
    if not code:
        return None
    code = code.upper()
    ticker_map = TICKER_TEAM_ABBREVIATIONS.get(sport, {})
    if code in ticker_map:
        return ticker_map[code]
    aliases = TEAM_ALIASES.get(sport, {})
    return aliases.get(code.lower())


def _split_compact_team_codes(raw: str, sport: str) -> tuple[str | None, str | None]:
    codes = sorted(TICKER_TEAM_ABBREVIATIONS.get(sport, {}), key=len, reverse=True)
    for left_code in codes:
        if not raw.startswith(left_code):
            continue
        right_code = raw[len(left_code):]
        if right_code in TICKER_TEAM_ABBREVIATIONS.get(sport, {}):
            return (
                _canonical_from_code(left_code, sport),
                _canonical_from_code(right_code, sport),
            )
    return None, None


def _split_compact_team_codes_with_suffix(raw: str, sport: str) -> tuple[str | None, str | None]:
    team_a, team_b = _split_compact_team_codes(raw, sport)
    if team_a and team_b:
        return team_a, team_b
    stripped = re.sub(r"G\d+$", "", raw)
    if stripped != raw:
        return _split_compact_team_codes(stripped, sport)
    return None, None


def _date_from_kalshi_code(code: str) -> str | None:
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    match = re.match(r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<day>\d{2})", str(code or ""))
    if not match:
        return None
    month = months.get(match.group("mon"))
    if month is None:
        return None
    year = 2000 + int(match.group("yy"))
    return f"{year:04d}-{month:02d}-{int(match.group('day')):02d}"


def _teams_from_kalshi_event_code(code: str, sport: str) -> tuple[str | None, str | None]:
    match = re.match(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?(?P<teams>[A-Z0-9]+)$", str(code or ""))
    if not match:
        return None, None
    return _split_compact_team_codes_with_suffix(match.group("teams"), sport)


def _event_key_from_parts(sport: str, date: str | None, team_a: str | None, team_b: str | None) -> str | None:
    teams = sorted(team for team in (team_a, team_b) if team)
    if len(teams) < 2:
        return None
    return f"{sport}:{date or 'unknown-date'}:{teams[0]}:{teams[1]}"


def _event_key_from_kalshi_event_ticker(event_ticker: str) -> tuple[str | None, str | None, str | None]:
    parts = str(event_ticker or "").split("-")
    if len(parts) < 2:
        return None, None, None
    series, code = parts[0], parts[1]
    sport = _sport_from_kalshi_series(series)
    if sport is None:
        return None, None, None
    team_a, team_b = _teams_from_kalshi_event_code(code, sport)
    date = _date_from_kalshi_code(code)
    return sport, date, _event_key_from_parts(sport, date, team_a, team_b)


def _selection_team_and_number(selection: str, sport: str) -> tuple[str | None, float | None]:
    text = str(selection or "").upper()
    codes = sorted(TICKER_TEAM_ABBREVIATIONS.get(sport, {}), key=len, reverse=True)
    for code in codes:
        if text.startswith(code):
            suffix = text[len(code):]
            return _canonical_from_code(code, sport), _float(suffix)
    return _canonical_from_code(text, sport), None


def _kalshi_leg_market_type(series: str) -> str | None:
    if series.endswith("GAME") or series.endswith("MATCH") or series.endswith("FIGHT"):
        return "moneyline"
    if series.endswith("ADVANCE"):
        return "team_advances"
    if series.endswith("TOTAL"):
        return "total"
    if series.endswith("SPREAD"):
        return "spread"
    return None


def _kalshi_spread_line(selection_number: float | None, market: dict[str, Any] | None = None) -> float | None:
    market = market or {}
    floor_strike = _float(market.get("floor_strike"))
    strike_type = str(market.get("strike_type") or "").lower()
    if floor_strike is not None and strike_type in {"", "greater"}:
        return -floor_strike
    return -(selection_number - 0.5) if selection_number is not None else None


def _kalshi_leg_line_and_side(
    series: str,
    selection: str,
    sport: str,
    market: dict[str, Any] | None = None,
) -> tuple[float | None, str | None]:
    market_type = _kalshi_leg_market_type(series)
    if market_type == "total":
        number = _float(selection)
        return (number - 0.5 if number is not None else None), "over"
    if market_type == "spread":
        team, number = _selection_team_and_number(selection, sport)
        return _kalshi_spread_line(number, market), team
    if market_type in {"moneyline", "team_advances"}:
        if str(selection).upper() == "TIE":
            return None, "draw"
        return None, _canonical_from_code(str(selection), sport)
    return None, None


def _identity_from_kalshi_leg(leg: dict[str, Any]) -> MarketIdentity | None:
    market_ticker = str(leg.get("market_ticker") or "")
    event_ticker = str(leg.get("event_ticker") or "")
    parts = market_ticker.split("-")
    if len(parts) < 3:
        return None
    series, selection = parts[0], parts[-1]
    sport, date, key = _event_key_from_kalshi_event_ticker(event_ticker)
    if sport is None:
        sport = _sport_from_kalshi_series(series)
    if sport is None:
        return None
    market_type = _kalshi_leg_market_type(series)
    if market_type is None:
        return None
    line, side = _kalshi_leg_line_and_side(series, selection, sport, leg)
    if market_type in {"moneyline", "team_advances", "spread"} and not side:
        return None
    if market_type == "total" and line is None:
        return None
    return MarketIdentity(
        sport=sport,
        league=sport.upper(),
        event_key=key,
        market_type=market_type,
        line=line,
        side=side,
        start_date=date,
        participant=side,
    )


def _identity_from_kalshi_market_ticker(
    market: dict[str, Any],
    fallback_sport: str,
) -> MarketIdentity | None:
    ticker = str(market.get("ticker") or "")
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    series, selection = parts[0], parts[-1]
    event_ticker = str(market.get("event_ticker") or "-".join(parts[:2]))
    sport, date, key = _event_key_from_kalshi_event_ticker(event_ticker)
    if sport is None:
        sport = _sport_from_kalshi_series(series) or fallback_sport
    if sport is None:
        return None
    market_type = _kalshi_leg_market_type(series)
    if market_type is None:
        return None
    line, side = _kalshi_leg_line_and_side(series, selection, sport, market)
    return MarketIdentity(
        sport=sport,
        league=sport.upper(),
        event_key=key,
        market_type=market_type,
        line=line,
        side=side,
        start_date=date,
        participant=side,
    )


def normalize_player(name: Any) -> str | None:
    words = _words(name)
    if not words:
        return None
    suffixes = {"jr", "sr", "ii", "iii", "iv"}
    filtered = [word for word in words if word not in suffixes]
    return filtered[-1] if filtered else words[-1]


@lru_cache(maxsize=1)
def _configured_market_types() -> set[str]:
    config = load_source_config()
    values = set(MARKET_ALIASES.values())
    for sport in (config.get("sports") or {}).values():
        for name in sport.get("first_markets") or []:
            values.add(MARKET_ALIASES.get(str(name).lower(), str(name).lower()))
    return values


def classify_market_type(raw_market_or_title: Any, sport: str | None = None) -> str:
    text = str(raw_market_or_title or "").lower()
    compact = _norm(text)
    for alias, canonical in MARKET_ALIASES.items():
        if alias in text or _norm(alias) in compact:
            return canonical
    if "over" in text or "under" in text or re.search(r"\b[0-9]+(?:\.[0-9]+)?\b", text):
        return "total"
    if "cover" in text or "spread" in text:
        return "spread"
    if "advance" in text:
        return "team_advances"
    if "win" in text:
        return "moneyline"
    configured = _configured_market_types()
    return "unknown" if "unknown" not in configured else "unknown"


def _line_from_text(text: Any) -> float | None:
    value = _float(text)
    if value is not None:
        return value
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(text or ""))
    return float(match.group(1)) if match else None


def _side_from_text(text: Any, market_type: str, sport: str) -> str | None:
    raw = str(text or "").lower()
    if " over " in f" {raw} " or raw.startswith("over"):
        return "over"
    if " under " in f" {raw} " or raw.startswith("under"):
        return "under"
    aliases = TEAM_ALIASES.get(sport, {})
    for phrase, normalized in sorted(aliases.items(), key=lambda row: len(row[0]), reverse=True):
        if _phrase_in_text(phrase, raw):
            return normalized
    if market_type == "moneyline" and raw:
        return normalize_team(raw, sport)
    return None


def event_key(sport: str, team_a: Any, team_b: Any, start_time: Any) -> str | None:
    teams = sorted(
        team for team in (
            normalize_team(team_a, sport),
            normalize_team(team_b, sport),
        )
        if team
    )
    if len(teams) < 2:
        return None
    date = _date_bucket(start_time) or "unknown-date"
    return f"{sport}:{date}:{teams[0]}:{teams[1]}"


def _candidate_event_key(candidate: dict[str, Any]) -> str | None:
    sport = str(candidate.get("sport") or "").lower()
    return event_key(
        sport,
        candidate.get("away_team"),
        candidate.get("home_team"),
        candidate.get("start_time"),
    )


def _participant_from_title(title: str, sport: str) -> str | None:
    before_colon = title.split(":", 1)[0] if ":" in title else ""
    if before_colon:
        return normalize_player(before_colon)
    for token in _words(title):
        team = normalize_team(token, sport)
        if team:
            return team
    return None


def _teams_from_title(title: str, sport: str) -> tuple[str | None, str | None]:
    raw = str(title or "").lower()
    found: list[str] = []
    aliases = TEAM_ALIASES.get(sport, {})
    for phrase, normalized in sorted(aliases.items(), key=lambda row: len(row[0]), reverse=True):
        if phrase and _phrase_in_text(phrase, raw) and normalized not in found:
            found.append(normalized)
        if len(found) >= 2:
            return found[0], found[1]
    separators = (" vs ", " v ", " @ ", " at ")
    for sep in separators:
        if sep in raw:
            left, _, right = raw.partition(sep)
            left_name = left.split(",")[-1].strip()
            right_name = right.split(",")[0].strip()
            if left_name and right_name:
                return normalize_team(left_name, sport), normalize_team(right_name, sport)
    return None, None


def resolve_kalshi_market(
    market: dict[str, Any],
    candidate: dict[str, Any] | None = None,
) -> MarketIdentity:
    candidate = candidate or {}
    sport = str(
        market.get("sport")
        or candidate.get("sport")
        or (market.get("sport_keys") or ["unknown"])[0]
    ).lower()
    ticker_identity = _identity_from_kalshi_market_ticker(market, sport)
    if ticker_identity is not None:
        sport = ticker_identity.sport
    title = str(market.get("title") or "")
    market_type = (
        ticker_identity.market_type
        if ticker_identity is not None
        else classify_market_type(title or market.get("market"), sport)
    )
    text_line = _line_from_text(title)
    line = (
        ticker_identity.line
        if ticker_identity is not None and ticker_identity.line is not None
        else text_line
    )
    side = (
        ticker_identity.side
        if ticker_identity is not None and ticker_identity.side
        else _side_from_text(title, market_type, sport)
    )
    start_date = (
        ticker_identity.start_date
        if ticker_identity is not None and ticker_identity.start_date
        else _date_bucket(market.get("close_time") or candidate.get("start_time"))
    )
    key = _candidate_event_key(candidate)
    if key is None and ticker_identity is not None:
        key = ticker_identity.event_key
    if key is None:
        team_a, team_b = _teams_from_title(title, sport)
        key = event_key(sport, team_a, team_b, market.get("close_time") or candidate.get("start_time"))
    return MarketIdentity(
        sport=sport,
        league=str(candidate.get("league")) if candidate.get("league") else (ticker_identity.league if ticker_identity else None),
        event_key=key,
        market_type=market_type,
        line=line,
        side=side,
        start_date=start_date,
        participant=side or _participant_from_title(title, sport),
    )


def resolve_kalshi_legs(market: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve Kalshi multivariate selected legs into comparable market identities."""
    out = []
    for index, leg in enumerate(market.get("mve_selected_legs") or []):
        if not isinstance(leg, dict):
            continue
        identity = _identity_from_kalshi_leg(leg)
        out.append({
            "index": index,
            "event_ticker": leg.get("event_ticker"),
            "market_ticker": leg.get("market_ticker"),
            "leg_side": str(leg.get("side") or "yes").lower(),
            "identity": identity,
            "supported": identity is not None,
        })
    return out


def resolve_line_market(line: dict[str, Any], candidate: dict[str, Any]) -> MarketIdentity:
    sport = str(candidate.get("sport") or line.get("sport") or "unknown").lower()
    market_type = classify_market_type(line.get("market"), sport)
    side = _side_from_text(line.get("name"), market_type, sport)
    return MarketIdentity(
        sport=sport,
        league=str(candidate.get("league")) if candidate.get("league") else None,
        event_key=_candidate_event_key(candidate),
        market_type=market_type,
        line=_line_from_text(line.get("point")),
        side=side,
        start_date=_date_bucket(candidate.get("start_time")),
        participant=normalize_player(line.get("name")) if market_type.startswith("player_") else side,
    )


def _same_line(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 0.01


def _event_key_parts(key: str | None) -> tuple[str, str, str, str] | None:
    parts = str(key or "").split(":")
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _same_event_key(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    pa = _event_key_parts(a)
    pb = _event_key_parts(b)
    if not pa or not pb:
        return False
    if pa[0] != pb[0] or pa[2:] != pb[2:]:
        return False
    return pa[1] == pb[1] or "unknown-date" in {pa[1], pb[1]}


def same_event_key(a: str | None, b: str | None) -> bool:
    return _same_event_key(a, b)


def _exact_identity(a: MarketIdentity, b: MarketIdentity) -> bool:
    return (
        a.sport == b.sport
        and a.event_key is not None
        and _same_event_key(a.event_key, b.event_key)
        and a.market_type == b.market_type
        and _same_line(a.line, b.line)
        and (a.side is None or b.side is None or a.side == b.side)
    )


def match_identities(
    kalshi: MarketIdentity,
    pool: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    exact = []
    fuzzy_pool = []
    for item in pool:
        identity = item.get("identity")
        if not isinstance(identity, MarketIdentity):
            continue
        if _exact_identity(kalshi, identity):
            exact.append({
                "candidate_id": item.get("candidate_id"),
                "line": item.get("line"),
                "identity": identity.as_dict(),
                "match_type": "exact",
                "score": 1.0,
            })
        elif (
            kalshi.event_key
            and _same_event_key(kalshi.event_key, identity.event_key)
            and identity.market_type == kalshi.market_type
        ):
            fuzzy_pool.append(item)
    if exact:
        return exact
    return [
        {
            "candidate_id": item.get("candidate_id"),
            "line": item.get("line"),
            "identity": item["identity"].as_dict(),
            "match_type": "fuzzy",
            "score": 0.25,
        }
        for item in fuzzy_pool[:5]
    ]


def identity_mismatch_reasons(kalshi: MarketIdentity, other: MarketIdentity) -> list[str]:
    reasons = []
    if kalshi.event_key is None:
        reasons.append("kalshi_event_key_missing")
    if other.event_key is None:
        reasons.append("line_event_key_missing")
    if kalshi.event_key and other.event_key and not _same_event_key(kalshi.event_key, other.event_key):
        kalshi_parts = _event_key_parts(kalshi.event_key)
        other_parts = _event_key_parts(other.event_key)
        if kalshi_parts and other_parts and kalshi_parts[0] == other_parts[0] and kalshi_parts[2:] == other_parts[2:]:
            reasons.append("date")
        else:
            reasons.append("team")
    if kalshi.market_type != other.market_type:
        reasons.append("market_type")
    if not _same_line(kalshi.line, other.line):
        reasons.append("line")
    if kalshi.side and other.side and kalshi.side != other.side:
        reasons.append("side")
    return reasons or ["unknown"]


def identity_rejection_fields(kalshi: MarketIdentity, other: MarketIdentity) -> list[str]:
    """Return exact-match contract fields that differ between two identities."""
    fields: list[str] = []
    if not kalshi.event_key or not other.event_key:
        fields.append("event_key")
    elif not _same_event_key(kalshi.event_key, other.event_key):
        kalshi_parts = _event_key_parts(kalshi.event_key)
        other_parts = _event_key_parts(other.event_key)
        if kalshi_parts and other_parts:
            if kalshi_parts[0] != other_parts[0]:
                fields.append("event_key")
            if kalshi_parts[2:] != other_parts[2:]:
                fields.append("team")
            if (
                kalshi_parts[1] != other_parts[1]
                and "unknown-date" not in {kalshi_parts[1], other_parts[1]}
            ):
                fields.append("date")
        else:
            fields.append("event_key")
    if kalshi.market_type != other.market_type:
        fields.append("market_type")
    if not _same_line(kalshi.line, other.line):
        fields.append("line")
    if kalshi.side and other.side and kalshi.side != other.side:
        fields.append("side")
    return fields or ["unknown"]


def _identity_similarity_score(kalshi: MarketIdentity, other: MarketIdentity) -> float:
    score = 0.0
    if kalshi.sport == other.sport:
        score += 100.0
    if kalshi.event_key and other.event_key:
        if _same_event_key(kalshi.event_key, other.event_key):
            score += 120.0
        else:
            kalshi_parts = _event_key_parts(kalshi.event_key)
            other_parts = _event_key_parts(other.event_key)
            if kalshi_parts and other_parts:
                if kalshi_parts[0] == other_parts[0]:
                    score += 20.0
                if kalshi_parts[2:] == other_parts[2:]:
                    score += 70.0
                elif kalshi_parts[2] in other_parts[2:] or kalshi_parts[3] in other_parts[2:]:
                    score += 35.0
                if kalshi_parts[1] == other_parts[1] or "unknown-date" in {kalshi_parts[1], other_parts[1]}:
                    score += 30.0
    if kalshi.market_type == other.market_type:
        score += 50.0
    if _same_line(kalshi.line, other.line):
        score += 25.0
    elif kalshi.line is not None and other.line is not None:
        score += max(0.0, 12.0 - min(abs(kalshi.line - other.line), 12.0))
    if kalshi.side and other.side:
        if kalshi.side == other.side:
            score += 20.0
    elif not kalshi.side and not other.side:
        score += 10.0
    return round(score, 4)


def closest_identity_rejection(
    kalshi: MarketIdentity,
    pool: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the nearest sportsbook identity that was not an exact match."""
    best: dict[str, Any] | None = None
    for item in pool:
        identity = item.get("identity")
        if not isinstance(identity, MarketIdentity):
            continue
        if _exact_identity(kalshi, identity):
            continue
        fields = identity_rejection_fields(kalshi, identity)
        score = _identity_similarity_score(kalshi, identity)
        candidate = {
            "candidate_id": item.get("candidate_id"),
            "score": score,
            "differing_fields": fields,
            "identity": identity.as_dict(),
            "line": item.get("line"),
        }
        if best is None or score > float(best.get("score") or 0):
            best = candidate
    return best


def _phrase_in_text(phrase: str, raw: str) -> bool:
    if not phrase:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
    return re.search(pattern, raw) is not None


def match_diagnostics(
    kalshi: MarketIdentity,
    pool: list[dict[str, Any]],
    *,
    include_closest: bool = True,
) -> dict[str, Any]:
    counts = Counter()
    compared = 0
    closest = None
    for item in pool:
        identity = item.get("identity")
        if not isinstance(identity, MarketIdentity):
            continue
        compared += 1
        reasons = identity_mismatch_reasons(kalshi, identity)
        if reasons == ["unknown"]:
            counts["none_unknown"] += 1
        for reason in reasons:
            counts[reason] += 1
        if include_closest and not _exact_identity(kalshi, identity):
            fields = identity_rejection_fields(kalshi, identity)
            score = _identity_similarity_score(kalshi, identity)
            candidate = {
                "candidate_id": item.get("candidate_id"),
                "score": score,
                "differing_fields": fields,
                "identity": identity.as_dict(),
                "line": item.get("line"),
            }
            if closest is None or score > float(closest.get("score") or 0):
                closest = candidate
    return {
        "compared": compared,
        "mismatch_breakdown": dict(sorted(counts.items())),
        "closest_rejection": closest,
    }
