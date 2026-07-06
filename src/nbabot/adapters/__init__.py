"""Sport adapter registry."""
from __future__ import annotations

from .base import MarketQuotes, SportAdapter
from .nba import NbaAdapter

_ADAPTERS = {
    "nba": NbaAdapter(),
}


def get_adapter(sport: str | None) -> SportAdapter:
    key = (sport or "nba").lower()
    try:
        return _ADAPTERS[key]
    except KeyError as e:
        supported = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"unsupported sport adapter {key!r}; supported: {supported}") from e


def available_adapters() -> dict[str, SportAdapter]:
    return dict(_ADAPTERS)


__all__ = ["MarketQuotes", "SportAdapter", "available_adapters", "get_adapter"]
