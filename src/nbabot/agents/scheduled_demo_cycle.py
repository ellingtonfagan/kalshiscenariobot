"""Phase: scheduled-demo-cycle. Cron-safe demo run with one final report."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import guardrails
from ..alerts import deliver
from .base import Context, load_context


def _now_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")


def _steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    steps = payload.get("steps")
    return steps if isinstance(steps, list) else []


def _step(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in _steps(payload):
        if item.get("name") == name:
            return item
    return None


def _step_result(payload: dict[str, Any], name: str) -> dict[str, Any]:
    item = _step(payload, name) or {}
    result = item.get("result")
    return result if isinstance(result, dict) else {}


def _count(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _sports_scanned(ctx: Context, cycle: dict[str, Any]) -> list[str]:
    slate = _step_result(cycle, "slate-discovery")
    sports = slate.get("sports")
    if isinstance(sports, list) and sports:
        return [str(sport) for sport in sports if sport]
    configured = os.environ.get("NBABOT_SLATE_SPORTS", "")
    if configured.strip():
        return [part.strip() for part in configured.split(",") if part.strip()]
    return [str(getattr(ctx.settings, "sport", "unknown") or "unknown")]


def _order_items(demo_result: dict[str, Any]) -> list[dict[str, Any]]:
    orders = demo_result.get("orders")
    if isinstance(orders, list):
        return [order for order in orders if isinstance(order, dict)]
    if "receipt" in demo_result or "intent" in demo_result:
        return [demo_result]
    return []


def _demo_order_summary(cycle: dict[str, Any]) -> tuple[int, list[str], str | None]:
    demo_step = _step(cycle, "demo-execute")
    execution_step = _step(cycle, "execution")
    if demo_step is None:
        if execution_step and execution_step.get("status") == "skipped":
            return 0, [], str(execution_step.get("reason") or "execution skipped")
        return 0, [], None

    if demo_step.get("status") == "error":
        return 0, [], None

    result = demo_step.get("result")
    result = result if isinstance(result, dict) else {}
    if result.get("reason") == "mode-blocked":
        detail = str(result.get("detail") or "demo mode blocked")
        return 0, [], f"ran, could not trade: {detail}"
    if result.get("reason"):
        return 0, [], str(result.get("reason"))

    placed: list[str] = []
    blocked: list[str] = []
    for item in _order_items(result):
        receipt = item.get("receipt") if isinstance(item.get("receipt"), dict) else {}
        intent = item.get("intent") if isinstance(item.get("intent"), dict) else {}
        status = str(receipt.get("status") or "")
        ticker = str(intent.get("ticker") or "")
        if status in {"submitted", "filled"}:
            if ticker:
                placed.append(ticker)
            continue
        if status:
            response = receipt.get("response") if isinstance(receipt.get("response"), dict) else {}
            reasons = response.get("reasons")
            if isinstance(reasons, list) and reasons:
                blocked.append("; ".join(str(reason) for reason in reasons))
            else:
                blocked.append(status)

    return len(placed), placed, "; ".join(blocked) if blocked else None


def _hard_errors(cycle: dict[str, Any], extra_errors: list[str]) -> list[str]:
    errors = list(extra_errors)
    for item in _steps(cycle):
        if item.get("status") != "error":
            continue
        name = str(item.get("name") or "unknown-step")
        error = str(item.get("error") or "error")
        errors.append(f"{name}: {error}")
    return errors


def _run_muted(ctx: Context, fn: Callable[[Context], dict]) -> dict:
    report_to = ctx.settings.deliver_to
    ctx.settings.deliver_to = "stdout"
    try:
        return fn(ctx)
    finally:
        ctx.settings.deliver_to = report_to


def _format_report(payload: dict[str, Any]) -> str:
    tickers = payload.get("demo_order_tickers") or []
    ticker_text = ",".join(tickers) if tickers else "none"
    hard_error = payload.get("hard_error") or "none"
    blocked = payload.get("blocked_reason") or "none"
    settlement = payload.get("settlement") or {}
    return "\n".join([
        f"[scheduled-demo-cycle] {payload['game_id']}",
        (
            f"time={payload['run_time_et']} mode={payload['mode']} "
            f"sports={','.join(payload['sports_scanned'])}"
        ),
        (
            f"candidates={payload['candidates_evaluated']} "
            f"edges_found={payload['edges_found']} "
            f"trade_eligible={payload['trade_eligible']}"
        ),
        (
            f"demo_orders={payload['demo_orders_placed']} "
            f"tickers={ticker_text}"
        ),
        (
            f"settlement checked={settlement.get('checked_count', 0)} "
            f"settled={settlement.get('settled_count', 0)} "
            f"pending={settlement.get('pending_count', 0)} "
            f"errors={settlement.get('error_count', 0)}"
        ),
        f"blocked={blocked}",
        f"hard_error={hard_error}",
    ])


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    report_to = ctx.settings.deliver_to
    ctx.settings.execution_mode = "demo"

    from . import daily_cycle, settlement_audit, status

    hard_errors: list[str] = []
    cycle: dict[str, Any] = {}
    settlement: dict[str, Any] = {}
    final_status: dict[str, Any] = {}

    try:
        cycle = _run_muted(ctx, daily_cycle.run)
    except Exception as exc:
        hard_errors.append(f"daily-cycle: {exc}")

    try:
        settlement = _run_muted(ctx, settlement_audit.run)
    except Exception as exc:
        hard_errors.append(f"settlement-audit: {exc}")

    try:
        final_status = _run_muted(ctx, status.run)
    except Exception as exc:
        hard_errors.append(f"status: {exc}")

    ranker = _step_result(cycle, "candidate-ranker")
    research = _step_result(cycle, "research-agent")
    orders_placed, tickers, blocked_reason = _demo_order_summary(cycle)
    hard_errors = _hard_errors(cycle, hard_errors)
    summary = settlement.get("summary") if isinstance(settlement.get("summary"), dict) else {}
    exit_code = 1 if hard_errors else 0

    payload = {
        "game_id": ctx.settings.game_id,
        "run_time_et": _now_et(),
        "mode": "demo",
        "dry_run": ctx.settings.dry_run,
        "sports_scanned": _sports_scanned(ctx, cycle),
        "candidates_evaluated": _count(ranker, "candidate_count"),
        "edges_found": _count(ranker, "edge_pass_count"),
        "trade_eligible": _count(
            research,
            "trade_eligible_count",
            "candidate_count",
        ),
        "demo_orders_placed": orders_placed,
        "demo_order_tickers": tickers,
        "blocked_reason": blocked_reason,
        "hard_error": "; ".join(hard_errors) if hard_errors else None,
        "exit_code": exit_code,
        "cycle": cycle,
        "settlement": {
            "checked_count": _count(settlement, "checked_count"),
            "pending_count": _count(settlement, "pending_count"),
            "error_count": _count(settlement, "error_count"),
            "settled_count": _count(summary, "settled_count"),
        },
        "status": final_status,
    }

    ctx.write_json("scheduled_demo_cycle.json", payload)
    ctx.settings.deliver_to = report_to
    payload["report_delivery_ok"] = deliver(
        guardrails.with_footer(_format_report(payload)),
        report_to,
    )
    ctx.write_json("scheduled_demo_cycle.json", payload)
    return payload
