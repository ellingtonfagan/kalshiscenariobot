"""NBA adapter implementation."""
from __future__ import annotations

from typing import Any

from ..base import MarketQuotes
from . import market_discovery, marketdata, scenarios, scores, triggers


class NbaAdapter:
    key = "nba"
    label = "NBA"

    def load_scenarios(self, doc: dict[str, Any]):
        return scenarios.load_scenarios(doc)

    def find_event(self, settings) -> str | None:
        return settings.espn_event_id or scores.find_event(settings.espn_keywords)

    def get_live_state(self, event_id: str) -> scores.GameState:
        return scores.get_game_state(event_id)

    def empty_game_state(self) -> scores.GameState:
        return scores.GameState()

    def market_quotes(self, kalshi, game_tag: str) -> MarketQuotes:
        return MarketQuotes(
            props=kalshi.prop_prices(game_tag),
            winners=kalshi.winner_prices(game_tag),
        )

    def price_leg(self, leg, quotes: MarketQuotes) -> float | None:
        return scenarios.price_leg(leg, quotes.props, quotes.winners)

    def evaluate_scenario(
        self,
        scenario,
        quotes: MarketQuotes,
        game_state: scores.GameState,
        haircut: float = 1.0,
    ):
        return scenarios.evaluate(scenario, quotes.props, quotes.winners, game_state, haircut)

    def evaluate_triggers(
        self,
        game_state: scores.GameState,
        halftime_total: int | None = None,
    ):
        return triggers.evaluate(game_state, halftime_total)

    def resolve_leg(self, leg, game_state: scores.GameState) -> int | None:
        return scenarios.resolve_leg(leg, game_state)

    def discovery_series(self, settings) -> list[str]:
        configured = (settings.game.get("sources", {}) or {}).get("kalshi_series", [])
        return list(dict.fromkeys([*configured, *market_discovery.DEFAULT_DISCOVERY_SERIES]))

    def fetch_raw_markets(self, kalshi, game_tag: str, series):
        return market_discovery.fetch_raw_markets(kalshi, game_tag, series)

    def build_catalog(self, game_id: str, game_tag: str, raw_markets: list[dict[str, Any]],
                      loaded_scenarios: list[scenarios.Scenario]) -> list[dict[str, Any]]:
        return market_discovery.build_catalog(game_id, game_tag, raw_markets, loaded_scenarios)

    def snapshot_rows(self, game_id: str, loaded_scenarios, quotes: MarketQuotes,
                      haircut: dict[str, float]) -> list[dict[str, Any]]:
        return marketdata.snapshot_rows(game_id, loaded_scenarios, quotes.props, quotes.winners, haircut)
