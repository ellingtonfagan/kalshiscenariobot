"""Shared agent context: wires config → kalshi client → scenarios → data paths."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import calibration
from ..adapters import SportAdapter, get_adapter
from ..config import Settings, load_settings
from ..kalshi import KalshiClient
from ..scenarios import Scenario


@dataclass
class Context:
    settings: Settings
    kalshi: KalshiClient
    adapter: SportAdapter
    scenarios: list[Scenario]
    market_map: dict[str, Any]
    haircut: dict[str, float]
    calibration_overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def game_tag(self) -> str:
        return self.settings.kalshi_game_tag

    def read_json(self, suffix: str) -> dict | None:
        p = self.settings.data_path(suffix)
        return json.loads(p.read_text()) if p.exists() else None

    def write_json(self, suffix: str, payload: dict) -> Path:
        p = self.settings.data_path(suffix)
        p.write_text(json.dumps(payload, indent=2, default=str))
        return p


def load_context(game_id: str | None = None) -> Context:
    settings = load_settings(game_id)
    adapter = get_adapter(settings.sport)
    kalshi = KalshiClient(
        settings.kalshi_api_key,
        settings.kalshi_private_key_path,
        settings.kalshi_api_base,
        demo_api_key=settings.kalshi_demo_api_key,
        demo_private_key_path=settings.kalshi_demo_private_key_path,
    )
    scen, mm, hc = adapter.load_scenarios(settings.scenarios_doc)
    overrides = calibration.load_overrides(settings.calibration_overrides_path)
    scen, hc = calibration.apply_overrides(scen, hc, overrides)
    return Context(settings, kalshi, adapter, scen, mm, hc, overrides)


def adapter_for(ctx: Context) -> SportAdapter:
    adapter = getattr(ctx, "adapter", None)
    if adapter is not None:
        return adapter
    settings = getattr(ctx, "settings", None)
    return get_adapter(getattr(settings, "sport", "nba"))


def resolve_event_id(ctx: Context) -> str | None:
    """Resolve the configured event id through the active sport adapter."""
    return adapter_for(ctx).find_event(ctx.settings)
