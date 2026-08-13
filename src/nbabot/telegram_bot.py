"""Telegram command polling for operator status checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

from . import guardrails


MAX_TELEGRAM_TEXT = 4096


def _token() -> str:
    return os.environ.get("NBABOT_TELEGRAM_BOT_TOKEN", "").strip()


def configured_chat_id() -> str:
    return os.environ.get("NBABOT_TELEGRAM_CHAT_ID", "").strip()


def offset_path(ctx: Any) -> Path:
    ctx.settings.data_dir.mkdir(parents=True, exist_ok=True)
    return ctx.settings.data_dir / "telegram_bot_offset.json"


def read_offset(ctx: Any) -> int | None:
    path = offset_path(ctx)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    try:
        return int(payload.get("offset"))
    except (AttributeError, TypeError, ValueError):
        return None


def write_offset(ctx: Any, offset: int) -> None:
    offset_path(ctx).write_text(json.dumps({"offset": int(offset)}, indent=2) + "\n")


def get_updates(
    token: str,
    *,
    offset: int | None = None,
    timeout_seconds: int = 0,
    requests_module: Any = requests,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": max(int(timeout_seconds), 0),
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = int(offset)
    response = requests_module.post(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=max(int(timeout_seconds), 0) + 6,
    )
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(str(body.get("description") or "Telegram getUpdates failed"))
    result = body.get("result") or []
    return [row for row in result if isinstance(row, dict)]


def send_message(
    token: str,
    chat_id: str | int,
    text: str,
    *,
    requests_module: Any = requests,
) -> bool:
    response = requests_module.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({
            "chat_id": str(chat_id),
            "text": text[:MAX_TELEGRAM_TEXT],
            "disable_web_page_preview": True,
        }),
        headers={"Content-Type": "application/json"},
        timeout=6,
    )
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    return True


def command_from_update(update: dict[str, Any]) -> tuple[str | None, str | None, int | None]:
    message = update.get("message")
    if not isinstance(message, dict):
        return None, None, None
    chat = message.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return None, str(chat_id) if chat_id is not None else None, update.get("update_id")
    command = text.split()[0].split("@", 1)[0].lower()
    return command, str(chat_id) if chat_id is not None else None, update.get("update_id")


def format_status_reply(ctx: Any) -> str:
    from .agents import status as status_agent

    monitor_md = ctx.settings.data_dir / "monitor.md"
    if monitor_md.exists():
        text = monitor_md.read_text().strip()
        if text:
            return guardrails.with_footer(text)
    status = status_agent.build_status(ctx)
    return guardrails.with_footer(status_agent._format_status(status))


def handle_update(
    ctx: Any,
    update: dict[str, Any],
    token: str,
    *,
    requests_module: Any = requests,
) -> dict[str, Any]:
    command, chat_id, update_id = command_from_update(update)
    configured_chat = configured_chat_id()
    result = {
        "update_id": update_id,
        "chat_id": chat_id,
        "command": command,
        "handled": False,
        "sent": False,
        "reason": None,
    }
    if command is None:
        result["reason"] = "not-command"
        return result
    if not chat_id:
        result["reason"] = "missing-chat-id"
        return result
    if configured_chat and str(chat_id) != str(configured_chat):
        result["reason"] = "unauthorized-chat"
        return result

    if command == "/status":
        text = format_status_reply(ctx)
    elif command in {"/help", "/start"}:
        text = "Commands: /status"
    else:
        result["reason"] = "unknown-command"
        return result

    result["sent"] = send_message(token, chat_id, text, requests_module=requests_module)
    result["handled"] = bool(result["sent"])
    return result


def run_once(ctx: Any, *, requests_module: Any = requests) -> dict[str, Any]:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing-telegram-token", "updates": []}
    if not configured_chat_id():
        return {"ok": False, "reason": "missing-telegram-chat-id", "updates": []}
    offset = read_offset(ctx)
    timeout_seconds = int(os.environ.get("NBABOT_TELEGRAM_POLL_TIMEOUT_SECONDS", "0"))
    updates = get_updates(
        token,
        offset=offset,
        timeout_seconds=timeout_seconds,
        requests_module=requests_module,
    )
    rows = []
    max_update_id = None
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            max_update_id = max(int(update_id), int(max_update_id or update_id))
        try:
            rows.append(handle_update(ctx, update, token, requests_module=requests_module))
        except Exception as exc:
            rows.append({
                "update_id": update_id,
                "handled": False,
                "sent": False,
                "reason": str(exc),
            })
    if max_update_id is not None:
        write_offset(ctx, int(max_update_id) + 1)
    return {
        "ok": True,
        "update_count": len(updates),
        "handled_count": sum(1 for row in rows if row.get("handled")),
        "updates": rows,
        "next_offset": int(max_update_id) + 1 if max_update_id is not None else offset,
    }
