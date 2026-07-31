"""Phase: monitor. Compose one operator snapshot and deliver it fail-soft."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..alerts import deliver
from ..monitor import (
    MonitorSnapshot,
    build_monitor_snapshot,
    format_monitor_md,
    read_balance_history,
    snapshot_to_json,
    trailing_balance_drop_pct,
)
from ..research import ResearchStore
from .base import Context, load_context


DELIVERY_STATE = "monitor_delivery_health.json"
ALERT_STATE = "monitor_alert_state.json"
BALANCE_HISTORY = "monitor_balance_history.jsonl"


def _data_path(ctx: Context, name: str) -> Path:
    ctx.settings.data_dir.mkdir(parents=True, exist_ok=True)
    return ctx.settings.data_dir / name


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _run_command(args: list[str], *, timeout: int = 10) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, f"{args[0]} not installed"
    except Exception as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, (proc.stdout or proc.stderr or "").strip()
    return False, (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()


def _git_commit() -> str | None:
    ok, out = _run_command(["git", "rev-parse", "HEAD"], timeout=5)
    return out.strip() if ok and out else None


def _pending_prs() -> tuple[list[int], str | None]:
    ok, out = _run_command(
        [
            "gh",
            "pr",
            "list",
            "--head",
            "codex/broad-slate-market-matcher",
            "--json",
            "number",
        ],
        timeout=10,
    )
    if not ok:
        return [], out
    try:
        loaded = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        return [], f"gh pr list parse failed: {exc}"
    if not isinstance(loaded, list):
        return [], "gh pr list returned non-list"
    numbers = []
    for item in loaded:
        if isinstance(item, dict) and item.get("number") is not None:
            try:
                numbers.append(int(item["number"]))
            except (TypeError, ValueError):
                pass
    return numbers, None


def _append_balance(ctx: Context, snapshot: MonitorSnapshot) -> None:
    if snapshot.balance_usd is None:
        return
    path = _data_path(ctx, BALANCE_HISTORY)
    with path.open("a") as f:
        f.write(json.dumps({
            "generated_at": snapshot.generated_at,
            "balance_usd": snapshot.balance_usd,
        }, sort_keys=True) + "\n")


def _transition_events(ctx: Context, active_conditions: list[str]) -> tuple[list[str], dict[str, Any]]:
    path = _data_path(ctx, ALERT_STATE)
    state = _read_json(path)
    previous = set(state.get("active_conditions") or [])
    current = set(active_conditions)
    events = [f"ALERT: {name}" for name in sorted(current - previous)]
    events.extend(f"ALL CLEAR: {name}" for name in sorted(previous - current))
    new_state = {
        "active_conditions": sorted(current),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }
    _write_json(path, new_state)
    return events, new_state


def _telegram_target(ctx: Context) -> str:
    target = str(getattr(ctx.settings, "deliver_to", "") or "")
    return target if target.startswith("telegram") else "telegram"


def _deliver_telegram(ctx: Context, text: str) -> tuple[bool, str]:
    ok = bool(deliver(text, _telegram_target(ctx)))
    return ok, "ok" if ok else "telegram deliver returned false"


def _deliver_osascript(text: str) -> tuple[bool, str]:
    title = "nbabot monitor"
    compact = text.replace('"', "'")[:220]
    return _run_command([
        "osascript",
        "-e",
        f'display notification "{compact}" with title "{title}"',
    ], timeout=10)


def _deliver_gist(ctx: Context, monitor_path: Path) -> tuple[bool, str]:
    id_path = _data_path(ctx, "monitor_gist_id.txt")
    if id_path.exists() and id_path.read_text().strip():
        gist_id = id_path.read_text().strip()
        ok, out = _run_command(["gh", "gist", "edit", gist_id, str(monitor_path)], timeout=20)
        return ok, out if out else gist_id
    ok, out = _run_command(
        ["gh", "gist", "create", str(monitor_path), "-d", "nbabot monitor"],
        timeout=20,
    )
    if not ok:
        return False, out
    gist_ref = out.splitlines()[-1].strip()
    id_path.write_text(gist_ref + "\n")
    return True, gist_ref


def _attempt_delivery(
    ctx: Context,
    *,
    text: str,
    monitor_path: Path,
    escalated: bool,
) -> list[dict[str, Any]]:
    """Deliver the routine snapshot AND always update the persistent gist.

    Push channels (telegram, osascript) are notifications: fire the primary and
    fall through only on failure, unless escalated, in which case try all so
    redundancy is real. The gist is DIFFERENT -- it is the persistent
    state-of-record URL a phone/browser can hit anytime and Claude can WebFetch
    across sessions, so it always runs regardless of push outcome.
    """
    attempts: list[dict[str, Any]] = []
    push_channels = [
        ("telegram", lambda: _deliver_telegram(ctx, text)),
        ("osascript", lambda: _deliver_osascript(text)),
    ]
    for name, fn in push_channels:
        ok, detail = fn()
        attempts.append({"channel": name, "ok": bool(ok), "detail": str(detail)[:500]})
        if ok and not escalated:
            break
    # Gist push always runs so the URL is always current.
    gist_ok, gist_detail = _deliver_gist(ctx, monitor_path)
    attempts.append({"channel": "gist", "ok": bool(gist_ok), "detail": str(gist_detail)[:500]})
    return attempts


def _record_delivery_health(ctx: Context, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    path = _data_path(ctx, DELIVERY_STATE)
    previous = _read_json(path)
    ok = any(bool(row.get("ok")) for row in attempts)
    now = datetime.now(timezone.utc).isoformat()
    if ok:
        state = {
            "consecutive_failures": 0,
            "failing": False,
            "last_success_at": now,
            "last_zero_success_at": previous.get("last_zero_success_at"),
            "last_attempts": attempts,
        }
    else:
        state = {
            "consecutive_failures": int(previous.get("consecutive_failures") or 0) + 1,
            "failing": True,
            "last_success_at": previous.get("last_success_at"),
            "last_zero_success_at": now,
            "last_attempts": attempts,
        }
    _write_json(path, state)
    return state


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    generated_at = datetime.now(timezone.utc).isoformat()
    pending_prs, pr_error = _pending_prs()
    history = read_balance_history(ctx.settings.data_dir)
    provisional = build_monitor_snapshot(
        ctx,
        store,
        generated_at=generated_at,
        git_commit=_git_commit(),
        pending_prs=pending_prs,
    )
    balance_drop = trailing_balance_drop_pct(
        history,
        current_balance=provisional.balance_usd,
        now=datetime.fromisoformat(generated_at),
    )
    snapshot = build_monitor_snapshot(
        ctx,
        store,
        generated_at=generated_at,
        git_commit=provisional.git_commit,
        pending_prs=pending_prs,
        balance_drop_pct=balance_drop,
    )
    errors = list(snapshot.errors)
    if pr_error:
        errors.append(f"pending_prs: {pr_error}")
    snapshot = MonitorSnapshot(**{**snapshot.to_dict(), "errors": errors})
    _append_balance(ctx, snapshot)

    transition_events, _state = _transition_events(ctx, snapshot.escalated_alert_conditions)
    md = format_monitor_md(snapshot)
    delivery_text = md
    if transition_events:
        delivery_text = "\n".join(transition_events) + "\n\n" + md
    snapshot_path = _data_path(ctx, "monitor_snapshot.json")
    md_path = _data_path(ctx, "monitor.md")
    snapshot_path.write_text(snapshot_to_json(snapshot))
    md_path.write_text(md)

    attempts = _attempt_delivery(
        ctx,
        text=delivery_text,
        monitor_path=md_path,
        escalated=bool(transition_events),
    )
    delivery_health = _record_delivery_health(ctx, attempts)
    snapshot = MonitorSnapshot(**{
        **snapshot.to_dict(),
        "alerts_delivery_health": {
            "consecutive_failures": int(delivery_health.get("consecutive_failures") or 0),
            "last_success_at": delivery_health.get("last_success_at"),
            "failing": bool(delivery_health.get("failing")),
            "last_attempts": delivery_health.get("last_attempts") or [],
        },
        "delivery_attempts": attempts,
    })
    snapshot_path.write_text(snapshot_to_json(snapshot))
    return snapshot.to_dict()
