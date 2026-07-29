"""Phase: health-check. Dead-man alarm for scheduled cycle and exchange health."""
from __future__ import annotations

import os
import json

from ..alerts import deliver
from ..cycle_health import build_health_verdict, health_status_path, record_delivery_result
from .base import Context, load_context


def _format(payload: dict) -> str:
    prefix = "ALERT [health-check]" if payload.get("alert") else "[health-check]"
    reasons = payload.get("reasons") or []
    if not reasons:
        return f"{prefix} ok"
    return "\n".join([f"{prefix} failed"] + [f"- {reason}" for reason in reasons])


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    max_age = float(os.environ.get("NBABOT_HEARTBEAT_MAX_AGE_HOURS", "8"))
    hard_limit = int(os.environ.get("NBABOT_HEARTBEAT_HARD_ERROR_LIMIT", "2"))
    payload = build_health_verdict(ctx, max_age_hours=max_age, hard_error_limit=hard_limit)
    ok = deliver(_format(payload), ctx.settings.deliver_to)
    payload["delivery_ok"] = ok
    payload["delivery_failures"] = record_delivery_result(
        ctx.settings.data_dir,
        ok=ok,
        target=ctx.settings.deliver_to,
        phase="health-check",
    )
    health_status_path(ctx.settings.data_dir).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str)
    )
    ctx.write_json("health_check.json", payload)
    payload["exit_code"] = 0 if payload.get("ok") else 1
    return payload
