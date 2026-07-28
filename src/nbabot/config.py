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
    sport: str
    deliver_to: str
    dry_run: bool
    data_dir: Path
    research_db_path: Path
    calibration_overrides_path: Path
    execution_mode: str
    live_trading_ack: str
    broad_slate_execution: str
    research_override_ack: str
    research_override_max_units: float
    demo_api_base: str
    kalshi_demo_api_key: str
    kalshi_demo_private_key_path: Path
    paper_demo_daily_trade_cap: int
    qual_daily_trade_cap: int
    unit_fraction: float
    paper_bankroll_usd: float
    max_order_notional_fraction: float
    resting_order_max_age_minutes: int
    max_daily_loss_units: float
    max_daily_exposure_units: float
    max_game_exposure_units: float
    min_edge: float
    demo_min_edge: float
    qual_min_edge: float
    kalshi_taker_fee_factor: float
    kalshi_maker_fee_factor: float
    near_miss_window: float
    near_miss_investigations_per_cycle: int
    qaq_floor_bonus: float
    confluence_agree_delta: float
    confluence_edge_bonus: float
    confluence_veto_delta: float
    qual_signal_max_age_hours: float
    qual_lessons_top_n: int
    qual_llm_cmd: str
    qual_llm_timeout_seconds: int
    qual_fallback_model: str
    research_teams_path: Path
    news_window_hours: float
    news_user_agent: str
    event_trigger_cooldown_minutes: int
    event_trigger_daily_cap: int
    max_plausible_edge: float
    stale_market_seconds: int
    max_spread_cents: int
    orderbook_depth: int | None
    book_watch_iterations: int
    book_watch_interval_seconds: float
    closing_snapshot_window_minutes: int
    concentration_max_winner_share: float
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
    def configured_unit_usd(self) -> float | None:
        """Legacy static YAML unit, for diagnostics only.

        Runtime unit sizing is canonicalized in ``nbabot.units`` from the
        resolved bankroll and ``unit_fraction``. Do not use this value for
        sizing or risk caps.
        """
        bankroll = self.game.get("bankroll", {})
        if not isinstance(bankroll, dict) or "unit_usd" not in bankroll:
            return None
        return float(bankroll["unit_usd"])

    @property
    def execution_min_edge(self) -> float:
        if self.execution_mode in {"demo", "paper"}:
            return float(self.demo_min_edge)
        return float(self.min_edge)

    def data_path(self, suffix: str) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / f"{self.game_id}.{suffix}"


