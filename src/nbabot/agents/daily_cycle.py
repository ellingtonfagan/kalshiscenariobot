"""Phase: daily-cycle. Autonomous research/execution loop for one configured event."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..research import ResearchStore
from .base import Context, load_context

StepFn = Callable[[Context], dict]


def _run_step(ctx: Context, name: str, fn: StepFn, steps: list[dict[str, Any]],
              audit: AuditTrail) -> dict | None:
    try:
        result = fn(ctx)
    except Exception as e:
        audit.dead_letter("DAILY_CYCLE_STEP", str(e), {"step": name}, ctx.settings.game_id)
        steps.append({"name": name, "status": "error", "error": str(e)})
        return None
    steps.append({"name": name, "status": "ok", "result": result})
    return result


def _execution_step(ctx: Context) -> tuple[str, StepFn] | None:
    from . import demo_execute, live_execute

    if ctx.settings.execution_mode == "live":
        return "live-execute", live_execute.run
    if ctx.settings.execution_mode == "demo":
        return "demo-execute", demo_execute.run
    return None


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    steps: list[dict[str, Any]] = []

    from . import backtest, book_watch, discover_markets, portfolio_sync, snapshot_market, status

    _run_step(ctx, "portfolio-sync", portfolio_sync.run, steps, audit)
    _run_step(ctx, "discover-markets", discover_markets.run, steps, audit)
    snapshot = _run_step(ctx, "snapshot-market", snapshot_market.run, steps, audit)
    if snapshot is not None:
        _run_step(ctx, "book-watch", book_watch.run, steps, audit)

    exec_step = _execution_step(ctx)
    if exec_step is None:
        steps.append({
            "name": "execution",
            "status": "skipped",
            "reason": "daily-cycle does not paper trade; set live/demo mode explicitly",
        })
    else:
        exec_name, exec_fn = exec_step
        _run_step(ctx, exec_name, exec_fn, steps, audit)

    if ctx.settings.data_path("log.jsonl").exists():
        _run_step(ctx, "backtest", backtest.run, steps, audit)
    else:
        steps.append({"name": "backtest", "status": "skipped", "reason": "no learning log"})

    final_status = _run_step(ctx, "status", status.run, steps, audit)
    payload = {
        "game_id": ctx.settings.game_id,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": ctx.settings.execution_mode,
        "dry_run": ctx.settings.dry_run,
        "steps": steps,
        "status": final_status,
    }
    ctx.write_json("daily_cycle.json", payload)
    failures = sum(1 for step in steps if step.get("status") == "error")
    skipped = sum(1 for step in steps if step.get("status") == "skipped")
    deliver(
        guardrails.with_footer(
            f"[daily-cycle] {ctx.settings.game_id}: steps={len(steps)} "
            f"failures={failures} skipped={skipped} mode={ctx.settings.execution_mode} "
            f"dry_run={ctx.settings.dry_run}"
        ),
        ctx.settings.deliver_to,
    )
    return payload
