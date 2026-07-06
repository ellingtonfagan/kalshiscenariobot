"""Freshness checks and live refresh helpers for odds/order-book artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .research import utc_now

TIMESTAMP_KEYS = ("generated_at", "verified_at", "captured_at", "updated_at", "ran_at")


def parse_timestamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def payload_timestamp(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in TIMESTAMP_KEYS:
        if payload.get(key):
            return str(payload[key])
    latest: datetime | None = None
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        parsed = parse_timestamp(payload_timestamp(row))
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    for snap in payload.get("snapshots") or []:
        if not isinstance(snap, dict):
            continue
        parsed = parse_timestamp(payload_timestamp(snap))
        if parsed and (latest is None or parsed > latest):
            latest = parsed
        for book in snap.get("books") or []:
            if not isinstance(book, dict):
                continue
            parsed = parse_timestamp(payload_timestamp(book))
            if parsed and (latest is None or parsed > latest):
                latest = parsed
        for metrics in (snap.get("metrics") or {}).values():
            if not isinstance(metrics, dict):
                continue
            for side in ("yes", "no"):
                parsed = parse_timestamp(payload_timestamp(metrics.get(side)))
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
    return latest.isoformat() if latest else None


def freshness_report(
    ctx: Any,
    suffix: str,
    payload: dict[str, Any] | None = None,
    *,
    max_age_seconds: int | None = None,
) -> dict[str, Any]:
    payload = payload if payload is not None else (ctx.read_json(suffix) or {})
    max_age = int(max_age_seconds or getattr(ctx.settings, "stale_market_seconds", 90))
    timestamp = payload_timestamp(payload)
    parsed = parse_timestamp(timestamp)
    now = datetime.now(timezone.utc)
    age = (now - parsed).total_seconds() if parsed else None
    fresh = age is not None and age <= max_age
    if not isinstance(payload, dict) or not payload:
        reason = "missing artifact"
    elif parsed is None:
        reason = "missing timestamp"
    elif fresh:
        reason = f"fresh <= {max_age}s"
    else:
        reason = f"stale > {max_age}s"
    return {
        "artifact": suffix,
        "checked_at": utc_now(),
        "timestamp": timestamp,
        "age_seconds": round(age, 3) if age is not None else None,
        "max_age_seconds": max_age,
        "fresh": fresh,
        "reason": reason,
    }


def refresh_if_stale(
    ctx: Any,
    suffix: str,
    runner: Callable[[Any], dict],
    *,
    max_age_seconds: int | None = None,
) -> dict[str, Any]:
    before = freshness_report(ctx, suffix, max_age_seconds=max_age_seconds)
    if before["fresh"]:
        return {"artifact": suffix, "refreshed": False, "before": before, "after": before}
    runner(ctx)
    after = freshness_report(ctx, suffix, max_age_seconds=max_age_seconds)
    return {"artifact": suffix, "refreshed": True, "before": before, "after": after}


def artifact_freshness(ctx: Any, suffixes: tuple[str, ...] | list[str]) -> dict[str, dict[str, Any]]:
    return {suffix: freshness_report(ctx, suffix) for suffix in suffixes}
