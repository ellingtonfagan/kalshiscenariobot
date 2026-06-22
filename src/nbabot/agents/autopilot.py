"""Phase: autopilot. Safe repeated orchestration for one game."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .. import scores
from ..alerts import deliver
from ..audit import AuditTrail
from ..research import ResearchStore
from .base import Context, load_context, resolve_event_id

StepFn = Callable[[Context], dict]


def _artifact_exists(ctx: Context, suffix: str) -> bool:
    return ctx.settings.data_path(suffix).exists()


def _order_artifact_submitted(ctx: Context, suffix: str) -> bool:
    payload = ctx.read_json(suffix) or {}
    receipt = payload.get("receipt", {})
    return receipt.get("status") in {"submitted", "filled"}


def _minutes_to_tip(ctx: Context) -> float | None:
    raw = (ctx.settings.game.get("game", {}) or {}).get("tip_iso")
    if not raw:
        return None
    try:
        tip = datetime.fromisoformat(raw)
    except ValueError:
        return None
    now = datetime.now(tip.tzinfo or timezone.utc)
    return (tip - now).total_seconds() / 60.0


def _run_step(ctx: Context, name: str, fn: StepFn, steps: list[dict[str, Any]],
              audit: AuditTrail) -> dict | None:
    try:
        result = fn(ctx)
    except Exception as e:  # fail closed: record the failure and stop dependent work
        payload = {"step": name, "error": str(e)}
        audit.dead_letter("AUTOPILOT_STEP", str(e), payload, ctx.settings.game_id)
        steps.append({"name": name, "status": "error", "error": str(e)})
        return None
    steps.append({"name": name, "status": "ok", "result": result})
    return result


def _execution_fn(ctx: Context) -> tuple[str, StepFn]:
    from . import demo_execute, live_execute, paper

    if ctx.settings.execution_mode == "live":
        return "live-execute", live_execute.run
    if ctx.settings.execution_mode == "demo":
        return "demo-execute", demo_execute.run
    return "paper", paper.run


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    steps: list[dict[str, Any]] = []

    from . import backtest, baseline, discover_markets, heartbeat, lineups, lock
    from . import reconcile, snapshot_market

    event_id = resolve_event_id(ctx)
    game_state = scores.get_game_state(event_id) if event_id else scores.GameState()
    minutes_to_tip = _minutes_to_tip(ctx)

    _run_step(ctx, "discover-markets", discover_markets.run, steps, audit)

    if game_state.state == "post":
        if not _artifact_exists(ctx, "reconcile.json"):
            _run_step(ctx, "reconcile", reconcile.run, steps, audit)
        else:
            steps.append({"name": "reconcile", "status": "skipped", "reason": "already-reconciled"})
        _run_step(ctx, "backtest", backtest.run, steps, audit)
    elif game_state.state == "in":
        _run_step(ctx, "heartbeat", heartbeat.run, steps, audit)
        snapshot = _run_step(ctx, "snapshot-market", snapshot_market.run, steps, audit)
        if snapshot is not None:
            exec_name, exec_fn = _execution_fn(ctx)
            if exec_name == "demo-execute" and _order_artifact_submitted(ctx, "demo_execute.json"):
                steps.append({"name": exec_name, "status": "skipped", "reason": "demo-already-submitted"})
            else:
                _run_step(ctx, exec_name, exec_fn, steps, audit)
    else:
        if not _artifact_exists(ctx, "board.json"):
            _run_step(ctx, "baseline", baseline.run, steps, audit)
        if minutes_to_tip is not None and minutes_to_tip <= 120 and not _artifact_exists(ctx, "lineups.json"):
            _run_step(ctx, "lineups", lineups.run, steps, audit)
        if minutes_to_tip is not None and minutes_to_tip <= 45 and not _artifact_exists(ctx, "locked_board.json"):
            _run_step(ctx, "lock", lock.run, steps, audit)
        snapshot = _run_step(ctx, "snapshot-market", snapshot_market.run, steps, audit)
        if snapshot is not None:
            exec_name, exec_fn = _execution_fn(ctx)
            if exec_name == "demo-execute":
                steps.append({"name": exec_name, "status": "skipped", "reason": "pregame-demo-disabled"})
            else:
                _run_step(ctx, exec_name, exec_fn, steps, audit)

    payload = {
        "game_id": ctx.settings.game_id,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "event_id": event_id,
        "game_state": game_state.state,
        "minutes_to_tip": minutes_to_tip,
        "execution_mode": ctx.settings.execution_mode,
        "dry_run": ctx.settings.dry_run,
        "steps": steps,
    }
    ctx.write_json("autopilot.json", payload)
    failures = sum(1 for step in steps if step.get("status") == "error")
    deliver(
        f"[autopilot] {ctx.settings.game_id}: state={game_state.state} "
        f"steps={len(steps)} failures={failures} mode={ctx.settings.execution_mode} "
        f"dry_run={ctx.settings.dry_run}",
        ctx.settings.deliver_to,
    )
    return payload
