"""Pure monitor snapshot composition for operator visibility."""
from __future__ import annotations

import json
import os
import sqlite3
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .research import ResearchStore
from .validation_report import report as build_validation_report

ET = ZoneInfo("America/New_York")
ENGINE_KEYS = ("consensus", "qual", "qual_activated_quant")
LIVE_ACK = "LIVE_TRADES_REAL_MONEY"


@dataclass(frozen=True)
class MonitorSnapshot:
    generated_at: str
    host: str
    git_commit: str | None
    game_id: str
    bot_alive: bool
    hours_since_last_successful_cycle: float | None
    last_cycle_run_time_et: str | None
    last_cycle_edges_found: int | None
    last_cycle_orders_placed: int | None
    last_cycle_hard_error: str | None
    health: dict[str, Any]
    balance_usd: float | None
    balance_source: str | None
    unit_size_dollars: float | None
    open_positions_count: int | None
    exposure_units_authoritative: float | None
    exposure_reconciliation_diverged: bool
    trades_today: dict[str, Any]
    pnl: dict[str, Any]
    per_engine: dict[str, dict[str, Any]]
    validation_distance: dict[str, dict[str, Any]]
    alerts_delivery_health: dict[str, Any]
    live_gates_status: dict[str, bool]
    pending_prs: list[int]
    errors: list[str] = field(default_factory=list)
    escalated_alert_conditions: list[str] = field(default_factory=list)
    delivery_attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_to_json(snapshot: MonitorSnapshot) -> str:
    return json.dumps(snapshot.to_dict(), indent=2, sort_keys=True, default=str) + "\n"


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        if not path.exists():
            return {}, None
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            return loaded, None
        return {}, f"{path.name}: expected object"
    except Exception as exc:
        return {}, f"{path.name}: {exc}"


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        if not path.exists():
            return [], None
        rows = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                rows.append(loaded)
        return rows, None
    except Exception as exc:
        return [], f"{path.name}: {exc}"


