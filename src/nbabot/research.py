"""SQLite mirror for research, backtests, snapshots, and execution ledgers."""
from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .qual_learning import normalize_lesson, qual_calibration_stats
from .qual_rag import (
    EMBEDDING_DIM,
    EMBEDDING_VERSION,
    MAX_CHUNK_CHARS,
    chunk_text_by_paragraphs,
    embedding,
    text_hash,
)

ORDER_TABLES = {"paper_orders", "demo_orders", "live_orders"}
DEFAULT_RISK_TIMEZONE = "America/New_York"
OPEN_EXPOSURE_STATUSES = {"resting", "open", "pending", "accepted", "created"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_json(obj: Any) -> str:
    if is_dataclass(obj):
        obj = asdict(obj)
    return json.dumps(obj, sort_keys=True, default=str)


class ResearchStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS games (
                    game_id TEXT PRIMARY KEY,
                    game_tag TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scenario_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    prior_p_joint REAL,
                    hit INTEGER,
                    resolved_legs INTEGER,
                    total_legs INTEGER,
                    notes TEXT
                );
                CREATE TABLE IF NOT EXISTS leg_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    scenario_id TEXT,
                    market TEXT NOT NULL,
                    line REAL,
                    prior_p REAL,
                    entry_implied_p REAL,
                    outcome INTEGER,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    scenario_id TEXT,
                    market TEXT NOT NULL,
                    ticker TEXT,
                    bid INTEGER,
                    ask INTEGER,
                    mid INTEGER,
                    implied REAL,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_snapshots_game_time
                    ON market_snapshots(game_id, captured_at DESC);
                CREATE TABLE IF NOT EXISTS market_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    game_tag TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    series TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    title TEXT,
                    player TEXT,
                    stat TEXT,
                    line REAL,
                    team TEXT,
                    bid INTEGER,
                    ask INTEGER,
                    mid INTEGER,
                    implied REAL,
                    mapping_status TEXT NOT NULL,
                    mapped_markets_json TEXT NOT NULL,
                    mapped_scenarios_json TEXT NOT NULL,
                    row_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_catalog_game_time
                    ON market_catalog(game_id, captured_at DESC);
                CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    yes_bids_json TEXT,
                    yes_asks_json TEXT,
                    no_bids_json TEXT,
                    metrics_json TEXT,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_game_time
                    ON orderbook_snapshots(game_id, captured_at DESC);
                CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_game_ticker_time
                    ON orderbook_snapshots(game_id, ticker, captured_at DESC);
                CREATE TABLE IF NOT EXISTS edge_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    model_prob REAL NOT NULL,
                    market_prob REAL,
                    edge REAL,
                    confidence TEXT,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_edge_history_game
                    ON edge_history(game_id, captured_at DESC);
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_scenario_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    prior_p_joint REAL,
                    hit INTEGER,
                    simulated_pnl REAL,
                    row_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_edge_runs (
                    run_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    api_cost_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_edge_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    sport TEXT,
                    market_family TEXT,
                    decision_time TEXT,
                    model_prob REAL,
                    kalshi_price_prob REAL,
                    edge REAL,
                    outcome INTEGER,
                    row_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_backtest_edge_records_run
                    ON backtest_edge_records(run_id);
                CREATE INDEX IF NOT EXISTS idx_backtest_edge_records_ticker_time
                    ON backtest_edge_records(ticker, decision_time DESC);
                CREATE TABLE IF NOT EXISTS trade_intents (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    signal_source TEXT NOT NULL DEFAULT 'consensus',
                    confluence_verdict TEXT NOT NULL DEFAULT 'none',
                    intent_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_decisions (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    decision_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    signal_source TEXT NOT NULL DEFAULT 'consensus',
                    confluence_verdict TEXT NOT NULL DEFAULT 'none',
                    intent_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_orders (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    signal_source TEXT NOT NULL DEFAULT 'consensus',
                    confluence_verdict TEXT NOT NULL DEFAULT 'none',
                    intent_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_orders (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    signal_source TEXT NOT NULL DEFAULT 'consensus',
                    confluence_verdict TEXT NOT NULL DEFAULT 'none',
                    intent_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    filled_at TEXT NOT NULL,
                    fill_key TEXT,
                    fill_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_lifecycle (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    filled_count REAL NOT NULL DEFAULT 0,
                    canceled_at TEXT,
                    cancel_reason TEXT,
                    checked_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_lifecycle_mode_status
                    ON order_lifecycle(mode, status);
                CREATE TABLE IF NOT EXISTS settlement_records (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    contracts REAL NOT NULL,
                    entry_price_cents REAL NOT NULL,
                    audited_at TEXT NOT NULL,
                    settled_at TEXT,
                    market_status TEXT,
                    winning_side TEXT,
                    outcome INTEGER NOT NULL,
                    payout_cents REAL,
                    pnl_cents REAL,
                    entry_model_prob REAL,
                    entry_market_prob REAL,
                    closing_consensus_prob REAL,
                    closing_consensus_cents REAL,
                    clv_cents REAL,
                    beat_closing_consensus INTEGER,
                    brier_entry_model REAL,
                    signal_source TEXT NOT NULL DEFAULT 'consensus',
                    confluence_verdict TEXT NOT NULL DEFAULT 'none',
                    market_json TEXT NOT NULL,
                    consensus_json TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_settlement_records_game_time
                    ON settlement_records(game_id, audited_at DESC);
                CREATE TABLE IF NOT EXISTS closing_snapshots (
                    ticker TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    market_close_time TEXT,
                    seconds_to_close REAL,
                    source TEXT NOT NULL,
                    consensus_prob REAL,
                    consensus_cents REAL,
                    kalshi_mid_cents REAL,
                    executable_cents REAL,
                    book_count INTEGER,
                    sources_json TEXT NOT NULL,
                    market_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_closing_snapshots_game_time
                    ON closing_snapshots(game_id, captured_at DESC);
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    scenario_id TEXT,
                    side TEXT NOT NULL,
                    contracts REAL NOT NULL,
                    entry_price_cents INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    current_pnl REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    position_id TEXT,
                    client_order_id TEXT,
                    game_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL,
                    side TEXT NOT NULL,
                    contracts REAL NOT NULL,
                    price_cents INTEGER NOT NULL,
                    fill_status TEXT,
                    response_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    daily_pnl_units REAL,
                    game_exposure_units REAL,
                    open_positions INTEGER,
                    circuit_breaker_on INTEGER DEFAULT 0,
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    game_id TEXT,
                    created_at TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    game_id TEXT,
                    created_at TEXT NOT NULL,
                    error TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS news_items (
                    id TEXT PRIMARY KEY,
                    team TEXT NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT,
                    body TEXT,
                    url TEXT,
                    published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_news_items_url
                    ON news_items(url) WHERE url IS NOT NULL AND url != '';
                CREATE INDEX IF NOT EXISTS idx_news_items_team_published
                    ON news_items(team, published_at DESC);
                CREATE TABLE IF NOT EXISTS qual_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    base_rate REAL,
                    qual_prob REAL NOT NULL,
                    confidence REAL NOT NULL,
                    rationale TEXT,
                    citations_json TEXT NOT NULL,
                    scenarios_json TEXT,
                    analysis_json TEXT,
                    news_item_ids_json TEXT,
                    created_at TEXT NOT NULL,
                    model_run_id TEXT NOT NULL,
                    prompt_version TEXT,
                    engine TEXT NOT NULL DEFAULT 'codex',
                    signal_source TEXT NOT NULL DEFAULT 'qual',
                    UNIQUE(ticker, model_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_qual_signals_ticker_time
                    ON qual_signals(ticker, created_at DESC);
                CREATE TABLE IF NOT EXISTS market_baskets (
                    basket_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    team TEXT,
                    reason TEXT,
                    event_ids_json TEXT NOT NULL,
                    news_item_ids_json TEXT NOT NULL,
                    tickers_json TEXT NOT NULL,
                    basket_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_baskets_game_time
                    ON market_baskets(game_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS near_miss_investigations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    candidate_id TEXT,
                    created_at TEXT NOT NULL,
                    model_run_id TEXT,
                    engine TEXT NOT NULL DEFAULT 'codex',
                    justified INTEGER NOT NULL DEFAULT 0,
                    direction_consistent INTEGER NOT NULL DEFAULT 0,
                    confidence REAL,
                    gap REAL,
                    citation_urls_json TEXT NOT NULL,
                    news_item_ids_json TEXT NOT NULL,
                    verdict_json TEXT NOT NULL,
                    UNIQUE(game_id, ticker, model_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_near_miss_game_time
                    ON near_miss_investigations(game_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS qual_recaps (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    signal_id INTEGER,
                    recap_status TEXT NOT NULL,
                    news_item_id TEXT,
                    team TEXT,
                    source TEXT,
                    recap_date TEXT,
                    fetched_at TEXT NOT NULL,
                    reason TEXT,
                    source_status_json TEXT NOT NULL,
                    settlement_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_qual_recaps_game
                    ON qual_recaps(game_id, fetched_at DESC);
                CREATE TABLE IF NOT EXISTS qual_postmortems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL UNIQUE,
                    game_id TEXT NOT NULL,
                    signal_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    model_run_id TEXT,
                    recap_news_item_id TEXT,
                    outcome INTEGER NOT NULL,
                    postmortem_json TEXT NOT NULL,
                    lessons_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_qual_postmortems_game
                    ON qual_postmortems(game_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS qual_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team TEXT NOT NULL,
                    market_family TEXT NOT NULL,
                    lesson_norm TEXT NOT NULL,
                    lesson_text TEXT NOT NULL,
                    evidence_cite TEXT,
                    hit_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    source_postmortem_id INTEGER,
                    UNIQUE(team, market_family, lesson_norm)
                );
                CREATE INDEX IF NOT EXISTS idx_qual_lessons_lookup
                    ON qual_lessons(team, market_family, hit_count DESC, last_seen_at DESC);
                CREATE TABLE IF NOT EXISTS qual_rag_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_row_id TEXT NOT NULL,
                    source_timestamp TEXT,
                    team TEXT,
                    teams_json TEXT NOT NULL,
                    ticker TEXT,
                    market_family TEXT,
                    title TEXT,
                    text TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id, text_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_qual_rag_chunks_source_time
                    ON qual_rag_chunks(source_type, source_timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_qual_rag_chunks_ticker_time
                    ON qual_rag_chunks(ticker, source_timestamp DESC);
                CREATE TABLE IF NOT EXISTS qual_rag_embeddings (
                    chunk_id TEXT PRIMARY KEY,
                    embedding_version TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    embedded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qual_signal_retrievals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    close_time TEXT,
                    query_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    no_lookahead_checked INTEGER NOT NULL DEFAULT 1,
                    leaked_context_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(model_run_id, ticker)
                );
                CREATE TABLE IF NOT EXISTS qual_groundedness_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    groundedness REAL NOT NULL,
                    faithfulness REAL NOT NULL,
                    context_relevance REAL NOT NULL,
                    unsupported_branch_rate REAL NOT NULL,
                    branch_count INTEGER NOT NULL,
                    supported_branch_count INTEGER NOT NULL,
                    tradeable INTEGER NOT NULL,
                    blocked_reason TEXT,
                    score_json TEXT NOT NULL,
                    UNIQUE(model_run_id, ticker)
                );
                CREATE INDEX IF NOT EXISTS idx_qual_groundedness_time
                    ON qual_groundedness_scores(created_at DESC);
                CREATE TABLE IF NOT EXISTS confluence_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    candidate_id TEXT,
                    created_at TEXT NOT NULL,
                    consensus_fair_prob REAL,
                    qual_prob REAL,
                    qual_confidence REAL,
                    delta REAL,
                    verdict TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_confluence_records_game_time
                    ON confluence_records(game_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS shadow_trade_intents (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    signal_source TEXT NOT NULL DEFAULT 'consensus',
                    confluence_verdict TEXT NOT NULL DEFAULT 'none',
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    contracts REAL NOT NULL,
                    price_cents REAL NOT NULL,
                    stake_units REAL NOT NULL,
                    edge REAL,
                    reason TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    audited_at TEXT,
                    settled_at TEXT,
                    market_status TEXT,
                    winning_side TEXT,
                    outcome INTEGER,
                    pnl_cents REAL,
                    market_json TEXT,
                    settlement_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_trade_intents_game_time
                    ON shadow_trade_intents(game_id, created_at DESC);
                """
            )
            self._ensure_column(db, "orderbook_snapshots", "no_bids_json", "TEXT")
            self._ensure_column(db, "orderbook_snapshots", "metrics_json", "TEXT")
            self._ensure_column(db, "trade_intents", "signal_source", "TEXT NOT NULL DEFAULT 'consensus'")
            self._ensure_column(db, "paper_orders", "signal_source", "TEXT NOT NULL DEFAULT 'consensus'")
            self._ensure_column(db, "demo_orders", "signal_source", "TEXT NOT NULL DEFAULT 'consensus'")
            self._ensure_column(db, "live_orders", "signal_source", "TEXT NOT NULL DEFAULT 'consensus'")
            self._ensure_column(db, "settlement_records", "signal_source", "TEXT NOT NULL DEFAULT 'consensus'")
            self._ensure_column(db, "trade_intents", "confluence_verdict", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(db, "paper_orders", "confluence_verdict", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(db, "demo_orders", "confluence_verdict", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(db, "live_orders", "confluence_verdict", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(db, "settlement_records", "confluence_verdict", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(db, "settlement_records", "clv_midpoint_cents", "REAL")
            self._ensure_column(db, "settlement_records", "closing_kalshi_mid_cents", "REAL")
            self._ensure_column(db, "settlement_records", "closing_executable_cents", "REAL")
            self._ensure_column(db, "settlement_records", "clv_unmeasured_reason", "TEXT")
            self._ensure_column(db, "fills", "fill_key", "TEXT")
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_order_key
                    ON fills(client_order_id, fill_key)
                    WHERE fill_key IS NOT NULL AND fill_key != ''
                """
            )
            self._ensure_column(db, "qual_signals", "base_rate", "REAL")
            self._ensure_column(db, "qual_signals", "scenarios_json", "TEXT")
            self._ensure_column(db, "qual_signals", "analysis_json", "TEXT")
            self._ensure_column(db, "qual_signals", "news_item_ids_json", "TEXT")
            self._ensure_column(db, "qual_signals", "prompt_version", "TEXT")
            self._ensure_column(db, "qual_signals", "engine", "TEXT NOT NULL DEFAULT 'codex'")
            self._ensure_column(db, "qual_signals", "groundedness", "REAL")
            self._ensure_column(db, "qual_signals", "tradeable", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(db, "qual_signals", "blocked_reason", "TEXT")

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str,
                       ddl: str) -> None:
        cols = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def upsert_game(self, game_id: str, game_tag: str) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO games(game_id, game_tag, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    game_tag=excluded.game_tag,
                    updated_at=excluded.updated_at
                """,
                (game_id, game_tag, utc_now()),
            )

    def insert_market_snapshots(self, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        rows = list(rows)
        if not rows:
            return 0
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO market_snapshots(
                    game_id, captured_at, scenario_id, market, ticker, bid, ask,
                    mid, implied, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["game_id"], r["captured_at"], r.get("scenario_id"),
                        r["market"], r.get("ticker"), r.get("bid"), r.get("ask"),
                        r.get("mid"), r.get("implied"), r.get("source", "kalshi"),
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def record_market_catalog(self, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        rows = list(rows)
        if not rows:
            return 0
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO market_catalog(
                    game_id, game_tag, captured_at, series, ticker, title, player,
                    stat, line, team, bid, ask, mid, implied, mapping_status,
                    mapped_markets_json, mapped_scenarios_json, row_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["game_id"], r["game_tag"], r["captured_at"],
                        r.get("series") or "", r.get("ticker") or "", r.get("title"),
                        r.get("player"), r.get("stat"), r.get("line"), r.get("team"),
                        r.get("bid"), r.get("ask"), r.get("mid"), r.get("implied"),
                        r.get("mapping_status", "unmatched"),
                        to_json(r.get("mapped_markets", [])),
                        to_json(r.get("mapped_scenarios", [])),
                        to_json(r),
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def record_orderbook_snapshots(self, game_id: str, books: Iterable[Any],
                                   metrics: dict[str, dict[str, Any]] | None = None) -> int:
        self.init_schema()
        rows = list(books)
        if not rows:
            return 0
        metrics = metrics or {}
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO orderbook_snapshots(
                    game_id, captured_at, ticker, yes_bids_json, yes_asks_json,
                    no_bids_json, metrics_json, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        game_id,
                        book.captured_at,
                        book.ticker,
                        to_json(book.bids("yes")),
                        to_json(book.asks("yes")),
                        to_json(book.bids("no")),
                        to_json(metrics.get(book.ticker, {})),
                        getattr(book, "source", "kalshi"),
                    )
                    for book in rows
                ],
            )
        return len(rows)

    def record_backtest(self, run_id: str, game_id: str, metrics: dict[str, Any]) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO backtest_runs(run_id, game_id, run_at, metrics_json)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, game_id, utc_now(), to_json(metrics)),
            )
            rows = metrics.get("scenario_rows", [])
            db.executemany(
                """
                INSERT INTO backtest_scenario_rows(
                    run_id, game_id, scenario_id, prior_p_joint, hit, simulated_pnl, row_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id, game_id, r["scenario_id"], r.get("prior_p_joint"),
                        r.get("hit"), r.get("simulated_pnl"), to_json(r),
                    )
                    for r in rows
                ],
            )

    def record_backtest_edge_run(self, run_id: str, game_id: str,
                                 payload: dict[str, Any]) -> None:
        """Persist historical edge backtest output outside real settlements."""
        self.init_schema()
        records = list(payload.get("records") or [])
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO backtest_edge_runs(
                    run_id, game_id, run_at, summary_json, api_cost_json, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    game_id,
                    utc_now(),
                    to_json(payload.get("summary", {})),
                    to_json(payload.get("api_cost", {})),
                    to_json(payload),
                ),
            )
            db.execute("DELETE FROM backtest_edge_records WHERE run_id = ?", (run_id,))
            db.executemany(
                """
                INSERT INTO backtest_edge_records(
                    run_id, game_id, ticker, sport, market_family, decision_time,
                    model_prob, kalshi_price_prob, edge, outcome, row_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        game_id,
                        r.get("ticker") or "",
                        r.get("sport"),
                        r.get("market_family"),
                        r.get("decision_time"),
                        r.get("model_prob"),
                        r.get("kalshi_price_prob"),
                        r.get("edge"),
                        r.get("outcome"),
                        to_json(r),
                    )
                    for r in records
                    if r.get("ticker")
                ],
            )

    def record_news_items(self, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        rows = list(rows)
        if not rows:
            return 0
        with self.connect() as db:
            cur = db.executemany(
                """
                INSERT OR IGNORE INTO news_items(
                    id, team, source, title, body, url, published_at, fetched_at,
                    content_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["id"],
                        r["team"],
                        r["source"],
                        r.get("title"),
                        r.get("body"),
                        r.get("url"),
                        r.get("published_at"),
                        r.get("fetched_at") or utc_now(),
                        r["content_hash"],
                    )
                    for r in rows
                ],
            )
        return cur.rowcount if cur.rowcount is not None else 0

    def existing_news_content_hashes(self, hashes: Iterable[str]) -> set[str]:
        self.init_schema()
        values = [str(value) for value in hashes if str(value)]
        if not values:
            return set()
        placeholders = ",".join("?" for _ in values)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT content_hash
                FROM news_items
                WHERE content_hash IN ({placeholders})
                """,
                tuple(values),
            ).fetchall()
        return {str(row["content_hash"]) for row in rows}

    def recent_news_items(
        self,
        teams: Iterable[str],
        *,
        window_hours: float = 48,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        team_list = [str(team) for team in teams if str(team)]
        if not team_list:
            return []
        placeholders = ",".join("?" for _ in team_list)
        with self.connect() as db:
            latest = db.execute(
                f"""
                SELECT MAX(fetched_at) AS fetched_at
                FROM news_items
                WHERE team IN ({placeholders})
                """,
                tuple(team_list),
            ).fetchone()
            latest_fetch = latest["fetched_at"] if latest else None
            try:
                anchor = datetime.fromisoformat(str(latest_fetch).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                anchor = datetime.now(timezone.utc)
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
            cutoff = (anchor - timedelta(hours=float(window_hours))).isoformat()
            rows = db.execute(
                f"""
                SELECT *
                FROM news_items
                WHERE team IN ({placeholders})
                  AND (published_at IS NULL OR published_at >= ? OR fetched_at >= ?)
                ORDER BY published_at DESC, fetched_at DESC
                """,
                (*team_list, cutoff, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def news_item_by_id(self, item_id: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM news_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def record_qual_signals(self, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        rows = list(rows)
        if not rows:
            return 0
        with self.connect() as db:
            cur = db.executemany(
                """
                INSERT OR IGNORE INTO qual_signals(
                    ticker, base_rate, qual_prob, confidence, rationale, citations_json,
                    scenarios_json, analysis_json, news_item_ids_json,
                    created_at, model_run_id, prompt_version, engine, signal_source,
                    groundedness, tradeable, blocked_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["ticker"],
                        r.get("base_rate"),
                        r["qual_prob"],
                        r["confidence"],
                        r.get("rationale"),
                        to_json(r.get("citation_urls") or r.get("citations") or []),
                        to_json(r.get("scenarios") or []),
                        to_json(r.get("analysis") or {
                            "ticker": r.get("ticker"),
                            "base_rate": r.get("base_rate"),
                            "qual_prob": r.get("qual_prob"),
                            "confidence": r.get("confidence"),
                            "rationale": r.get("rationale"),
                            "citation_urls": r.get("citation_urls") or r.get("citations") or [],
                            "news_item_ids_used": r.get("news_item_ids_used") or [],
                            "scenarios": r.get("scenarios") or [],
                            "model_run_id": r.get("model_run_id"),
                            "prompt_version": r.get("prompt_version"),
                            "engine": r.get("engine") or "codex",
                            "created_at": r.get("created_at"),
                        }),
                        to_json(r.get("news_item_ids_used") or []),
                        r.get("created_at") or utc_now(),
                        r["model_run_id"],
                        r.get("prompt_version"),
                        str(r.get("engine") or "codex"),
                        str(r.get("signal_source") or "qual"),
                        r.get("groundedness"),
                        int(bool(r.get("tradeable", True))),
                        r.get("blocked_reason"),
                    )
                    for r in rows
                ],
            )
        return cur.rowcount if cur.rowcount is not None else 0

    def record_market_basket(self, game_id: str, basket: dict[str, Any]) -> bool:
        self.init_schema()
        basket_id = str(basket.get("basket_id") or "")
        if not basket_id:
            return False
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR REPLACE INTO market_baskets(
                    basket_id, game_id, created_at, team, reason,
                    event_ids_json, news_item_ids_json, tickers_json, basket_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    basket_id,
                    game_id,
                    basket.get("created_at") or utc_now(),
                    basket.get("team"),
                    basket.get("reason"),
                    to_json(basket.get("event_ids") or []),
                    to_json(basket.get("news_item_ids") or []),
                    to_json(basket.get("tickers") or []),
                    to_json(basket),
                ),
            )
        return cur.rowcount is not None and cur.rowcount > 0

    def record_near_miss_investigations(self, game_id: str, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        records = [row for row in rows if isinstance(row, dict) and row.get("ticker")]
        if not records:
            return 0
        with self.connect() as db:
            cur = db.executemany(
                """
                INSERT OR IGNORE INTO near_miss_investigations(
                    game_id, ticker, candidate_id, created_at, model_run_id,
                    engine, justified, direction_consistent, confidence, gap,
                    citation_urls_json, news_item_ids_json, verdict_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        game_id,
                        str(row.get("ticker")),
                        row.get("candidate_id"),
                        row.get("created_at") or utc_now(),
                        row.get("model_run_id"),
                        str(row.get("engine") or "codex"),
                        int(bool(row.get("justified"))),
                        int(bool(row.get("direction_consistent"))),
                        row.get("confidence"),
                        row.get("gap"),
                        to_json(row.get("citation_urls") or []),
                        to_json(row.get("news_item_ids") or []),
                        to_json(row),
                    )
                    for row in records
                ],
            )
        return cur.rowcount if cur.rowcount is not None else 0

    def latest_qual_signals(
        self,
        *,
        max_age_hours: float = 12,
        tickers: Iterable[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        self.init_schema()
        ticker_list = [str(ticker) for ticker in (tickers or []) if str(ticker)]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=float(max_age_hours))).isoformat()
        where = "created_at >= ?"
        params: list[Any] = [cutoff]
        if ticker_list:
            placeholders = ",".join("?" for _ in ticker_list)
            where += f" AND ticker IN ({placeholders})"
            params.extend(ticker_list)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT *
                FROM qual_signals
                WHERE {where}
                ORDER BY created_at DESC, confidence DESC
                """,
                tuple(params),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            ticker = row["ticker"]
            if ticker in latest:
                continue
            item = dict(row)
            try:
                item["citation_urls"] = json.loads(item.pop("citations_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["citation_urls"] = []
            try:
                item["scenarios"] = json.loads(item.pop("scenarios_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["scenarios"] = []
            try:
                item["analysis"] = json.loads(item.pop("analysis_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["analysis"] = {}
            try:
                item["news_item_ids_used"] = json.loads(item.pop("news_item_ids_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["news_item_ids_used"] = []
            latest[ticker] = item
        return latest

    @staticmethod
    def _decode_qual_signal_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for source_key, target_key, default in (
            ("citations_json", "citation_urls", []),
            ("scenarios_json", "scenarios", []),
            ("analysis_json", "analysis", {}),
            ("news_item_ids_json", "news_item_ids_used", []),
        ):
            raw = item.pop(source_key, None)
            try:
                item[target_key] = json.loads(raw or to_json(default))
            except (TypeError, ValueError, json.JSONDecodeError):
                item[target_key] = default
        return item

    def latest_qual_signal_for_ticker(
        self,
        ticker: str,
        *,
        at_or_before: str | None = None,
    ) -> dict[str, Any] | None:
        self.init_schema()
        params: list[Any] = [ticker]
        where = "ticker = ?"
        if at_or_before:
            where += " AND created_at <= ?"
            params.append(at_or_before)
        with self.connect() as db:
            row = db.execute(
                f"""
                SELECT *
                FROM qual_signals
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        if row is None and at_or_before:
            return self.latest_qual_signal_for_ticker(ticker)
        return None if row is None else self._decode_qual_signal_row(row)

    def pending_qual_postmortem_settlements(self, limit: int | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        query = """
            SELECT sr.*
            FROM settlement_records sr
            LEFT JOIN qual_postmortems qp
              ON qp.client_order_id = sr.client_order_id
            WHERE qp.client_order_id IS NULL
              AND (
                sr.signal_source = 'qual'
                OR sr.signal_source = 'qual_activated_quant'
                OR (sr.confluence_verdict IS NOT NULL AND sr.confluence_verdict != 'none')
              )
            ORDER BY sr.audited_at ASC
        """
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (int(limit),)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_qual_recap(self, record: dict[str, Any]) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO qual_recaps(
                    client_order_id, game_id, signal_id, recap_status, news_item_id,
                    team, source, recap_date, fetched_at, reason,
                    source_status_json, settlement_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["client_order_id"],
                    record["game_id"],
                    record.get("signal_id"),
                    record["recap_status"],
                    record.get("news_item_id"),
                    record.get("team"),
                    record.get("source"),
                    record.get("recap_date"),
                    record.get("fetched_at") or utc_now(),
                    record.get("reason"),
                    to_json(record.get("source_status") or []),
                    to_json(record.get("settlement") or {}),
                ),
            )

    def qual_recap_for_settlement(self, client_order_id: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM qual_recaps WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["source_status"] = json.loads(item.pop("source_status_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["source_status"] = []
        try:
            item["settlement"] = json.loads(item.pop("settlement_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["settlement"] = {}
        return item

    def record_qual_postmortem(self, row: dict[str, Any]) -> int | None:
        self.init_schema()
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO qual_postmortems(
                    client_order_id, game_id, signal_id, ticker, created_at,
                    model_run_id, recap_news_item_id, outcome,
                    postmortem_json, lessons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["client_order_id"],
                    row["game_id"],
                    row["signal_id"],
                    row["ticker"],
                    row.get("created_at") or utc_now(),
                    row.get("model_run_id"),
                    row.get("recap_news_item_id"),
                    int(row["outcome"]),
                    to_json(row.get("postmortem") or {}),
                    to_json(row.get("lessons") or []),
                ),
            )
            if cur.rowcount != 1:
                existing = db.execute(
                    "SELECT id FROM qual_postmortems WHERE client_order_id = ?",
                    (row["client_order_id"],),
                ).fetchone()
                return int(existing["id"]) if existing else None
            return int(cur.lastrowid)

    def upsert_qual_lesson(
        self,
        *,
        team: str,
        market_family: str,
        lesson_text: str,
        evidence_cite: str,
        postmortem_id: int | None,
    ) -> bool:
        self.init_schema()
        norm = normalize_lesson(lesson_text)
        if not norm:
            return False
        now = utc_now()
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT INTO qual_lessons(
                    team, market_family, lesson_norm, lesson_text, evidence_cite,
                    hit_count, first_seen_at, last_seen_at, source_postmortem_id
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(team, market_family, lesson_norm) DO UPDATE SET
                    hit_count = hit_count + 1,
                    lesson_text = excluded.lesson_text,
                    evidence_cite = excluded.evidence_cite,
                    last_seen_at = excluded.last_seen_at,
                    source_postmortem_id = excluded.source_postmortem_id
                """,
                (
                    team,
                    market_family,
                    norm,
                    lesson_text[:300],
                    evidence_cite[:400],
                    now,
                    now,
                    postmortem_id,
                ),
            )
        return cur.rowcount is not None and cur.rowcount > 0

    def top_qual_lessons(
        self,
        *,
        teams: Iterable[str],
        market_family: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        team_list = [str(team) for team in teams if str(team)]
        if not team_list:
            return []
        placeholders = ",".join("?" for _ in team_list)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT *
                FROM qual_lessons
                WHERE team IN ({placeholders})
                  AND market_family = ?
                ORDER BY hit_count DESC, last_seen_at DESC
                LIMIT ?
                """,
                (*team_list, market_family, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def qual_calibration_rows(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connect() as db:
            settlements = db.execute(
                """
                SELECT *
                FROM settlement_records
                WHERE signal_source IN ('qual', 'qual_activated_quant')
                   OR (confluence_verdict IS NOT NULL AND confluence_verdict != 'none')
                ORDER BY audited_at ASC
                """
            ).fetchall()
        rows = []
        signal_cache: dict[str, dict[str, Any] | None] = {}
        for settlement in settlements:
            record = dict(settlement)
            ticker = str(record.get("ticker") or "")
            if ticker not in signal_cache:
                signal_cache[ticker] = self.latest_qual_signal_for_ticker(
                    ticker,
                    at_or_before=record.get("audited_at"),
                )
            signal = signal_cache[ticker] or {}
            rows.append({
                "ticker": ticker,
                "confidence": signal.get("confidence"),
                "prob": record.get("entry_model_prob"),
                "outcome": record.get("outcome"),
                "client_order_id": record.get("client_order_id"),
            })
        return rows

    def qual_calibration_table(self) -> list[dict[str, Any]]:
        return qual_calibration_stats(self.qual_calibration_rows())

    def qual_learning_summary(self) -> dict[str, Any]:
        self.init_schema()
        with self.connect() as db:
            postmortems = int(db.execute("SELECT COUNT(*) FROM qual_postmortems").fetchone()[0])
            lessons = int(db.execute("SELECT COUNT(*) FROM qual_lessons").fetchone()[0])
            recap_found = int(
                db.execute("SELECT COUNT(*) FROM qual_recaps WHERE recap_status = 'found'").fetchone()[0]
            )
            recap_missing = int(
                db.execute("SELECT COUNT(*) FROM qual_recaps WHERE recap_status = 'missing'").fetchone()[0]
            )
        return {
            "postmortems_completed": postmortems,
            "lessons_stored": lessons,
            "recaps_found": recap_found,
            "recaps_missing": recap_missing,
            "calibration": self.qual_calibration_table(),
        }

    def _rag_upsert_chunks(self, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        records = [row for row in rows if str(row.get("text") or "").strip()]
        if not records:
            return 0
        now = utc_now()
        with self.connect() as db:
            cur = db.executemany(
                """
                INSERT INTO qual_rag_chunks(
                    chunk_id, source_type, source_id, source_row_id,
                    source_timestamp, team, teams_json, ticker, market_family,
                    title, text, text_hash, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_id, text_hash) DO UPDATE SET
                    source_timestamp=excluded.source_timestamp,
                    team=excluded.team,
                    teams_json=excluded.teams_json,
                    ticker=excluded.ticker,
                    market_family=excluded.market_family,
                    title=excluded.title,
                    text=excluded.text,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        row["chunk_id"],
                        row["source_type"],
                        row["source_id"],
                        row["source_row_id"],
                        row.get("source_timestamp"),
                        row.get("team"),
                        to_json(row.get("teams") or ([] if not row.get("team") else [row.get("team")])),
                        row.get("ticker"),
                        row.get("market_family"),
                        row.get("title"),
                        row["text"],
                        row["text_hash"],
                        to_json(row.get("metadata") or {}),
                        row.get("created_at") or now,
                        now,
                    )
                    for row in records
                ],
            )
            db.executemany(
                """
                INSERT INTO qual_rag_embeddings(chunk_id, embedding_version, dim, vector_json, embedded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    embedding_version=excluded.embedding_version,
                    dim=excluded.dim,
                    vector_json=excluded.vector_json,
                    embedded_at=excluded.embedded_at
                """,
                [
                    (
                        row["chunk_id"],
                        EMBEDDING_VERSION,
                        EMBEDDING_DIM,
                        to_json(embedding(row["text"])),
                        now,
                    )
                    for row in records
                ],
            )
        return cur.rowcount if cur.rowcount is not None else len(records)

    @staticmethod
    def _rag_chunk_id(source_type: str, source_id: Any, chunk_hash: str, idx: int) -> str:
        raw = f"{source_type}:{source_id}:{chunk_hash}:{idx}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def build_qual_rag_index(self) -> dict[str, Any]:
        """Index accumulated bot experience into provenance-preserving chunks."""
        self.init_schema()
        chunks: list[dict[str, Any]] = []
        counts: dict[str, int] = {}

        def add(
            *,
            source_type: str,
            source_id: Any,
            source_row_id: Any,
            timestamp: Any,
            text: str,
            team: str | None = None,
            teams: list[str] | None = None,
            ticker: str | None = None,
            family: str | None = None,
            title: str | None = None,
            metadata: dict[str, Any] | None = None,
            split: bool = True,
        ) -> None:
            parts = chunk_text_by_paragraphs(text) if split else [text.strip()]
            for idx, part in enumerate(parts):
                digest = text_hash(part)
                chunks.append({
                    "chunk_id": self._rag_chunk_id(source_type, source_id, digest, idx),
                    "source_type": source_type,
                    "source_id": str(source_id),
                    "source_row_id": str(source_row_id),
                    "source_timestamp": timestamp,
                    "team": team,
                    "teams": teams or ([] if not team else [team]),
                    "ticker": ticker,
                    "market_family": family,
                    "title": title,
                    "text": part[:MAX_CHUNK_CHARS],
                    "text_hash": digest,
                    "metadata": {**(metadata or {}), "chunk_index": idx, "chunk_strategy": "source-specific"},
                })
                counts[source_type] = counts.get(source_type, 0) + 1

        with self.connect() as db:
            for row in db.execute("SELECT * FROM news_items ORDER BY fetched_at ASC"):
                text = "\n".join(str(v or "") for v in (row["title"], row["body"], row["url"]) if v)
                add(
                    source_type="news_items",
                    source_id=row["id"],
                    source_row_id=row["id"],
                    timestamp=row["published_at"] or row["fetched_at"],
                    text=text,
                    team=row["team"],
                    title=row["title"],
                    metadata={"url": row["url"], "source": row["source"]},
                    split=False,
                )
            for row in db.execute("SELECT * FROM settlement_records ORDER BY audited_at ASC"):
                record = json.loads(row["record_json"] or "{}")
                market = json.loads(row["market_json"] or "{}")
                family = str(record.get("market_family") or market.get("market_family") or "")
                text = (
                    f"Settlement {row['ticker']} {row['side']} outcome={row['outcome']} "
                    f"entry={row['entry_price_cents']}c closing={row['closing_consensus_cents']}c "
                    f"clv={row['clv_cents']} pnl={row['pnl_cents']} "
                    f"market={json.dumps(market, sort_keys=True)}"
                )
                add(
                    source_type="settlement_records",
                    source_id=row["client_order_id"],
                    source_row_id=row["client_order_id"],
                    timestamp=row["audited_at"],
                    text=text,
                    ticker=row["ticker"],
                    family=family,
                    title=str(market.get("title") or row["ticker"]),
                    metadata={"mode": row["mode"], "signal_source": row["signal_source"]},
                    split=False,
                )
            for row in db.execute("SELECT * FROM qual_signals ORDER BY created_at ASC"):
                analysis = json.loads(row["analysis_json"] or "{}")
                scenarios = json.loads(row["scenarios_json"] or "[]")
                market = analysis.get("market") if isinstance(analysis.get("market"), dict) else {}
                teams = [str(t) for t in (market.get("teams") or [])]
                family = str(market.get("market_family") or "")
                base = (
                    f"Prior qual signal {row['ticker']} base_rate={row['base_rate']} "
                    f"qual_prob={row['qual_prob']} confidence={row['confidence']} "
                    f"rationale={row['rationale']}"
                )
                add(
                    source_type="qual_signals",
                    source_id=row["id"],
                    source_row_id=row["id"],
                    timestamp=row["created_at"],
                    text=base,
                    teams=teams,
                    ticker=row["ticker"],
                    family=family,
                    title=str(market.get("title") or row["ticker"]),
                    metadata={"model_run_id": row["model_run_id"], "kind": "signal-summary"},
                    split=False,
                )
                for idx, branch in enumerate(scenarios if isinstance(scenarios, list) else []):
                    add(
                        source_type="qual_signals",
                        source_id=f"{row['id']}:branch:{idx}",
                        source_row_id=row["id"],
                        timestamp=row["created_at"],
                        text=(
                            f"Prior branch for {row['ticker']}: {branch.get('event')} "
                            f"p_event={branch.get('p_event')} "
                            f"p_outcome_given_event={branch.get('p_outcome_given_event')} "
                            f"status={branch.get('status', 'supported')}"
                        ),
                        teams=teams,
                        ticker=row["ticker"],
                        family=family,
                        title=str(market.get("title") or row["ticker"]),
                        metadata={"model_run_id": row["model_run_id"], "kind": "scenario-branch"},
                        split=False,
                    )
            for row in db.execute("SELECT * FROM qual_postmortems ORDER BY created_at ASC"):
                postmortem = json.loads(row["postmortem_json"] or "{}")
                lessons = json.loads(row["lessons_json"] or "[]")
                text = (
                    f"Postmortem {row['ticker']} outcome={row['outcome']} "
                    f"scenario_occurred={postmortem.get('which_scenario_occurred')} "
                    f"event_grade={postmortem.get('event_prediction_grade')} "
                    f"conditional_grade={postmortem.get('conditional_grade')} "
                    f"missed={postmortem.get('what_was_missed')} "
                    f"lessons={json.dumps(lessons, sort_keys=True)}"
                )
                add(
                    source_type="qual_postmortems",
                    source_id=row["id"],
                    source_row_id=row["id"],
                    timestamp=row["created_at"],
                    text=text,
                    ticker=row["ticker"],
                    metadata={"client_order_id": row["client_order_id"]},
                    split=True,
                )
            for row in db.execute("SELECT * FROM qual_lessons ORDER BY last_seen_at ASC"):
                add(
                    source_type="qual_lessons",
                    source_id=row["id"],
                    source_row_id=row["id"],
                    timestamp=row["last_seen_at"],
                    text=f"Reusable lesson: {row['lesson_text']} Evidence: {row['evidence_cite']}",
                    team=row["team"],
                    family=row["market_family"],
                    metadata={"hit_count": row["hit_count"], "evidence_cite": row["evidence_cite"]},
                    split=False,
                )
            for row in db.execute("SELECT * FROM closing_snapshots ORDER BY captured_at ASC"):
                snapshot = json.loads(row["snapshot_json"] or "{}")
                market = json.loads(row["market_json"] or "{}")
                text = (
                    f"Closing snapshot {row['ticker']} side={row['side']} "
                    f"consensus={row['consensus_cents']}c kalshi_mid={row['kalshi_mid_cents']}c "
                    f"executable={row['executable_cents']}c book_count={row['book_count']} "
                    f"market={json.dumps(market, sort_keys=True)}"
                )
                add(
                    source_type="closing_snapshots",
                    source_id=row["ticker"],
                    source_row_id=row["ticker"],
                    timestamp=row["captured_at"],
                    text=text,
                    ticker=row["ticker"],
                    family=str(snapshot.get("market_family") or market.get("market_family") or ""),
                    title=str(market.get("title") or row["ticker"]),
                    metadata={"market_close_time": row["market_close_time"]},
                    split=False,
                )
        written = self._rag_upsert_chunks(chunks)
        return {
            "status": "ok",
            "indexed_at": utc_now(),
            "source_counts": counts,
            "chunk_count": self.qual_rag_corpus_summary().get("chunk_count", 0),
            "written": written,
            "embedding_version": EMBEDDING_VERSION,
            "embedding_dim": EMBEDDING_DIM,
        }

    def qual_rag_chunks(self, *, at_or_before: str | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        where = ""
        params: tuple[Any, ...] = ()
        if at_or_before:
            where = "WHERE source_timestamp IS NOT NULL AND source_timestamp <= ?"
            params = (at_or_before,)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT c.*, e.vector_json
                FROM qual_rag_chunks c
                LEFT JOIN qual_rag_embeddings e ON e.chunk_id = c.chunk_id
                {where}
                ORDER BY c.source_timestamp DESC
                """,
                params,
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for source_key, target_key, default in (
                ("teams_json", "teams", []),
                ("metadata_json", "metadata", {}),
                ("vector_json", "embedding", []),
            ):
                raw = item.get(source_key)
                try:
                    item[target_key] = json.loads(raw or to_json(default))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item[target_key] = default
            out.append(item)
        return out

    def record_qual_signal_retrieval(
        self,
        *,
        model_run_id: str,
        ticker: str,
        query: dict[str, Any],
        contexts: list[dict[str, Any]],
        close_time: str | None,
        leaked_context_count: int = 0,
    ) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO qual_signal_retrievals(
                    model_run_id, ticker, retrieved_at, close_time, query_json,
                    context_json, no_lookahead_checked, leaked_context_count
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    model_run_id,
                    ticker,
                    utc_now(),
                    close_time,
                    to_json(query),
                    to_json(contexts),
                    int(leaked_context_count),
                ),
            )

    def record_qual_groundedness(self, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        records = [row for row in rows if row.get("ticker") and row.get("model_run_id")]
        if not records:
            return 0
        with self.connect() as db:
            cur = db.executemany(
                """
                INSERT OR REPLACE INTO qual_groundedness_scores(
                    model_run_id, ticker, created_at, groundedness, faithfulness,
                    context_relevance, unsupported_branch_rate, branch_count,
                    supported_branch_count, tradeable, blocked_reason, score_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["model_run_id"],
                        row["ticker"],
                        row.get("created_at") or utc_now(),
                        row["groundedness"],
                        row["faithfulness"],
                        row["context_relevance"],
                        row["unsupported_branch_rate"],
                        int(row.get("branch_count") or 0),
                        int(row.get("supported_branch_count") or 0),
                        int(bool(row.get("tradeable"))),
                        row.get("blocked_reason"),
                        to_json(row),
                    )
                    for row in records
                ],
            )
        return cur.rowcount if cur.rowcount is not None else len(records)

    def qual_rag_corpus_summary(self) -> dict[str, Any]:
        self.init_schema()
        with self.connect() as db:
            by_source = {
                row["source_type"]: int(row["count"])
                for row in db.execute(
                    """
                    SELECT source_type, COUNT(*) AS count
                    FROM qual_rag_chunks
                    GROUP BY source_type
                    ORDER BY source_type
                    """
                )
            }
            total = int(db.execute("SELECT COUNT(*) FROM qual_rag_chunks").fetchone()[0])
            retrievals = int(db.execute("SELECT COUNT(*) FROM qual_signal_retrievals").fetchone()[0])
            leaked = int(db.execute("SELECT COALESCE(SUM(leaked_context_count), 0) FROM qual_signal_retrievals").fetchone()[0])
            scores = db.execute(
                """
                SELECT AVG(groundedness) AS mean_groundedness,
                       AVG(unsupported_branch_rate) AS unsupported_rate,
                       SUM(CASE WHEN tradeable = 0 AND blocked_reason = 'low-groundedness' THEN 1 ELSE 0 END) AS low_groundedness_blocked,
                       COUNT(*) AS score_count
                FROM qual_groundedness_scores
                """
            ).fetchone()
        return {
            "chunk_count": total,
            "source_counts": by_source,
            "retrieval_count": retrievals,
            "leaked_context_count": leaked,
            "mean_groundedness": (
                round(float(scores["mean_groundedness"]), 6)
                if scores and scores["mean_groundedness"] is not None else None
            ),
            "unsupported_branch_rate": (
                round(float(scores["unsupported_rate"]), 6)
                if scores and scores["unsupported_rate"] is not None else None
            ),
            "low_groundedness_blocked": int(scores["low_groundedness_blocked"] or 0) if scores else 0,
            "groundedness_score_count": int(scores["score_count"] or 0) if scores else 0,
            "embedding_version": EMBEDDING_VERSION,
            "embedding_dim": EMBEDDING_DIM,
        }

    def record_order(self, table: str, game_id: str, intent: Any, decision: Any,
                     request: Any, receipt: Any) -> bool:
        if table not in ORDER_TABLES:
            raise ValueError(f"unsupported order table: {table}")
        self.init_schema()
        client_order_id = getattr(request, "client_order_id")
        signal_source = str(getattr(intent, "signal_source", "consensus") or "consensus")
        confluence_verdict = str(getattr(intent, "confluence_verdict", "none") or "none")
        with self.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO trade_intents(
                    client_order_id, game_id, created_at, signal_source,
                    confluence_verdict, intent_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id, game_id, utc_now(), signal_source,
                    confluence_verdict, to_json(intent),
                ),
            )
            db.execute(
                """
                INSERT OR REPLACE INTO risk_decisions(
                    client_order_id, game_id, created_at, approved, decision_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    client_order_id, game_id, utc_now(),
                    int(bool(getattr(decision, "approved", False))), to_json(decision),
                ),
            )
            cur = db.execute(
                f"""
                INSERT OR IGNORE INTO {table}(
                    client_order_id, game_id, created_at, signal_source,
                    confluence_verdict, intent_json, decision_json, request_json,
                    receipt_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id, game_id, utc_now(), signal_source,
                    confluence_verdict, to_json(intent), to_json(decision),
                    to_json(request), to_json(receipt),
                ),
            )
        return cur.rowcount == 1

    def record_confluence_records(self, game_id: str, rows: Iterable[dict[str, Any]]) -> int:
        self.init_schema()
        records = [
            row for row in rows
            if isinstance(row, dict) and row.get("ticker") and isinstance(row.get("confluence"), dict)
            and row["confluence"].get("consensus_fair_prob") is not None
            and row["confluence"].get("qual_prob") is not None
        ]
        if not records:
            return 0
        with self.connect() as db:
            cur = db.executemany(
                """
                INSERT INTO confluence_records(
                    game_id, ticker, candidate_id, created_at, consensus_fair_prob,
                    qual_prob, qual_confidence, delta, verdict, record_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        game_id,
                        str(row.get("ticker")),
                        row.get("candidate_id"),
                        utc_now(),
                        row["confluence"].get("consensus_fair_prob"),
                        row["confluence"].get("qual_prob"),
                        row["confluence"].get("qual_confidence"),
                        row["confluence"].get("delta"),
                        str(row.get("confluence_verdict") or row["confluence"].get("verdict") or "none"),
                        to_json(row["confluence"]),
                    )
                    for row in records
                ],
            )
        return cur.rowcount if cur.rowcount is not None else 0

    def record_shadow_intent(
        self,
        *,
        game_id: str,
        mode: str,
        client_order_id: str,
        intent: Any,
        reason: str,
    ) -> bool:
        self.init_schema()
        signal_source = str(getattr(intent, "signal_source", "consensus") or "consensus")
        confluence_verdict = str(getattr(intent, "confluence_verdict", "none") or "none")
        record = {
            "client_order_id": client_order_id,
            "game_id": game_id,
            "mode": mode,
            "signal_source": signal_source,
            "confluence_verdict": confluence_verdict,
            "ticker": getattr(intent, "ticker", None),
            "side": getattr(intent, "side", None),
            "contracts": getattr(intent, "contracts", None),
            "price_cents": getattr(intent, "price_cents", None),
            "stake_units": getattr(intent, "stake_units", None),
            "edge": getattr(intent, "edge", None),
            "reason": reason,
            "intent": intent,
        }
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO shadow_trade_intents(
                    client_order_id, game_id, created_at, mode, signal_source,
                    confluence_verdict, ticker, side, contracts, price_cents,
                    stake_units, edge, reason, intent_json, record_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    game_id,
                    utc_now(),
                    mode,
                    signal_source,
                    confluence_verdict,
                    str(getattr(intent, "ticker") or ""),
                    str(getattr(intent, "side") or "yes"),
                    float(getattr(intent, "contracts", 0) or 0),
                    float(getattr(intent, "price_cents", 0) or 0),
                    float(getattr(intent, "stake_units", 0) or 0),
                    getattr(intent, "edge", None),
                    reason,
                    to_json(intent),
                    to_json(record),
                ),
            )
        return cur.rowcount == 1

    def list_shadow_intents(self, unsettled_only: bool = True) -> list[dict[str, Any]]:
        self.init_schema()
        query = "SELECT * FROM shadow_trade_intents"
        if unsettled_only:
            query += " WHERE outcome IS NULL"
        query += " ORDER BY created_at ASC"
        with self.connect() as db:
            rows = db.execute(query).fetchall()
        return [dict(row) for row in rows]

    def record_shadow_settlement(self, record: dict[str, Any]) -> bool:
        self.init_schema()
        with self.connect() as db:
            cur = db.execute(
                """
                UPDATE shadow_trade_intents
                SET audited_at = ?,
                    settled_at = ?,
                    market_status = ?,
                    winning_side = ?,
                    outcome = ?,
                    pnl_cents = ?,
                    market_json = ?,
                    settlement_json = ?
                WHERE client_order_id = ?
                  AND outcome IS NULL
                """,
                (
                    record.get("audited_at") or utc_now(),
                    record.get("settled_at"),
                    record.get("market_status"),
                    record.get("winning_side"),
                    record.get("outcome"),
                    record.get("pnl_cents"),
                    to_json(record.get("market", {})),
                    to_json(record),
                    record["client_order_id"],
                ),
            )
        return cur.rowcount == 1

    @staticmethod
    def _fill_key(fill: Any) -> str:
        if isinstance(fill, dict):
            for key in ("fill_id", "id", "trade_id", "transaction_id"):
                value = fill.get(key)
                if value not in (None, ""):
                    return str(value)
            pieces = [
                fill.get("ticker"),
                fill.get("side"),
                fill.get("filled_at") or fill.get("created_time") or fill.get("created_at"),
                fill.get("contracts") or fill.get("count") or fill.get("fill_count"),
                fill.get("price_cents") or fill.get("price") or fill.get("average_fill_price"),
            ]
            return "|".join(str(piece) for piece in pieces if piece not in (None, ""))
        return ""

    def record_fill(self, game_id: str, client_order_id: str, fill: Any) -> bool:
        self.init_schema()
        fill_key = self._fill_key(fill)
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO fills(client_order_id, game_id, filled_at, fill_key, fill_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (client_order_id, game_id, utc_now(), fill_key, to_json(fill)),
            )
        return cur.rowcount == 1

    def record_order_lifecycle(
        self,
        *,
        client_order_id: str,
        game_id: str,
        mode: str,
        status: str,
        terminal: bool,
        filled_count: float = 0.0,
        canceled_at: str | None = None,
        cancel_reason: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO order_lifecycle(
                    client_order_id, game_id, mode, status, terminal, filled_count,
                    canceled_at, cancel_reason, checked_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    status=excluded.status,
                    terminal=excluded.terminal,
                    filled_count=excluded.filled_count,
                    canceled_at=COALESCE(excluded.canceled_at, order_lifecycle.canceled_at),
                    cancel_reason=COALESCE(excluded.cancel_reason, order_lifecycle.cancel_reason),
                    checked_at=excluded.checked_at,
                    raw_json=excluded.raw_json
                """,
                (
                    client_order_id,
                    game_id,
                    mode,
                    status,
                    int(bool(terminal)),
                    float(filled_count or 0.0),
                    canceled_at,
                    cancel_reason,
                    utc_now(),
                    to_json(raw or {}),
                ),
            )

    def record_closing_snapshot(self, snapshot: dict[str, Any]) -> bool:
        self.init_schema()
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT INTO closing_snapshots(
                    ticker, game_id, side, captured_at, market_close_time,
                    seconds_to_close, source, consensus_prob, consensus_cents,
                    kalshi_mid_cents, executable_cents, book_count, sources_json,
                    market_json, snapshot_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    game_id=excluded.game_id,
                    side=excluded.side,
                    captured_at=excluded.captured_at,
                    market_close_time=excluded.market_close_time,
                    seconds_to_close=excluded.seconds_to_close,
                    source=excluded.source,
                    consensus_prob=excluded.consensus_prob,
                    consensus_cents=excluded.consensus_cents,
                    kalshi_mid_cents=excluded.kalshi_mid_cents,
                    executable_cents=excluded.executable_cents,
                    book_count=excluded.book_count,
                    sources_json=excluded.sources_json,
                    market_json=excluded.market_json,
                    snapshot_json=excluded.snapshot_json
                """,
                (
                    snapshot["ticker"],
                    snapshot["game_id"],
                    snapshot["side"],
                    snapshot["captured_at"],
                    snapshot.get("market_close_time"),
                    snapshot.get("seconds_to_close"),
                    snapshot.get("source") or "candidate-ranker",
                    snapshot.get("consensus_prob"),
                    snapshot.get("consensus_cents"),
                    snapshot.get("kalshi_mid_cents"),
                    snapshot.get("executable_cents"),
                    snapshot.get("book_count"),
                    to_json(snapshot.get("sources") or []),
                    to_json(snapshot.get("market") or {}),
                    to_json(snapshot),
                ),
            )
        return cur.rowcount is not None and cur.rowcount > 0

    def latest_closing_snapshot(self, ticker: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT *
                FROM closing_snapshots
                WHERE ticker = ?
                LIMIT 1
                """,
                (ticker,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        for source_key, target_key, default in (
            ("sources_json", "sources", []),
            ("market_json", "market", {}),
            ("snapshot_json", "snapshot", {}),
        ):
            raw = item.pop(source_key, None)
            try:
                item[target_key] = json.loads(raw or to_json(default))
            except (TypeError, ValueError, json.JSONDecodeError):
                item[target_key] = default
        return item

    def list_closing_snapshots(self, limit: int | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        query = "SELECT * FROM closing_snapshots ORDER BY captured_at DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (int(limit),)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["sources"] = json.loads(item.pop("sources_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["sources"] = []
            try:
                item["market"] = json.loads(item.pop("market_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["market"] = {}
            try:
                item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["snapshot"] = {}
            out.append(item)
        return out

    def order_lifecycle_map(self) -> dict[str, dict[str, Any]]:
        self.init_schema()
        with self.connect() as db:
            rows = db.execute("SELECT * FROM order_lifecycle").fetchall()
        return {row["client_order_id"]: dict(row) for row in rows}

    def list_execution_orders(self, unsettled_only: bool = True) -> list[dict[str, Any]]:
        self.init_schema()
        rows: list[dict[str, Any]] = []
        with self.connect() as db:
            settled_ids = set()
            if unsettled_only:
                settled_ids = {
                    row["client_order_id"]
                    for row in db.execute("SELECT client_order_id FROM settlement_records")
                }
            fills = db.execute(
                """
                SELECT client_order_id, filled_at, fill_json
                FROM fills
                ORDER BY id ASC
                """
            ).fetchall()
            fills_by_order: dict[str, list[dict[str, Any]]] = {}
            for fill in fills:
                fills_by_order.setdefault(fill["client_order_id"], []).append(dict(fill))
            lifecycle_map = self.order_lifecycle_map()

            for table in sorted(ORDER_TABLES):
                mode = table.removesuffix("_orders")
                for row in db.execute(
                    f"""
                    SELECT client_order_id, game_id, created_at, signal_source,
                           confluence_verdict, intent_json, decision_json,
                           request_json, receipt_json
                    FROM {table}
                    ORDER BY created_at ASC
                    """
                ):
                    client_order_id = row["client_order_id"]
                    if client_order_id in settled_ids:
                        continue
                    payload = dict(row)
                    payload["mode"] = mode
                    payload["order_table"] = table
                    payload["fills"] = fills_by_order.get(client_order_id, [])
                    lifecycle = lifecycle_map.get(client_order_id)
                    payload["lifecycle"] = lifecycle
                    if lifecycle and int(lifecycle.get("terminal") or 0) and not payload["fills"]:
                        continue
                    rows.append(payload)
        return rows

    def list_order_records(self, mode: str = "demo", include_terminal: bool = False) -> list[dict[str, Any]]:
        table = f"{mode}_orders"
        if table not in ORDER_TABLES:
            raise ValueError(f"unsupported order mode: {mode}")
        self.init_schema()
        lifecycle = self.order_lifecycle_map()
        rows: list[dict[str, Any]] = []
        with self.connect() as db:
            for row in db.execute(
                f"""
                SELECT client_order_id, game_id, created_at, signal_source,
                       confluence_verdict, intent_json, decision_json,
                       request_json, receipt_json
                FROM {table}
                ORDER BY created_at ASC
                """
            ):
                item = dict(row)
                item["mode"] = mode
                item["order_table"] = table
                item["lifecycle"] = lifecycle.get(item["client_order_id"])
                if (
                    not include_terminal
                    and item["lifecycle"]
                    and int(item["lifecycle"].get("terminal") or 0)
                ):
                    continue
                rows.append(item)
        return rows

    def fill_rate_summary(self) -> dict[str, Any]:
        self.init_schema()
        summary: dict[str, dict[str, Any]] = {}
        with self.connect() as db:
            lifecycle = {
                row["client_order_id"]: dict(row)
                for row in db.execute("SELECT * FROM order_lifecycle")
            }
            fill_ids = {
                row["client_order_id"]
                for row in db.execute("SELECT DISTINCT client_order_id FROM fills")
            }
            for table in sorted(ORDER_TABLES):
                for row in db.execute(f"SELECT client_order_id, intent_json, signal_source FROM {table}"):
                    try:
                        intent = json.loads(row["intent_json"])
                    except (json.JSONDecodeError, TypeError):
                        intent = {}
                    source = str(intent.get("signal_source") or row["signal_source"] or "consensus")
                    role = str(intent.get("liquidity_role") or "maker")
                    key = f"{source}:{role}"
                    bucket = summary.setdefault(key, {
                        "signal_source": source,
                        "liquidity_role": role,
                        "placed": 0,
                        "filled": 0,
                        "canceled_unfilled": 0,
                        "terminal_unfilled": 0,
                    })
                    bucket["placed"] += 1
                    cid = row["client_order_id"]
                    lc = lifecycle.get(cid) or {}
                    if cid in fill_ids or float(lc.get("filled_count") or 0) > 0:
                        bucket["filled"] += 1
                    elif int(lc.get("terminal") or 0):
                        bucket["terminal_unfilled"] += 1
                        if "cancel" in str(lc.get("status") or "").lower():
                            bucket["canceled_unfilled"] += 1
        return {"buckets": list(summary.values())}

    def settlement_exists(self, client_order_id: str) -> bool:
        self.init_schema()
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM settlement_records WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return row is not None

    def record_settlement(self, record: dict[str, Any]) -> bool:
        self.init_schema()
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO settlement_records(
                    client_order_id, game_id, mode, ticker, side, contracts,
                    entry_price_cents, audited_at, settled_at, market_status,
                    winning_side, outcome, payout_cents, pnl_cents,
                    entry_model_prob, entry_market_prob, closing_consensus_prob,
                    closing_consensus_cents, clv_cents, beat_closing_consensus,
                    brier_entry_model, signal_source, confluence_verdict,
                    market_json, consensus_json, record_json,
                    clv_midpoint_cents, closing_kalshi_mid_cents,
                    closing_executable_cents, clv_unmeasured_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["client_order_id"], record["game_id"], record["mode"],
                    record["ticker"], record["side"], record["contracts"],
                    record["entry_price_cents"], record["audited_at"],
                    record.get("settled_at"), record.get("market_status"),
                    record.get("winning_side"), record["outcome"],
                    record.get("payout_cents"), record.get("pnl_cents"),
                    record.get("entry_model_prob"), record.get("entry_market_prob"),
                    record.get("closing_consensus_prob"),
                    record.get("closing_consensus_cents"), record.get("clv_cents"),
                    (
                        None if record.get("beat_closing_consensus") is None
                        else int(bool(record.get("beat_closing_consensus")))
                    ),
                    record.get("brier_entry_model"),
                    str(record.get("signal_source") or "consensus"),
                    str(record.get("confluence_verdict") or "none"),
                    to_json(record.get("market", {})),
                    to_json(record.get("closing_consensus", {})),
                    to_json(record),
                    record.get("clv_midpoint_cents"),
                    record.get("closing_kalshi_mid_cents"),
                    record.get("closing_executable_cents"),
                    record.get("clv_unmeasured_reason"),
                ),
            )
        return cur.rowcount == 1

    def update_settlement_clv(self, client_order_id: str, record: dict[str, Any]) -> bool:
        self.init_schema()
        with self.connect() as db:
            cur = db.execute(
                """
                UPDATE settlement_records
                SET closing_consensus_prob = ?,
                    closing_consensus_cents = ?,
                    clv_cents = ?,
                    beat_closing_consensus = ?,
                    consensus_json = ?,
                    record_json = ?,
                    clv_midpoint_cents = ?,
                    closing_kalshi_mid_cents = ?,
                    closing_executable_cents = ?,
                    clv_unmeasured_reason = ?
                WHERE client_order_id = ?
                """,
                (
                    record.get("closing_consensus_prob"),
                    record.get("closing_consensus_cents"),
                    record.get("clv_cents"),
                    (
                        None if record.get("beat_closing_consensus") is None
                        else int(bool(record.get("beat_closing_consensus")))
                    ),
                    to_json(record.get("closing_consensus", {})),
                    to_json(record),
                    record.get("clv_midpoint_cents"),
                    record.get("closing_kalshi_mid_cents"),
                    record.get("closing_executable_cents"),
                    record.get("clv_unmeasured_reason"),
                    client_order_id,
                ),
            )
        return cur.rowcount is not None and cur.rowcount > 0

    def list_settlement_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        query = "SELECT * FROM settlement_records ORDER BY audited_at ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (int(limit),)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_orders(self, table: str) -> int:
        if table not in ORDER_TABLES:
            raise ValueError(f"unsupported order table: {table}")
        self.init_schema()
        with self.connect() as db:
            return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def order_exists(self, table: str, client_order_id: str) -> bool:
        if table not in ORDER_TABLES:
            raise ValueError(f"unsupported order table: {table}")
        self.init_schema()
        with self.connect() as db:
            row = db.execute(
                f"SELECT 1 FROM {table} WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return row is not None

    def game_order_exposure_units(self, table: str, game_id: str) -> float:
        if table not in ORDER_TABLES:
            raise ValueError(f"unsupported order table: {table}")
        self.init_schema()
        return sum(row["stake_units"] for row in self.open_exposure_rows(table, game_id=game_id))

    @staticmethod
    def _order_tables(tables: str | Iterable[str]) -> list[str]:
        selected = [tables] if isinstance(tables, str) else list(tables)
        if not selected:
            raise ValueError("at least one order table is required")
        unsupported = [table for table in selected if table not in ORDER_TABLES]
        if unsupported:
            raise ValueError(f"unsupported order table: {unsupported[0]}")
        return selected

    @staticmethod
    def _risk_day(
        calendar_day: str | date | None,
        timezone_name: str = DEFAULT_RISK_TIMEZONE,
    ) -> tuple[date, ZoneInfo]:
        tz = ZoneInfo(timezone_name)
        if calendar_day is None:
            return datetime.now(tz).date(), tz
        if isinstance(calendar_day, date):
            return calendar_day, tz
        return date.fromisoformat(str(calendar_day)), tz

    @staticmethod
    def _created_on_day(raw: str | None, target_day: date, tz: ZoneInfo) -> bool:
        if not raw:
            return False
        try:
            created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created.astimezone(tz).date() == target_day

    def daily_order_exposure_units(
        self,
        tables: str | Iterable[str],
        calendar_day: str | date | None = None,
        *,
        timezone_name: str = DEFAULT_RISK_TIMEZONE,
        broad_slate_only: bool = False,
        signal_source: str | None = None,
    ) -> float:
        target_day, tz = self._risk_day(calendar_day, timezone_name)
        return sum(
            row["stake_units"]
            for row in self.open_exposure_rows(
                tables,
                calendar_day=target_day,
                timezone_name=timezone_name,
                broad_slate_only=broad_slate_only,
                signal_source=signal_source,
            )
            if self._created_on_day(row["created_at"], target_day, tz)
        )

    @staticmethod
    def _json_obj(raw: str | None) -> dict[str, Any]:
        try:
            obj = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def _stake_units(intent: dict[str, Any]) -> float:
        try:
            return float(intent.get("stake_units", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _is_open_exposure_row(lifecycle: dict[str, Any] | None, settled: bool) -> bool:
        if settled:
            return False
        if not lifecycle:
            return True
        if int(lifecycle.get("terminal") or 0):
            return False
        status = str(lifecycle.get("status") or "").lower()
        if not status:
            return True
        return status in OPEN_EXPOSURE_STATUSES

    def open_exposure_rows(
        self,
        tables: str | Iterable[str],
        *,
        game_id: str | None = None,
        calendar_day: str | date | None = None,
        timezone_name: str = DEFAULT_RISK_TIMEZONE,
        broad_slate_only: bool = False,
        signal_source: str | None = None,
    ) -> list[dict[str, Any]]:
        selected = self._order_tables(tables)
        risk_day = self._risk_day(calendar_day, timezone_name) if calendar_day is not None else None
        self.init_schema()
        out: list[dict[str, Any]] = []
        with self.connect() as db:
            lifecycle = {
                row["client_order_id"]: dict(row)
                for row in db.execute("SELECT * FROM order_lifecycle")
            }
            settled = {
                row["client_order_id"]
                for row in db.execute("SELECT client_order_id FROM settlement_records")
            }
            for table in selected:
                rows = db.execute(
                    f"""
                    SELECT client_order_id, game_id, created_at, signal_source,
                           intent_json, request_json
                    FROM {table}
                    {'' if game_id is None else 'WHERE game_id = ?'}
                    """,
                    () if game_id is None else (game_id,),
                )
                for row in rows:
                    if risk_day is not None and not self._created_on_day(row["created_at"], risk_day[0], risk_day[1]):
                        continue
                    client_order_id = row["client_order_id"]
                    lc = lifecycle.get(client_order_id)
                    if not self._is_open_exposure_row(lc, client_order_id in settled):
                        continue
                    intent = self._json_obj(row["intent_json"])
                    if broad_slate_only and not bool(intent.get("broad_slate")):
                        continue
                    if signal_source is not None and str(
                        intent.get("signal_source") or row["signal_source"] or "consensus"
                    ) != signal_source:
                        continue
                    stake_units = self._stake_units(intent)
                    if stake_units <= 0:
                        continue
                    request = self._json_obj(row["request_json"])
                    out.append({
                        "client_order_id": client_order_id,
                        "table": table,
                        "game_id": row["game_id"],
                        "created_at": row["created_at"],
                        "signal_source": row["signal_source"],
                        "ticker": str(intent.get("ticker") or request.get("ticker") or ""),
                        "stake_units": stake_units,
                        "intent": intent,
                        "lifecycle": lc,
                    })
        return out

    def daily_order_count(
        self,
        tables: str | Iterable[str],
        calendar_day: str | date | None = None,
        *,
        timezone_name: str = DEFAULT_RISK_TIMEZONE,
        broad_slate_only: bool = False,
        signal_source: str | None = None,
    ) -> int:
        selected = self._order_tables(tables)
        target_day, tz = self._risk_day(calendar_day, timezone_name)
        self.init_schema()
        count = 0
        with self.connect() as db:
            for table in selected:
                rows = db.execute(
                    f"SELECT created_at, signal_source, intent_json FROM {table}",
                ).fetchall()
                for row in rows:
                    if not self._created_on_day(row["created_at"], target_day, tz):
                        continue
                    if broad_slate_only or signal_source is not None:
                        try:
                            intent = json.loads(row["intent_json"])
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if broad_slate_only and not bool(intent.get("broad_slate")):
                            continue
                        if signal_source is not None and str(
                            intent.get("signal_source") or row["signal_source"] or "consensus"
                        ) != signal_source:
                            continue
                    count += 1
        return count

    def record_scenario_results(self, game_id: str, scenarios: list[dict[str, Any]],
                                legs: list[dict[str, Any]]) -> None:
        self.init_schema()
        observed_at = utc_now()
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO scenario_results(
                    game_id, scenario_id, observed_at, prior_p_joint, hit,
                    resolved_legs, total_legs, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        game_id, r["id"], observed_at, r.get("prior_p_joint"),
                        r.get("hit"), r.get("resolved_legs"), r.get("total_legs"),
                        r.get("notes"),
                    )
                    for r in scenarios
                ],
            )
            db.executemany(
                """
                INSERT INTO leg_results(
                    game_id, scenario_id, market, line, prior_p, entry_implied_p,
                    outcome, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        game_id, r.get("scenario_id"), r["market"], r.get("line"),
                        r.get("prior_p"), r.get("entry_implied_p"), r.get("outcome"),
                        observed_at,
                    )
                    for r in legs
                ],
            )

    def record_edge(self, game_id: str, scenario_id: str, model_prob: float,
                    market_prob: float | None, edge: float | None,
                    confidence: str, source: str = "scenario") -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO edge_history(
                    game_id, scenario_id, captured_at, model_prob, market_prob,
                    edge, confidence, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (game_id, scenario_id, utc_now(), model_prob, market_prob, edge, confidence, source),
            )

    def record_risk_snapshot(self, game_id: str, snapshot: dict[str, Any]) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO risk_snapshots(
                    game_id, captured_at, daily_pnl_units, game_exposure_units,
                    open_positions, circuit_breaker_on, snapshot_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id, utc_now(), snapshot.get("daily_pnl_units"),
                    snapshot.get("game_exposure_units"), snapshot.get("open_positions"),
                    int(bool(snapshot.get("circuit_breaker_on"))), to_json(snapshot),
                ),
            )

    def record_audit(self, event_type: str, payload: dict[str, Any],
                     game_id: str | None = None) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO audit_events(event_type, game_id, created_at, event_json)
                VALUES (?, ?, ?, ?)
                """,
                (event_type, game_id, utc_now(), to_json(payload)),
            )

    def record_dlq(self, event_type: str, error: str, payload: dict[str, Any],
                   game_id: str | None = None) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO dead_letter_queue(event_type, game_id, created_at, error, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_type, game_id, utc_now(), error, to_json(payload)),
            )

    def latest_rows(self, table: str, limit: int = 20) -> list[dict[str, Any]]:
        allowed = {
            "market_snapshots", "edge_history", "backtest_runs", "paper_orders",
            "demo_orders", "live_orders", "risk_decisions", "risk_snapshots", "audit_events",
            "dead_letter_queue", "market_catalog", "orderbook_snapshots",
            "settlement_records", "news_items", "qual_signals",
            "qual_recaps", "qual_postmortems", "qual_lessons",
            "market_baskets", "near_miss_investigations", "closing_snapshots",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        self.init_schema()
        order_col = {
            "backtest_runs": "run_at",
            "paper_orders": "created_at",
            "demo_orders": "created_at",
            "live_orders": "created_at",
            "risk_decisions": "created_at",
            "settlement_records": "audited_at",
            "news_items": "fetched_at",
            "qual_signals": "created_at",
            "qual_recaps": "fetched_at",
            "qual_postmortems": "created_at",
            "qual_lessons": "last_seen_at",
            "market_baskets": "created_at",
            "near_miss_investigations": "created_at",
            "closing_snapshots": "captured_at",
        }.get(table, "id")
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def signal_engine_summary(self) -> dict[str, dict[str, Any]]:
        self.init_schema()
        summary: dict[str, dict[str, Any]] = {
            "consensus": {
                "trades_placed": 0,
                "settled": 0,
                "brier": None,
                "clv_available": 0,
                "clv_beat_rate": None,
            },
            "qual": {
                "trades_placed": 0,
                "settled": 0,
                "brier": None,
                "clv_available": 0,
                "clv_beat_rate": None,
            },
            "qual_activated_quant": {
                "trades_placed": 0,
                "settled": 0,
                "brier": None,
                "clv_available": 0,
                "clv_beat_rate": None,
            },
        }
        with self.connect() as db:
            for table in sorted(ORDER_TABLES):
                for row in db.execute(f"SELECT signal_source, intent_json FROM {table}"):
                    source = str(row["signal_source"] or "consensus")
                    try:
                        intent = json.loads(row["intent_json"])
                        source = str(intent.get("signal_source") or source or "consensus")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    bucket = summary.setdefault(source, {
                        "trades_placed": 0,
                        "settled": 0,
                        "brier": None,
                        "clv_available": 0,
                        "clv_beat_rate": None,
                    })
                    bucket["trades_placed"] += 1
            settlements = db.execute(
                """
                SELECT signal_source, brier_entry_model, clv_cents, beat_closing_consensus
                FROM settlement_records
                """
            ).fetchall()
        brier_values: dict[str, list[float]] = {}
        clv_hits: dict[str, list[int]] = {}
        for row in settlements:
            source = str(row["signal_source"] or "consensus")
            bucket = summary.setdefault(source, {
                "trades_placed": 0,
                "settled": 0,
                "brier": None,
                "clv_available": 0,
                "clv_beat_rate": None,
            })
            bucket["settled"] += 1
            if row["brier_entry_model"] is not None:
                try:
                    brier_values.setdefault(source, []).append(float(row["brier_entry_model"]))
                except (TypeError, ValueError):
                    pass
            if row["clv_cents"] is not None:
                clv_hits.setdefault(source, []).append(1 if row["beat_closing_consensus"] else 0)
        for source, values in brier_values.items():
            if values:
                summary[source]["brier"] = round(sum(values) / len(values), 6)
        for source, hits in clv_hits.items():
            summary[source]["clv_available"] = len(hits)
            summary[source]["clv_beat_rate"] = round(sum(hits) / len(hits), 4) if hits else None
        return summary