def load_settings(game_id: str | None = None) -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    gid = game_id or os.environ.get("NBABOT_GAME_ID", "NBA-2026-FINALS-G3")

    game = yaml.safe_load((CONFIG_DIR / f"{gid}.game.yaml").read_text())
    scen = yaml.safe_load((CONFIG_DIR / f"{gid}.scenarios.yaml").read_text())
    sport = str(game.get("sport") or game.get("game", {}).get("sport") or "nba").lower()

    pk_path = Path(os.environ.get("KALSHI_PRIVATE_KEY_PATH", "./secrets/kalshi-private-key.pem"))
    if not pk_path.is_absolute():
        pk_path = (REPO_ROOT / pk_path).resolve()

    demo_pk_path = Path(
        os.environ.get("KALSHI_DEMO_PRIVATE_KEY_PATH")
        or "./secrets/kalshi-demo-private-key.txt"
    )
    if not demo_pk_path.is_absolute():
        demo_pk_path = (REPO_ROOT / demo_pk_path).resolve()

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

    research_teams_path = Path(
        os.environ.get("NBABOT_RESEARCH_TEAMS", CONFIG_DIR / "research_teams.yaml")
    )
    if not research_teams_path.is_absolute():
        research_teams_path = (REPO_ROOT / research_teams_path).resolve()

    return Settings(
        kalshi_api_key=os.environ.get("KALSHI_API_KEY", ""),
        kalshi_private_key_path=pk_path,
        kalshi_api_base=os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com"),
        game_id=gid,
        sport=sport,
        deliver_to=os.environ.get("NBABOT_DELIVER_TO", "stdout"),
        dry_run=os.environ.get("NBABOT_DRY_RUN", "1") not in ("0", "false", "False", ""),
        data_dir=data_dir,
        research_db_path=research_db_path,
        calibration_overrides_path=calibration_overrides_path,
        execution_mode=os.environ.get("NBABOT_EXECUTION_MODE", "paper").lower(),
        live_trading_ack=os.environ.get("NBABOT_LIVE_TRADING_ACK", ""),
        broad_slate_execution=os.environ.get("NBABOT_BROAD_SLATE_EXECUTION", ""),
        research_override_ack=os.environ.get("NBABOT_RESEARCH_OVERRIDE_ACK", ""),
        research_override_max_units=min(
            float(os.environ.get("NBABOT_RESEARCH_OVERRIDE_MAX_UNITS", "1")),
            1.0,
        ),
        demo_api_base=os.environ.get(
            "NBABOT_DEMO_API_BASE",
            "https://external-api.demo.kalshi.co/trade-api/v2",
        ).rstrip("/"),
        kalshi_demo_api_key=os.environ.get("KALSHI_DEMO_API_KEY", ""),
        kalshi_demo_private_key_path=demo_pk_path,
        paper_demo_daily_trade_cap=int(os.environ.get("NBABOT_PAPER_DEMO_DAILY_TRADE_CAP", "50")),
        qual_daily_trade_cap=int(os.environ.get("NBABOT_QUAL_DAILY_TRADE_CAP", "10")),
        unit_fraction=float(os.environ.get("NBABOT_UNIT_FRACTION", "0.015")),
        paper_bankroll_usd=float(os.environ.get("NBABOT_PAPER_BANKROLL", "1000")),
        max_order_notional_fraction=float(
            os.environ.get("NBABOT_MAX_ORDER_NOTIONAL_FRACTION", "0.10")
        ),
        resting_order_max_age_minutes=int(
            os.environ.get("NBABOT_RESTING_ORDER_MAX_AGE_MINUTES", "90")
        ),
        max_daily_loss_units=float(os.environ.get("NBABOT_MAX_DAILY_LOSS_UNITS", "2")),
        max_daily_exposure_units=float(
            os.environ.get("NBABOT_MAX_DAILY_EXPOSURE_UNITS", str(MAX_STAKE_UNITS))
        ),
        max_game_exposure_units=float(
            os.environ.get("NBABOT_MAX_GAME_EXPOSURE_UNITS", str(MAX_STAKE_UNITS))
        ),
        min_edge=float(os.environ.get("NBABOT_MIN_EDGE", "0.05")),
        demo_min_edge=float(os.environ.get("NBABOT_DEMO_MIN_EDGE", "0.03")),
        qual_min_edge=float(os.environ.get("NBABOT_QUAL_MIN_EDGE", "0.06")),
        kalshi_taker_fee_factor=float(os.environ.get("NBABOT_KALSHI_TAKER_FEE_FACTOR", "0.07")),
        kalshi_maker_fee_factor=float(os.environ.get("NBABOT_KALSHI_MAKER_FEE_FACTOR", "0.0175")),
        near_miss_window=float(os.environ.get("NBABOT_NEAR_MISS_WINDOW", "0.02")),
        near_miss_investigations_per_cycle=int(
            os.environ.get("NBABOT_NEAR_MISS_INVESTIGATIONS_PER_CYCLE", "5")
        ),
        qaq_floor_bonus=float(os.environ.get("NBABOT_QAQ_FLOOR_BONUS", "0.015")),
        confluence_agree_delta=float(os.environ.get("NBABOT_CONFLUENCE_AGREE_DELTA", "0.05")),
        confluence_edge_bonus=float(os.environ.get("NBABOT_CONFLUENCE_EDGE_BONUS", "0.01")),
        confluence_veto_delta=float(os.environ.get("NBABOT_CONFLUENCE_VETO_DELTA", "0.08")),
        qual_signal_max_age_hours=float(os.environ.get("NBABOT_QUAL_SIGNAL_MAX_AGE_HOURS", "12")),
        qual_lessons_top_n=int(os.environ.get("NBABOT_QUAL_LESSONS_TOP_N", "5")),
        qual_llm_cmd=os.environ.get(
            "NBABOT_QUAL_LLM_CMD",
            "~/.codex/plugins/.plugin-appserver/codex exec",
        ),
        qual_llm_timeout_seconds=int(os.environ.get("NBABOT_QUAL_LLM_TIMEOUT_SECONDS", "600")),
        qual_fallback_model=os.environ.get("NBABOT_QUAL_FALLBACK_MODEL", "claude-opus-4-8"),
        research_teams_path=research_teams_path,
        news_window_hours=float(os.environ.get("NBABOT_NEWS_WINDOW_HOURS", "48")),
        news_user_agent=os.environ.get("NBABOT_NEWS_USER_AGENT", "nbabot-research/0.1"),
        event_trigger_cooldown_minutes=int(
            os.environ.get("NBABOT_EVENT_TRIGGER_COOLDOWN_MINUTES", "45")
        ),
        event_trigger_daily_cap=int(os.environ.get("NBABOT_EVENT_TRIGGER_DAILY_CAP", "8")),
        max_plausible_edge=float(os.environ.get("NBABOT_MAX_PLAUSIBLE_EDGE", "0.15")),
        stale_market_seconds=int(os.environ.get("NBABOT_STALE_MARKET_SECONDS", "90")),
        max_spread_cents=int(os.environ.get("NBABOT_MAX_SPREAD_CENTS", "10")),
        orderbook_depth=(
            int(os.environ["NBABOT_ORDERBOOK_DEPTH"])
            if os.environ.get("NBABOT_ORDERBOOK_DEPTH") else None
        ),
        book_watch_iterations=int(os.environ.get("NBABOT_BOOK_WATCH_ITERATIONS", "1")),
        book_watch_interval_seconds=float(os.environ.get("NBABOT_BOOK_WATCH_INTERVAL_SECONDS", "0")),
        closing_snapshot_window_minutes=int(os.environ.get("NBABOT_CLOSING_SNAPSHOT_WINDOW_MINUTES", "240")),
        concentration_max_winner_share=float(os.environ.get("NBABOT_CONCENTRATION_MAX_WINNER_SHARE", "0.50")),
        kill_switch_path=kill_switch_path,
        ui_host=os.environ.get("NBABOT_UI_HOST", "127.0.0.1"),
        ui_port=int(os.environ.get("NBABOT_UI_PORT", "8765")),
        game=game,
        scenarios_doc=scen,
    )
