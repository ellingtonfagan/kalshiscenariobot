"""Phase: meta-check. Watch the monitor itself and escalate on silence."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from ..alerts import deliver
from .base import Context, load_context


MONITOR_LABEL = "com.nbabot.monitor"


def _data_path(ctx: Context, name: str) -> Path:
    ctx.settings.data_dir.mkdir(parents=True, exist_ok=True)
    return ctx.settings.data_dir / name


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_gist_ref(ctx: Context) -> str | None:
    path = _data_path(ctx, "monitor_gist_id.txt")
    try:
        return path.read_text().strip() or None
    except Exception:
        return None


def _raw_gist_url(ref: str) -> str:
    if "gist.githubusercontent.com" in ref and "/raw" in ref:
        return ref
    if "gist.github.com/" in ref:
        parts = ref.rstrip("/").split("/")
        if len(parts) >= 2:
            user = parts[-2]
            gist_id = parts[-1]
            return f"https://gist.githubusercontent.com/{user}/{gist_id}/raw/monitor.md"
    return ref


def _http_get(url: str, *, timeout: int = 10) -> tuple[bool, str, int | None]:
    try:
        response = requests.get(url, timeout=timeout)
        status = int(getattr(response, "status_code", 0) or 0)
        return status == 200, response.text, status
    except Exception as exc:
        return False, str(exc), None


def _launchd_has_monitor(label: str = MONITOR_LABEL) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False, "launchctl not installed"
    except Exception as exc:
        return False, str(exc)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0 and label in output, output[:500]


def _telegram_target(ctx: Context) -> str:
    target = str(getattr(ctx.settings, "deliver_to", "") or "")
    return target if target.startswith("telegram") else "telegram"


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    now = datetime.now(timezone.utc)
    failures: list[str] = []

    heartbeat = _data_path(ctx, "monitor_heartbeat.txt")
    heartbeat_dt = _parse_dt(heartbeat.read_text()) if heartbeat.exists() else None
    if heartbeat_dt is None:
        failures.append("monitor heartbeat missing or unparsable")
    elif now - heartbeat_dt >= timedelta(hours=3):
        failures.append("monitor heartbeat older than 3h")

    gist_ref = _read_gist_ref(ctx)
    gist_ok = False
    gist_status = None
    if not gist_ref:
        failures.append("monitor gist URL missing")
    else:
        gist_ok, gist_body, gist_status = _http_get(_raw_gist_url(gist_ref))
        if not gist_ok:
            failures.append(f"monitor gist HTTP status {gist_status or 'unreachable'}")
        else:
            body_lower = gist_body.lower()
            recent_marker = str(now.year) in gist_body or now.strftime("%Y-%m-%d") in gist_body
            if "monitor" not in body_lower or not recent_marker:
                failures.append("monitor gist content is not plausible")

    launchd_ok, launchd_detail = _launchd_has_monitor()
    if not launchd_ok:
        failures.append("monitor launchd agent missing from launchctl list")

    alert_sent = False
    if failures:
        text = "🔴 URGENT: meta-check monitor failure\n" + "\n".join(f"- {item}" for item in failures)
        alert_sent = bool(deliver(text, _telegram_target(ctx)))

    result = {
        "ok": not failures,
        "checked_at": now.isoformat(),
        "failures": failures,
        "heartbeat_at": heartbeat_dt.isoformat() if heartbeat_dt else None,
        "gist_ref": gist_ref,
        "gist_status": gist_status,
        "launchd_monitor_present": launchd_ok,
        "launchd_detail": launchd_detail[:300],
        "alert_sent": alert_sent,
    }
    _data_path(ctx, "monitor_meta_check.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    )
    return result
