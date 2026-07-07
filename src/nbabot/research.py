"""SQLite mirror for research, backtests, snapshots, and execution ledgers."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .qual_learning import normalize_lesson, qual_calibration_stats

ORDER_TABLES = {"paper_orders", "demo_orders", "live_orders"}
DEFAULT_RISK_TIMEZONE = "America/New_York"


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
                    fill_json TEXT NOT NULL
                );
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
                    signal_source TEXT NOT NULL DEFAULT 'qual',
                    UNIQUE(ticker, model_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_qual_signals_ticker_time
                    ON qual_signals(ticker, created_at DESC);
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
            self._ensure_column(db, "qual_signals", "base_rate", "REAL")
            self._ensure_column(db, "qual_signals", "scenarios_json", "TEXT")
            self._ensure_column(db, "qual_signals", "analysis_json", "TEXT")
            self._ensure_column(db, "qual_signals", "news_item_ids_json", "TEXT")
            self._ensure_column(db, "qual_signals", "prompt_version", "TEXT")

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
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=float(window_hours))).isoformat()
        placeholders = ",".join("?" for _ in team_list)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT *
                FROM news_items
                WHERE team IN ({placeholders})
                  AND (published_at IS NULL OR published_at >= ?)
                ORDER BY published_at DESC, fetched_at DESC
                """,
                (*team_list, cutoff),
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
                    created_at, model_run_id, prompt_version, signal_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            "created_at": r.get("created_at"),
                        }),
                        to_json(r.get("news_item_ids_used") or []),
                        r.get("created_at") or utc_now(),
                        r["model_run_id"],
                        r.get("prompt_version"),
                        str(r.get("signal_source") or "qual"),
                    )
                    for r in rows
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
                WHERE signal_source = 'qual'
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

    def record_fill(self, game_id: str, client_order_id: str, fill: Any) -> None:
        self.init_schema()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO fills(client_order_id, game_id, filled_at, fill_json)
                VALUES (?, ?, ?, ?)
                """,
                (client_order_id, game_id, utc_now(), to_json(fill)),
            )

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
                    rows.append(payload)
        return rows

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
                    market_json, consensus_json, record_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        return cur.rowcount == 1

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
        with self.connect() as db:
            rows = db.execute(
                f"SELECT intent_json FROM {table} WHERE game_id = ?",
                (game_id,),
            ).fetchall()
        exposure = 0.0
        for row in rows:
            try:
                intent = json.loads(row["intent_json"])
                exposure += float(intent.get("stake_units", 0.0) or 0.0)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return exposure

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
        selected = self._order_tables(tables)
        target_day, tz = self._risk_day(calendar_day, timezone_name)
        self.init_schema()
        exposure = 0.0
        with self.connect() as db:
            for table in selected:
                rows = db.execute(
                    f"SELECT created_at, signal_source, intent_json FROM {table}",
                ).fetchall()
                for row in rows:
                    if not self._created_on_day(row["created_at"], target_day, tz):
                        continue
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
                    try:
                        exposure += float(intent.get("stake_units", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        continue
        return exposure

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
