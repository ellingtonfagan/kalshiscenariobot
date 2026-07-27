"""Durable scheduled-cycle ledger and health checks."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .exposure import reconcile_open_exposure
from .research import ResearchStore, utc_now

EXPECTED_PHASES = {
    "source-check",
    "news-ingest",
    "slate-discovery",
    "slate-verify",
    "portfolio-sync",
    "discover-markets",
    "snapshot-market",
    "book-watch",
    "market-matcher",
    "candidate-ranker",
    "research-agent",
    "demo-execute",
    "order-reconcile",
    "scheduled-demo-cycle",
    "health-check",
    "status",
}


def _path(data_dir: Path, name: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / name


def cycle_ledger_path(data_dir: Path) -> Path:
    return _path(data_dir, "scheduled_cycle_runs.jsonl")


def health_status_path(data_dir: Path) -> Path:
    return _path(data_dir, "health_status.json")


def delivery_failure_path(data_dir: Path) -> Path:
    return _path(data_dir, "alert_delivery_failures.json")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def record_cycle_started(ctx: Any, *, phase: str = "scheduled-demo-cycle") -> str:
    started_at = utc_now()
    _append_jsonl(cycle_ledger_path(ctx.settings.data_dir), {
        "event": "started",
        "phase": phase,
        "game_id": ctx.settings.game_id,
        "started_at": started_at,
        "execution_mode": ctx.settings.execution_mode,
        "dry_run": ctx.settings.dry_run,
    })
    return started_at


def record_cycle_finished(
    ctx: Any,
    *,
    started_at: str,
    payload: dict[str, Any],
    phase: str = "scheduled-demo-cycle",
) -> None:
    _append_jsonl(cycle_ledger_path(ctx.settings.data_dir), {
        "event": "finished",
        "phase": phase,
        "game_id": ctx.settings.game_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "exit_code": int(payload.get("exit_code") or 0),
        "candidates": int(payload.get("candidates_evaluated") or 0),
        "edges": int(payload.get("edges_found") or 0),
        "orders_placed": int(payload.get("demo_orders_placed") or 0),
        "hard_error": payload.get("hard_error"),
    })


def read_cycle_finishes(data_dir: Path, limit: int | None = None) -> list[dict[str, Any]]:
    path = cycle_ledger_path(data_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("event") == "finished":
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("finished_at") or ""))
    return rows[-limit:] if limit is not None else rows


def registry_self_test() -> tuple[bool, list[str]]:
    try:
        from .agents import PHASES
    except Exception as exc:
        return False, [f"phase registry import failed: {exc}"]
    missing = sorted(phase for phase in EXPECTED_PHASES if phase not in PHASES)
    bad = sorted(phase for phase, fn in PHASES.items() if not callable(fn))
    reasons = [f"missing expected phase: {phase}" for phase in missing]
    reasons.extend(f"phase is not callable: {phase}" for phase in bad)
    return not reasons, reasons


def record_delivery_result(data_dir: Path, *, ok: bool, target: str, phase: str) -> dict[str, Any]:
    path = delivery_failure_path(data_dir)
    if ok:
        if path.exists():
            path.unlink()
        return {"failing": False, "count": 0}
    now = utc_now()
    state: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            state = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            state = {}
    state = {
        "failing": True,
        "count": int(state.get("count") or 0) + 1,
        "first_failed_at": state.get("first_failed_at") or now,
        "last_failed_at": now,
        "target": target,
        "phase": phase,
    }
    path.write_text(json.dumps(state, indent=2, sort_keys=True))
    return state


def read_delivery_failures(data_dir: Path) -> dict[str, Any]:
    path = delivery_failure_path(data_dir)
    if not path.exists():
        return {"failing": False, "count": 0}
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"failing": True, "count": 0, "reason": "delivery failure file unreadable"}
    return loaded if isinstance(loaded, dict) else {"failing": True, "count": 0}


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_health_verdict(ctx: Any, *, max_age_hours: float = 8.0, hard_error_limit: int = 2) -> dict[str, Any]:
    reasons: list[str] = []
    cycles = read_cycle_finishes(ctx.settings.data_dir)
    successful = [row for row in cycles if int(row.get("exit_code") or 0) == 0 and not row.get("hard_error")]
    last_success = successful[-1] if successful else None
    now = datetime.now(timezone.utc)
    if last_success is None:
        reasons.append("no scheduled cycle has completed successfully")
    else:
        finished = _parse_dt(last_success.get("finished_at"))
        if finished is None:
            reasons.append("last successful cycle has an unreadable finished_at")
        elif now - finished > timedelta(hours=float(max_age_hours)):
            age_hours = (now - finished).total_seconds() / 3600.0
            reasons.append(
                f"last successful cycle is stale: {age_hours:.2f}h > {float(max_age_hours):.2f}h"
            )

    recent = cycles[-max(int(hard_error_limit), 1):]
    if len(recent) >= int(hard_error_limit) and all(row.get("hard_error") or int(row.get("exit_code") or 0) for row in recent):
        reasons.append(f"last {int(hard_error_limit)} scheduled cycles ended in hard_error")

    registry_ok, registry_reasons = registry_self_test()
    if not registry_ok:
        reasons.extend(registry_reasons)

    exposure = {}
    table = "demo_orders" if str(getattr(ctx.settings, "execution_mode", "")).lower() == "demo" else "live_orders"
    try:
        exposure = reconcile_open_exposure(
            ctx,
            ResearchStore(ctx.settings.research_db_path),
            table,
            game_id=ctx.settings.game_id,
        )
        if exposure.get("warnings"):
            reasons.extend(str(reason) for reason in exposure["warnings"])
        if not exposure.get("exchange_ok", False):
            reasons.append("exchange is unreachable for exposure reconciliation")
    except Exception as exc:
        reasons.append(f"exchange is unreachable for exposure reconciliation: {exc}")

    verdict = {
        "checked_at": utc_now(),
        "ok": not reasons,
        "alert": bool(reasons),
        "reasons": reasons,
        "last_successful_cycle": last_success,
        "recent_cycles": recent,
        "registry_ok": registry_ok,
        "exposure_reconciliation": exposure,
        "delivery_failures": read_delivery_failures(ctx.settings.data_dir),
    }
    health_status_path(ctx.settings.data_dir).write_text(json.dumps(verdict, indent=2, sort_keys=True, default=str))
    return verdict


def read_health_status(data_dir: Path) -> dict[str, Any] | None:
    path = health_status_path(data_dir)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"ok": False, "reasons": ["health status file unreadable"]}
    return loaded if isinstance(loaded, dict) else None
