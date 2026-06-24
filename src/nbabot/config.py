"""Configuration loading: .env + per-game YAML.

No third-party env loader; we parse a .env file ourselves so the package stays light.
Real environment variables always win over the .env file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .guardrails import MAX_STAKE_UNITS

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without overwriting real env vars."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


@dataclass
class Settings:
    kalshi_api_key: str
    kalshi_private_key_path: Path
    kalshi_api_base: str
    game_id: str
    deliver_to: str
    dry_run: bool
    data_dir: Path
    research_db_path: Path
    calibration_overrides_path: Path
    execution_mode: str
    live_trading_ack: str
    research_override_ack: str
    research_override_max_units: float
    demo_api_base: str
    max_daily_loss_units: float
    max_game_exposure_units: float
    min_edge: float
    stale_market_seconds: int
    max_spread_cents: int
    kill_switch_path: Path
    ui_host: str
    ui_port: int
    game: dict[str, Any] = field(default_factory=dict)
    scenarios_doc: dict[str, Any] = field(default_factory=dict)

    # convenience accessors -----------------------------------------------------
    @property
    def kalshi_game_tag(self) -> str:
        return self.game["sources"]["kalshi_game_tag"]

    @property
    def espn_event_id(self) -> str | None:
        return self.game["sources"].get("espn_event_id")

    @property
    def espn_keywords(self) -> list[str]:
        return self.game["sources"].get("espn_matchup_keywords", [])

    @property
    def tracked_players(self) -> list[str]:
        return self.game.get("tracked_players", [])

    @property
    def unit_usd(self) -> float:
        return float(self.game.get("bankroll", {}).get("unit_usd", 1))

    def data_path(self, suffix: str) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / f"{self.game_id}.{suffix}"


def load_settings(game_id: str | None = None) -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    gid = game_id or os.environ.get("NBABOT_GAME_ID", "NBA-2026-FINALS-G3")

    game = yaml.safe_load((CONFIG_DIR / f"{gid}.game.yaml").read_text())
    scen = yaml.safe_load((CONFIG_DIR / f"{gid}.scenarios.yaml").read_text())

    pk_path = Path(os.environ.get("KALSHI_PRIVATE_KEY_PATH", "./secrets/kalshi-private-key.pem"))
    if not pk_path.is_absolute():
        pk_path = (REPO_ROOT / pk_path).resolve()

    data_dir = Path(os.environ.get("NBABOT_DATA_DIR", REPO_ROOT / "data"))
    if not data_dir.is_absolute():
        data_dir = (REPO_ROOT / data_dir).resolve()

    research_db_path = Path(os.environ.get("NBABOT_RESEARCH_DB", data_dir / "research.sqlite"))
    if not research_db_path.is_absolute():
        research_db_path = (REPO_ROOT / research_db_path).resolve()

    calibration_overrides_path = Path(
        os.environ.get("NBABOT_CALIBRATION_OVERRIDES", data_dir / "calibration_overrides.json")
    )
    if not calibration_overrides_path.is_absolute():
        calibration_overrides_path = (REPO_ROOT / calibration_overrides_path).resolve()

    kill_switch_path = Path(os.environ.get("NBABOT_KILL_SWITCH", data_dir / "KILL_SWITCH"))
    if not kill_switch_path.is_absolute():
        kill_switch_path = (REPO_ROOT / kill_switch_path).resolve()

    return Settings(
        kalshi_api_key=os.environ.get("KALSHI_API_KEY", ""),
        kalshi_private_key_path=pk_path,
        kalshi_api_base=os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com"),
        game_id=gid,
        deliver_to=os.environ.get("NBABOT_DELIVER_TO", "stdout"),
        dry_run=os.environ.get("NBABOT_DRY_RUN", "1") not in ("0", "false", "False", ""),
        data_dir=data_dir,
        research_db_path=research_db_path,
        calibration_overrides_path=calibration_overrides_path,
        execution_mode=os.environ.get("NBABOT_EXECUTION_MODE", "paper").lower(),
        live_trading_ack=os.environ.get("NBABOT_LIVE_TRADING_ACK", ""),
        research_override_ack=os.environ.get("NBABOT_RESEARCH_OVERRIDE_ACK", ""),
        research_override_max_units=min(
            float(os.environ.get("NBABOT_RESEARCH_OVERRIDE_MAX_UNITS", "1")),
            1.0,
        ),
        demo_api_base=os.environ.get(
            "NBABOT_DEMO_API_BASE",
            "https://external-api.demo.kalshi.co/trade-api/v2",
        ).rstrip("/"),
        max_daily_loss_units=float(os.environ.get("NBABOT_MAX_DAILY_LOSS_UNITS", "2")),
        max_game_exposure_units=float(
            os.environ.get("NBABOT_MAX_GAME_EXPOSURE_UNITS", str(MAX_STAKE_UNITS))
        ),
        min_edge=float(os.environ.get("NBABOT_MIN_EDGE", "0.05")),
        stale_market_seconds=int(os.environ.get("NBABOT_STALE_MARKET_SECONDS", "90")),
        max_spread_cents=int(os.environ.get("NBABOT_MAX_SPREAD_CENTS", "10")),
        kill_switch_path=kill_switch_path,
        ui_host=os.environ.get("NBABOT_UI_HOST", "127.0.0.1"),
        ui_port=int(os.environ.get("NBABOT_UI_PORT", "8765")),
        game=game,
        scenarios_doc=scen,
    )
