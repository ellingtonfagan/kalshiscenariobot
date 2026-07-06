"""Phase: telegram-test. Send a Telegram delivery smoke-test."""
from __future__ import annotations

import os

from ..alerts import deliver
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    target = ctx.settings.deliver_to
    token = os.environ.get("NBABOT_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("NBABOT_TELEGRAM_CHAT_ID", "")
    target_has_chat = target.startswith("telegram:") and bool(target.split(":", 1)[1].strip())

    if not target.startswith("telegram"):
        msg = "[telegram-test] set NBABOT_DELIVER_TO=telegram or telegram:<chat_id>"
        deliver(msg, "stdout")
        return {"ok": False, "reason": "deliver-target-not-telegram"}

    if not token or (not chat_id and not target_has_chat):
        msg = "[telegram-test] missing NBABOT_TELEGRAM_BOT_TOKEN or NBABOT_TELEGRAM_CHAT_ID"
        deliver(msg, "stdout")
        return {"ok": False, "reason": "missing-telegram-config"}

    deliver(
        f"[telegram-test] {ctx.settings.game_id}: Telegram alerts are configured.",
        target,
    )
    return {"ok": True, "target": "telegram"}