def _safe_float(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _safe_int(raw: Any) -> int | None:
    try:
        if raw is None or raw == "":
            return None
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _json_obj(raw: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _cycle_summary(data_dir: Path, now: datetime) -> tuple[dict[str, Any], list[str]]:
    rows, error = _read_jsonl(data_dir / "scheduled_cycle_runs.jsonl")
    errors = [error] if error else []
    finishes = [row for row in rows if row.get("event") == "finished"]
    finishes.sort(key=lambda row: str(row.get("finished_at") or ""))
    last = finishes[-1] if finishes else {}
    successful = [
        row for row in finishes
        if int(row.get("exit_code") or 0) == 0 and not row.get("hard_error")
    ]
    last_success = successful[-1] if successful else {}
    success_dt = _parse_dt(last_success.get("finished_at"))
    last_dt = _parse_dt(last.get("finished_at"))
    hours = None
    if success_dt is not None:
        hours = round((now - success_dt).total_seconds() / 3600.0, 4)
    return {
        "bot_alive": bool(hours is not None and hours <= 6.0),
        "hours_since_last_successful_cycle": hours,
        "last_cycle_run_time_et": last_dt.astimezone(ET).isoformat() if last_dt else None,
        "last_cycle_edges_found": _safe_int(last.get("edges")),
        "last_cycle_orders_placed": _safe_int(last.get("orders_placed")),
        "last_cycle_hard_error": None if last.get("hard_error") is None else str(last.get("hard_error")),
        "last_two_hard_errors": (
            len(finishes[-2:]) == 2
            and all(row.get("hard_error") or int(row.get("exit_code") or 0) for row in finishes[-2:])
        ),
    }, errors


def _balance_and_exposure(ctx: Any) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    data_dir = ctx.settings.data_dir
    portfolio, error = _read_json(data_dir / "portfolio_sync.json")
    if error:
        errors.append(error)
    status, error = _read_json(data_dir / f"{ctx.settings.game_id}.status.json")
    if error:
        errors.append(error)
    if not status:
        status, error = _read_json(data_dir / "status.json")
        if error:
            errors.append(error)

    unit = portfolio.get("unit") if isinstance(portfolio.get("unit"), dict) else {}
    status_unit = status.get("unit") if isinstance(status.get("unit"), dict) else {}
    exposure = portfolio.get("exposure_reconciliation")
    if not isinstance(exposure, dict):
        exposure = status.get("exposure_reconciliation") if isinstance(status.get("exposure_reconciliation"), dict) else {}

    balance = _safe_float(portfolio.get("balance_usd_exact"))
    if balance is None:
        balance = _safe_float(portfolio.get("balance_usd"))
    if balance is None and _safe_float(portfolio.get("balance_cents")) is not None:
        balance = float(portfolio["balance_cents"]) / 100.0
    if balance is None:
        balance = _safe_float(status_unit.get("bankroll_usd"))
    return {
        "balance_usd": None if balance is None else round(balance, 4),
        "balance_source": portfolio.get("balance_source") or (status_unit or {}).get("bankroll_source"),
        "unit_size_dollars": _safe_float(unit.get("unit_size_dollars")) or _safe_float(status_unit.get("unit_size_dollars")),
        "open_positions_count": _safe_int(portfolio.get("open_positions"))
        if portfolio.get("open_positions") is not None
        else _safe_int(((status.get("latest_risk_snapshot") or {}) if isinstance(status.get("latest_risk_snapshot"), dict) else {}).get("open_positions")),
        "exposure_units_authoritative": _safe_float(exposure.get("authoritative_portfolio_exposure_units"))
        if exposure else None,
        "exposure_reconciliation_diverged": bool(exposure.get("diverged") or exposure.get("warnings")),
    }, errors


def _order_rows(store: ResearchStore) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with store.connect() as db:
            for table in ("paper_orders", "demo_orders", "live_orders"):
                try:
                    found = db.execute(
                        f"""
                        SELECT client_order_id, game_id, created_at, signal_source,
                               intent_json, request_json, receipt_json
                        FROM {table}
                        """
                    ).fetchall()
                except sqlite3.Error as exc:
                    errors.append(f"{table}: {exc}")
                    continue
                for row in found:
                    item = dict(row)
                    item["table"] = table
                    rows.append(item)
    except Exception as exc:
        errors.append(f"orders db: {exc}")
    return rows, errors


def _settlement_rows(store: ResearchStore) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        with store.connect() as db:
            rows = db.execute("SELECT * FROM settlement_records").fetchall()
        return [dict(row) for row in rows], []
    except Exception as exc:
        return [], [f"settlement_records: {exc}"]


def _created_today(raw: Any, today_et: datetime) -> bool:
    parsed = _parse_dt(raw)
    return bool(parsed and parsed.astimezone(ET).date() == today_et.date())


def _created_since(raw: Any, now: datetime, delta: timedelta) -> bool:
    parsed = _parse_dt(raw)
    return bool(parsed and now - parsed <= delta)


def _ticker(row: dict[str, Any]) -> str:
    intent = _json_obj(row.get("intent_json"))
    request = _json_obj(row.get("request_json"))
    return str(intent.get("ticker") or request.get("ticker") or row.get("ticker") or "")


def _signal_source(row: dict[str, Any]) -> str:
    intent = _json_obj(row.get("intent_json"))
    source = intent.get("signal_source") or row.get("signal_source") or "consensus"
    return str(source if source in ENGINE_KEYS else "consensus")


def _stake_usd(row: dict[str, Any], unit_size: float | None) -> float:
    intent = _json_obj(row.get("intent_json"))
    explicit = _safe_float(intent.get("stake_dollars"))
    if explicit is not None:
        return explicit
    units = _safe_float(intent.get("units_staked")) or _safe_float(intent.get("stake_units"))
    intent_unit = _safe_float(intent.get("unit_size_dollars")) or unit_size
    if units is not None and intent_unit is not None:
        return units * intent_unit
    contracts = _safe_float(intent.get("contracts"))
    price = _safe_float(intent.get("price_cents"))
    if contracts is not None and price is not None:
        return contracts * price / 100.0
    return 0.0


def _trades_today(rows: list[dict[str, Any]], now: datetime, unit_size: float | None) -> dict[str, Any]:
    today = [row for row in rows if _created_today(row.get("created_at"), now.astimezone(ET))]
    breakdown = {key: 0 for key in ENGINE_KEYS}
    for row in today:
        breakdown[_signal_source(row)] += 1
    return {
        "count": len(today),
        "tickers": sorted(ticker for ticker in {_ticker(row) for row in today} if ticker),
        "total_stake_usd": round(sum(_stake_usd(row, unit_size) for row in today), 4),
        "source_breakdown": breakdown,
    }


def _pnl(settlements: list[dict[str, Any]], now: datetime, max_share: float) -> dict[str, Any]:
    today = [row for row in settlements if _created_today(row.get("settled_at") or row.get("audited_at"), now.astimezone(ET))]
    week = [row for row in settlements if _created_since(row.get("settled_at") or row.get("audited_at"), now, timedelta(days=7))]
    all_pnl = [(_safe_float(row.get("pnl_cents")) or 0.0) for row in settlements]
    winners = [pnl for pnl in all_pnl if pnl > 0]
    total = sum(all_pnl)
    largest = max(winners) if winners else None
    share = round(largest / total, 6) if largest is not None and total > 0 else None
    return {
        "today_usd": round(sum((_safe_float(row.get("pnl_cents")) or 0.0) for row in today) / 100.0, 4),
        "week_usd": round(sum((_safe_float(row.get("pnl_cents")) or 0.0) for row in week) / 100.0, 4),
        "all_time_usd": round(total / 100.0, 4),
        "largest_winner_share": share,
        "concentration_flag": bool(share is not None and share > max_share),
    }


def _per_engine(orders: list[dict[str, Any]], settlements: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    filled_ids: set[str] = set()
    try:
        # Filled counts are calculated in the agent's DB state; absent fills leave this conservative.
        pass
    except Exception:
        pass
    out: dict[str, dict[str, Any]] = {}
    for key in ENGINE_KEYS:
        placed = [row for row in orders if _signal_source(row) == key]
        settled = [row for row in settlements if str(row.get("signal_source") or "consensus") == key]
        wins = sum(1 for row in settled if int(row.get("outcome") or 0) == 1)
        losses = sum(1 for row in settled if int(row.get("outcome") or 0) == 0)
        clv_values = [_safe_float(row.get("clv_cents")) for row in settled]
        clv_values = [value for value in clv_values if value is not None]
        out[key] = {
            "placed": len(placed),
            "filled": None if not filled_ids else sum(1 for row in placed if row.get("client_order_id") in filled_ids),
            "settled": len(settled),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(settled), 4) if settled else None,
            "avg_clv": round(sum(clv_values) / len(clv_values), 4) if clv_values else None,
            "pnl_usd": round(sum((_safe_float(row.get("pnl_cents")) or 0.0) for row in settled) / 100.0, 4),
        }
    return out


def _per_engine_with_fills(
    store: ResearchStore,
    orders: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    out = _per_engine(orders, settlements)
    try:
        with store.connect() as db:
            fill_ids = {
                row["client_order_id"]
                for row in db.execute("SELECT DISTINCT client_order_id FROM fills").fetchall()
            }
            lifecycle_filled = {
                row["client_order_id"]
                for row in db.execute("SELECT client_order_id FROM order_lifecycle WHERE filled_count > 0").fetchall()
            }
        filled_ids = fill_ids | lifecycle_filled
    except Exception as exc:
        return out, [f"fills: {exc}"]
    for key in ENGINE_KEYS:
        placed = [row for row in orders if _signal_source(row) == key]
        settled_ids = {row.get("client_order_id") for row in settlements if str(row.get("signal_source") or "consensus") == key}
        out[key]["filled"] = sum(
            1 for row in placed
            if row.get("client_order_id") in filled_ids or row.get("client_order_id") in settled_ids
        )
    return out, []


def _validation_distance(ctx: Any, store: ResearchStore, settlements: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    try:
        report = build_validation_report(
            settlements,
            default_sport=ctx.settings.sport,
            concentration_max_winner_share=float(getattr(ctx.settings, "concentration_max_winner_share", 0.50)),
        )
    except Exception as exc:
        return {}, [f"validation_report: {exc}"]
    thresholds = report.get("validation_thresholds") or {}
    out: dict[str, Any] = {}
    for group in report.get("groups") or []:
        key = f"{group.get('market_family')}:{group.get('signal_source')}"
        out[key] = {
            "needed_settled": max(
                int(thresholds.get("min_validated_n") or 0) - int(group.get("settled_count") or 0),
                0,
            ),
            "needed_clv_beat_rate": (
                None
                if group.get("clv_beat_rate") is None
                else max(round(float(thresholds.get("min_clv_beat_rate") or 0) - float(group["clv_beat_rate"]), 6), 0)
            ),
            "needed_brier_improvement": (
                None
                if group.get("brier_improvement_vs_market") is None
                else max(round(float(thresholds.get("min_brier_improvement") or 0) - float(group["brier_improvement_vs_market"]), 6), 0)
            ),
            "remaining_distance": group.get("remaining_distance") or [],
        }
    return out, []


def _delivery_health(data_dir: Path) -> tuple[dict[str, Any], list[str]]:
    state, error = _read_json(data_dir / "monitor_delivery_health.json")
    errors = [error] if error else []
    return {
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "last_success_at": state.get("last_success_at"),
        "failing": bool(state.get("failing")),
        "last_attempts": state.get("last_attempts") or [],
    }, errors


def _live_gates(ctx: Any) -> dict[str, bool]:
    return {
        "execution_mode_live": str(getattr(ctx.settings, "execution_mode", "")).lower() == "live",
        "dry_run_disabled": not bool(getattr(ctx.settings, "dry_run", True)),
        "live_trading_ack": getattr(ctx.settings, "live_trading_ack", "") == LIVE_ACK,
        "kill_switch_clear": not Path(getattr(ctx.settings, "kill_switch_path", "")).exists(),
    }


def evaluate_alert_conditions(
    snapshot: MonitorSnapshot,
    *,
    balance_drop_pct: float | None = None,
) -> list[str]:
    conditions = []
    if (
        snapshot.hours_since_last_successful_cycle is None
        or snapshot.hours_since_last_successful_cycle > 6.0
    ):
        conditions.append("bot silent > 6h")
    if snapshot.last_cycle_hard_error and snapshot.health.get("recent_cycle_hard_error_count", 0) >= 2:
        conditions.append("consecutive cycle failures")
    if balance_drop_pct is not None and balance_drop_pct > 0.05:
        conditions.append("balance drop")
    if int(snapshot.alerts_delivery_health.get("consecutive_failures") or 0) > 12:
        conditions.append(
            f"your alerts have been failing for {snapshot.alerts_delivery_health.get('consecutive_failures')} hours"
        )
    return conditions


def build_monitor_snapshot(
    ctx: Any,
    store: ResearchStore,
    *,
    generated_at: str | None = None,
    host: str | None = None,
    git_commit: str | None = None,
    pending_prs: list[int] | None = None,
    balance_drop_pct: float | None = None,
) -> MonitorSnapshot:
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    now = _parse_dt(generated) or datetime.now(timezone.utc)
    data_dir = ctx.settings.data_dir
    errors: list[str] = []

    cycle, cycle_errors = _cycle_summary(data_dir, now)
    errors.extend(cycle_errors)
    health_artifact, error = _read_json(data_dir / "health_status.json")
    if error:
        errors.append(error)
    health_reasons = health_artifact.get("reasons") if isinstance(health_artifact.get("reasons"), list) else []
    recent = health_artifact.get("recent_cycles") if isinstance(health_artifact.get("recent_cycles"), list) else []
    hard_error_count = sum(1 for row in recent[-2:] if isinstance(row, dict) and (row.get("hard_error") or int(row.get("exit_code") or 0)))
    health = {
        "alert": bool(health_artifact.get("alert") or (health_artifact and not health_artifact.get("ok", True))),
        "reasons": [str(reason) for reason in health_reasons],
        "checked_at": health_artifact.get("checked_at"),
        "from": "data/health_status.json" if health_artifact else None,
        "recent_cycle_hard_error_count": hard_error_count,
    }

    balance, balance_errors = _balance_and_exposure(ctx)
    errors.extend(balance_errors)
    orders, order_errors = _order_rows(store)
    errors.extend(order_errors)
    settlements, settlement_errors = _settlement_rows(store)
    errors.extend(settlement_errors)
    per_engine, fill_errors = _per_engine_with_fills(store, orders, settlements)
    errors.extend(fill_errors)
    validation_distance, validation_errors = _validation_distance(ctx, store, settlements)
    errors.extend(validation_errors)
    delivery_health, delivery_errors = _delivery_health(data_dir)
    errors.extend(delivery_errors)

    snapshot = MonitorSnapshot(
        generated_at=generated,
        host=host or socket.gethostname(),
        git_commit=git_commit,
        game_id=ctx.settings.game_id,
        bot_alive=bool(cycle["bot_alive"]),
        hours_since_last_successful_cycle=cycle["hours_since_last_successful_cycle"],
        last_cycle_run_time_et=cycle["last_cycle_run_time_et"],
        last_cycle_edges_found=cycle["last_cycle_edges_found"],
        last_cycle_orders_placed=cycle["last_cycle_orders_placed"],
        last_cycle_hard_error=cycle["last_cycle_hard_error"],
        health=health,
        balance_usd=balance["balance_usd"],
        balance_source=balance["balance_source"],
        unit_size_dollars=balance["unit_size_dollars"],
        open_positions_count=balance["open_positions_count"],
        exposure_units_authoritative=balance["exposure_units_authoritative"],
        exposure_reconciliation_diverged=balance["exposure_reconciliation_diverged"],
        trades_today=_trades_today(orders, now, balance["unit_size_dollars"]),
        pnl=_pnl(
            settlements,
            now,
            float(getattr(ctx.settings, "concentration_max_winner_share", 0.50)),
        ),
        per_engine=per_engine,
        validation_distance=validation_distance,
        alerts_delivery_health=delivery_health,
        live_gates_status=_live_gates(ctx),
        pending_prs=sorted(pending_prs or []),
        errors=errors,
    )
    return MonitorSnapshot(
        **{
            **snapshot.to_dict(),
            "escalated_alert_conditions": evaluate_alert_conditions(
                snapshot,
                balance_drop_pct=balance_drop_pct,
            ),
        }
    )


def format_monitor_md(snapshot: MonitorSnapshot) -> str:
    s = snapshot.to_dict()
    health = s["health"]
    trades = s["trades_today"]
    pnl = s["pnl"]
    delivery = s["alerts_delivery_health"]
    engines = s["per_engine"]
    lines = [
        f"# monitor {s['game_id']}",
        "## alive",
        f"- alive: {s['bot_alive']} last_success_hours={s['hours_since_last_successful_cycle']}",
        f"- last_cycle: {s['last_cycle_run_time_et']} edges={s['last_cycle_edges_found']} orders={s['last_cycle_orders_placed']} hard_error={s['last_cycle_hard_error']}",
        f"- host: {s['host']} commit={s['git_commit'] or 'unknown'}",
        "## trading",
        f"- balance: ${s['balance_usd'] if s['balance_usd'] is not None else 'n/a'} unit=${s['unit_size_dollars'] if s['unit_size_dollars'] is not None else 'n/a'} positions={s['open_positions_count']}",
        f"- exposure: authoritative={s['exposure_units_authoritative']}u diverged={s['exposure_reconciliation_diverged']}",
        f"- trades_today: count={trades['count']} stake=${trades['total_stake_usd']} tickers={','.join(trades['tickers']) or 'none'}",
        f"- live_gates: {s['live_gates_status']}",
        "## health",
        f"- health_alert: {health['alert']} reasons={'; '.join(health['reasons']) or 'none'}",
        f"- delivery: failing={delivery['failing']} consecutive_failures={delivery['consecutive_failures']} last_success={delivery['last_success_at']}",
        f"- escalated: {', '.join(s['escalated_alert_conditions']) or 'none'}",
        f"- errors: {'; '.join(s['errors']) or 'none'}",
        "## performance",
        f"- pnl: today=${pnl['today_usd']} week=${pnl['week_usd']} all_time=${pnl['all_time_usd']} concentration={pnl['concentration_flag']}",
    ]
    for key in ENGINE_KEYS:
        row = engines.get(key) or {}
        lines.append(
            f"- {key}: placed={row.get('placed')} filled={row.get('filled')} "
            f"settled={row.get('settled')} W-L={row.get('wins')}-{row.get('losses')} "
            f"win_rate={row.get('win_rate')} avg_clv={row.get('avg_clv')} pnl=${row.get('pnl_usd')}"
        )
    lines.append(f"- pending_prs: {','.join(str(n) for n in s['pending_prs']) or 'none'}")
    return "\n".join(lines[:39]) + "\n"


def read_balance_history(data_dir: Path) -> list[dict[str, Any]]:
    rows, _ = _read_jsonl(data_dir / "monitor_balance_history.jsonl")
    return rows


def trailing_balance_drop_pct(
    history: list[dict[str, Any]],
    *,
    current_balance: float | None,
    now: datetime,
) -> float | None:
    if current_balance is None:
        return None
    cutoff = now - timedelta(hours=24)
    candidates = []
    for row in history:
        ts = _parse_dt(row.get("generated_at"))
        balance = _safe_float(row.get("balance_usd"))
        if ts is not None and balance is not None and ts <= cutoff:
            candidates.append((ts, balance))
    if not candidates:
        return None
    _, baseline = max(candidates, key=lambda item: item[0])
    if baseline <= 0:
        return None
    return round((baseline - current_balance) / baseline, 6)


def env_live_gate_status() -> dict[str, bool]:
    return {
        "execution_mode_live": os.environ.get("NBABOT_EXECUTION_MODE", "").lower() == "live",
        "dry_run_disabled": os.environ.get("NBABOT_DRY_RUN", "1") in {"0", "false", "False", ""},
        "live_trading_ack": os.environ.get("NBABOT_LIVE_TRADING_ACK", "") == LIVE_ACK,
        "kill_switch_clear": True,
    }
