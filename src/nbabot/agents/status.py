"""Phase: status. Summarize operating mode, artifacts, and recent ledger state."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import guardrails
from ..alerts import deliver
from ..research import ResearchStore
from .base import Context, load_context


LIVE_ACK = "LIVE_TRADES_REAL_MONEY"


def _artifact_summary(ctx: Context, suffix: str) -> dict[str, Any]:
    path = ctx.settings.data_path(suffix)
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _live_ready(ctx: Context) -> tuple[bool, list[str]]:
    missing = []
    if ctx.settings.execution_mode != "live":
        missing.append("NBABOT_EXECUTION_MODE=live")
    if ctx.settings.dry_run:
        missing.append("NBABOT_DRY_RUN=0")
    if ctx.settings.live_trading_ack != LIVE_ACK:
        missing.append(f"NBABOT_LIVE_TRADING_ACK={LIVE_ACK}")
    if Path(ctx.settings.kill_switch_path).exists():
        missing.append(f"remove kill switch at {ctx.settings.kill_switch_path}")
    return not missing, missing


def _count_orders(store: ResearchStore, table: str) -> int:
    try:
        return store.count_orders(table)
    except Exception:
        return 0


def build_status(ctx: Context) -> dict[str, Any]:
    store = ResearchStore(ctx.settings.research_db_path)
    live_ready, blockers = _live_ready(ctx)
    artifacts = {
        name: _artifact_summary(ctx, name)
        for name in (
            "market_catalog.json",
            "market_snapshot.json",
            "book_watch.json",
            "autopilot.json",
            "daily_cycle.json",
            "portfolio_sync.json",
            "source_check.json",
            "reconcile.json",
            "backtest.json",
        )
    }
    latest_risk = store.latest_rows("risk_snapshots", 1)
    latest_live = store.latest_rows("live_orders", 1)
    latest_audit = store.latest_rows("audit_events", 3)
    return {
        "game_id": ctx.settings.game_id,
        "sport": ctx.settings.sport,
        "execution_mode": ctx.settings.execution_mode,
        "dry_run": ctx.settings.dry_run,
        "live_ready": live_ready,
        "live_blockers": blockers,
        "kill_switch": str(ctx.settings.kill_switch_path),
        "kill_switch_on": Path(ctx.settings.kill_switch_path).exists(),
        "orders": {
            "paper": _count_orders(store, "paper_orders"),
            "demo": _count_orders(store, "demo_orders"),
            "live": _count_orders(store, "live_orders"),
        },
        "latest_risk_snapshot": latest_risk[0] if latest_risk else None,
        "latest_live_order": latest_live[0] if latest_live else None,
        "latest_audit_events": latest_audit,
        "artifacts": artifacts,
    }


def _format_status(status: dict[str, Any]) -> str:
    blockers = status["live_blockers"]
    live_line = "ready" if status["live_ready"] else "blocked: " + "; ".join(blockers)
    risk = status.get("latest_risk_snapshot") or {}
    orders = status["orders"]
    exposure = risk.get("game_exposure_units")
    daily_pnl = risk.get("daily_pnl_units")
    exposure_label = f"{exposure}u" if exposure is not None else "n/a"
    daily_pnl_label = f"{daily_pnl}u" if daily_pnl is not None else "n/a"
    return "\n".join([
        f"[status] {status['game_id']} sport={status['sport']}",
        f"  mode={status['execution_mode']} dry_run={status['dry_run']} live={live_line}",
        f"  orders paper={orders['paper']} demo={orders['demo']} live={orders['live']}",
        (
            "  risk "
            f"exposure={exposure_label} "
            f"daily_pnl={daily_pnl_label} "
            f"open_positions={risk.get('open_positions', 'n/a')}"
        ),
        f"  kill_switch={'ON' if status['kill_switch_on'] else 'clear'}",
    ])


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    status = build_status(ctx)
    ctx.write_json("status.json", status)
    deliver(guardrails.with_footer(_format_status(status)), ctx.settings.deliver_to)
    return status
