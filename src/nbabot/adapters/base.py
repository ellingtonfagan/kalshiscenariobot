"""Sport adapter interface for runtime phases.

Adapters normalize sport-specific market parsing, live state, scenario evaluation, and
resolution behind one facade. Core phases should depend on this interface instead of
importing NBA modules directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..config import Settings
    from ..kalshi import KalshiClient, Quote


@dataclass(frozen=True)
class MarketQuotes:
    props: dict[Any, "Quote"]
    winners: dict[str, "Quote"]


class SportAdapter(Protocol):
    key: str
    label: str

    def load_scenarios(self, doc: dict[str, Any]) -> tuple[list[Any], dict, dict]:
        ...

    def find_event(self, settings: "Settings") -> str | None:
        ...

    def get_live_state(self, event_id: str) -> Any:
        ...

    def empty_game_state(self) -> Any:
        ...

    def market_quotes(self, kalshi: "KalshiClient", game_tag: str) -> MarketQuotes:
        ...

    def price_leg(self, leg: Any, quotes: MarketQuotes) -> float | None:
        ...

    def evaluate_scenario(
        self,
        scenario: Any,
        quotes: MarketQuotes,
        game_state: Any,
        haircut: float = 1.0,
    ) -> Any:
        ...

    def evaluate_triggers(
        self,
        game_state: Any,
        halftime_total: int | None = None,
    ) -> list[Any]:
        ...

    def resolve_leg(self, leg: Any, game_state: Any) -> int | None:
        ...

    def discovery_series(self, settings: "Settings") -> list[str]:
        ...

    def fetch_raw_markets(
        self,
        kalshi: "KalshiClient",
        game_tag: str,
        series: list[str] | tuple[str, ...],
    ) -> list[dict[str, Any]]:
        ...

    def build_catalog(
        self,
        game_id: str,
        game_tag: str,
        raw_markets: list[dict[str, Any]],
        scenarios: list[Any],
    ) -> list[dict[str, Any]]:
        ...

    def snapshot_rows(
        self,
        game_id: str,
        scenarios: list[Any],
        quotes: MarketQuotes,
        haircut: dict[str, float],
    ) -> list[dict[str, Any]]:
        ...
