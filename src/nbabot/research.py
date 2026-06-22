"""SQLite mirror for research, backtests, snapshots, and execution ledgers."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ORDER_TABLES = {"paper_orders", "demo_orders", "live_orders"}


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
                    source TEXT NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS trade_intents (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
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
                    intent_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_orders (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_orders (
                    client_order_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
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
                """
            )

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

    def record_order(self, table: str, game_id: str, intent: Any, decision: Any,
                     request: Any, receipt: Any) -> bool:
        if table not in ORDER_TABLES:
            raise ValueError(f"unsupported order table: {table}")
        self.init_schema()
        client_order_id = getattr(request, "client_order_id")
        with self.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO trade_intents(client_order_id, game_id, created_at, intent_json)
                VALUES (?, ?, ?, ?)
                """,
                (client_order_id, game_id, utc_now(), to_json(intent)),
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
                    client_order_id, game_id, created_at, intent_json, decision_json,
                    request_json, receipt_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id, game_id, utc_now(), to_json(intent),
                    to_json(decision), to_json(request), to_json(receipt),
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
            "dead_letter_queue", "market_catalog",
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
        }.get(table, "id")
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
